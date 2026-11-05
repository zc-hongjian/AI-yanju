"""Web 版入口：FastAPI + 单页 HTML，复用同一张 LangGraph 图与同一套工具/人工确认逻辑。

启动：
    python web.py
    # 或
    uvicorn web:app --host 127.0.0.1 --port 8000

如果当前解释器缺少依赖（常见原因：在根目录 uv 环境里运行了本文件），会优先自动切换到
项目自带的 .venv；找不到时给出可直接复制的安装命令。

API：
    POST /api/research          {"question": "..."}            -> 第一次中断或最终结果
    POST /api/resume            {"thread_id": "...", "value": {...}} -> 下一次中断或最终结果
    POST /api/research/stream   同上，但用 NDJSON 流式返回实时日志 + 最终结果
    POST /api/resume/stream     同上
    GET  /api/health

会话状态存放在 LangGraph checkpointer（InMemorySaver）里，按 thread_id 隔离；
进程重启后未完成的会话会丢失。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
REQUIREMENTS = BASE_DIR / "requirements.txt"
PROJECT_VENV_PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"


def _bootstrap_dependency(exc: ModuleNotFoundError) -> None:
    """当前解释器缺少依赖时：优先自动切到项目 .venv，否则打印安装命令并退出。"""
    current = Path(sys.executable)
    if PROJECT_VENV_PYTHON.exists() and current.resolve() != PROJECT_VENV_PYTHON.resolve():
        print(f"[提示] 当前解释器缺少依赖 {exc.name!r}：{current}", flush=True)
        print(f"       已自动切换到项目虚拟环境：{PROJECT_VENV_PYTHON}", flush=True)
        completed = subprocess.run(
            [str(PROJECT_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]]
        )
        raise SystemExit(completed.returncode)
    print(
        f"[错误] 当前 Python 解释器缺少依赖 {exc.name!r}：{current}\n"
        f'       请安装："{current}" -m pip install -r "{REQUIREMENTS}"\n'
        f"       或使用项目虚拟环境：{PROJECT_VENV_PYTHON} {Path(__file__).resolve()}",
        file=sys.stderr,
    )
    raise SystemExit(1)


try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
    from langgraph.types import Command
    from pydantic import BaseModel

    from dotenv import load_dotenv

    from graph import build_graph
    from llm_client import get_llm
    from logging_utils import register_log_sink, unregister_log_sink
    from state import new_state
except ModuleNotFoundError as exc:  # 依赖没装（最常见：用错了虚拟环境）
    _bootstrap_dependency(exc)

# 必须在创建图 / 第一次请求前加载 .env，否则 web 版会一直停留在离线模式
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="LangGraph 研究助手", version="0.1.0")
GRAPH = build_graph()
try:
    _startup_llm = get_llm()
    print(
        f"[研究助手] LLM 模式={_startup_llm.mode} | 模型={_startup_llm.model} | "
        f"Base={_startup_llm.base_url or 'OpenAI 默认'}（改 .env 后需重启本服务）",
        flush=True,
    )
except Exception as exc:  # noqa: BLE001 - 启动日志失败不影响服务
    print(f"[研究助手] LLM 配置读取失败：{exc}", flush=True)
_SESSIONS: dict[str, str] = {}  # thread_id -> trace_id，流式 resume 需要靠它订阅日志


class ResearchRequest(BaseModel):
    question: str


class ResumeRequest(BaseModel):
    thread_id: str
    value: dict[str, Any]


def _pack(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """把图的一次 invoke 结果包装成前端需要的形状。"""
    interrupts = result.get("__interrupt__") or []
    payload = interrupts[0].value if interrupts else None
    return {
        "thread_id": thread_id,
        "status": "interrupt" if payload is not None else "completed",
        "interrupt": payload,
        "logs": result.get("logs") or [],
        "final_output": result.get("final_output") or None,
    }


def _start_run(question: str) -> tuple[str, str, Any, dict[str, Any]]:
    trace_id = uuid.uuid4().hex[:8]
    thread_id = f"web-{trace_id}"
    _SESSIONS[thread_id] = trace_id
    state = new_state(question, trace_id=trace_id)
    config = {"configurable": {"thread_id": thread_id}}
    return trace_id, thread_id, state, config


def _stream_events(
    trace_id: str,
    thread_id: str,
    runner: Callable[[], dict[str, Any]],
) -> Iterator[str]:
    """NDJSON 流：实时推日志，最后推 result 或 error。

    图的执行放在后台线程，日志通过 logging_utils 的 sink 推进队列；
    StreamingResponse 在读队列时逐步把日志发给浏览器。
    """
    events: "queue.Queue[dict[str, Any]]" = queue.Queue()

    def sink(entry: dict[str, Any]) -> None:
        events.put({"type": "log", "entry": entry})

    register_log_sink(trace_id, sink)
    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["result"] = runner()
        except Exception as exc:  # noqa: BLE001 - 转成流里的 error 事件
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            events.put({"type": "__done__"})

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        while True:
            event = events.get()
            if event.get("type") == "__done__":
                break
            yield json.dumps(event, ensure_ascii=False) + "\n"
        if "error" in outcome:
            yield json.dumps({"type": "error", "message": outcome["error"]}, ensure_ascii=False) + "\n"
        else:
            payload = _pack(thread_id, outcome["result"])
            yield json.dumps({"type": "result", "data": payload}, ensure_ascii=False) + "\n"
    finally:
        unregister_log_sink(trace_id, sink)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=500, detail="缺少 index.html")
    return FileResponse(INDEX_FILE)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "llm_mode": get_llm().mode}


@app.post("/api/research")
def start_research(req: ResearchRequest) -> dict[str, Any]:
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="研究问题不能为空")
    trace_id, thread_id, state, config = _start_run(question)
    try:
        result = GRAPH.invoke(state, config)
    except Exception as exc:  # noqa: BLE001 - 接口层统一转成 500，前端展示
        raise HTTPException(status_code=500, detail=f"研究流程失败：{exc}") from exc
    return _pack(thread_id, result)


@app.post("/api/research/stream")
def start_research_stream(req: ResearchRequest) -> StreamingResponse:
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="研究问题不能为空")
    trace_id, thread_id, state, config = _start_run(question)
    return StreamingResponse(
        _stream_events(trace_id, thread_id, lambda: GRAPH.invoke(state, config)),
        media_type="application/x-ndjson",
    )


@app.post("/api/resume")
def resume(req: ResumeRequest) -> dict[str, Any]:
    thread_id = (req.thread_id or "").strip()
    if not thread_id:
        raise HTTPException(status_code=400, detail="缺少 thread_id")
    config = {"configurable": {"thread_id": thread_id}}
    try:
        result = GRAPH.invoke(Command(resume=req.value), config)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"恢复执行失败：{exc}") from exc
    return _pack(thread_id, result)


@app.post("/api/resume/stream")
def resume_stream(req: ResumeRequest) -> StreamingResponse:
    thread_id = (req.thread_id or "").strip()
    if not thread_id:
        raise HTTPException(status_code=400, detail="缺少 thread_id")
    trace_id = _SESSIONS.get(thread_id)
    if not trace_id:
        raise HTTPException(
            status_code=400,
            detail="会话不存在或服务已重启，请重新开始研究",
        )
    config = {"configurable": {"thread_id": thread_id}}
    value = req.value
    return StreamingResponse(
        _stream_events(
            trace_id,
            thread_id,
            lambda: GRAPH.invoke(Command(resume=value), config),
        ),
        media_type="application/x-ndjson",
    )


if __name__ == "__main__":
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        _bootstrap_dependency(exc)
    port = int(os.getenv("AGENT_WEB_PORT", "8000"))
    print(f"研究助手 Web 版：http://127.0.0.1:{port}")
    uvicorn.run("web:app", host="127.0.0.1", port=port, reload=False)
