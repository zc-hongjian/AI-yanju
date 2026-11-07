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
from urllib.parse import urlparse

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
    from llm_client import LLMClient, get_llm
    import llm_client
    from logging_utils import register_log_sink, unregister_log_sink
    from state import new_state
except ModuleNotFoundError as exc:  # 依赖没装（最常见：用错了虚拟环境）
    _bootstrap_dependency(exc)

# 必须在创建图 / 第一次请求前加载 .env，否则 web 版会一直停留在离线模式
load_dotenv(BASE_DIR / ".env")

# 页面运行时填写的 LLM 配置只保存在本进程内存（不写 .env、不返回给前端明文）。
# 服务重启后回到 .env 的配置。
_LLM_ENV_KEYS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "AGENT_LLM_MODE")
_STARTUP_ENV = {key: os.getenv(key) for key in _LLM_ENV_KEYS}
_RUNTIME_CONFIG: dict[str, str] | None = None
_CONFIG_LOCK = threading.Lock()

app = FastAPI(title="LangGraph 研究助手", version="0.2.0")
GRAPH = build_graph()
try:
    _startup_llm = get_llm()
    print(
        f"[研究助手] LLM 模式={_startup_llm.mode} | 模型={_startup_llm.model} | "
        f"Base={_startup_llm.base_url or 'OpenAI 默认'}；也可在网页右上角「模型设置」直接填写 Key（仅存内存，重启失效）",
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


class LLMConfigRequest(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""


def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def _current_llm_config() -> dict[str, str]:
    """取当前生效配置：运行时 > 启动时 .env > 空。"""
    if _RUNTIME_CONFIG:
        return dict(_RUNTIME_CONFIG)
    return {
        "api_key": (os.getenv("OPENAI_API_KEY") or "").strip(),
        "base_url": (os.getenv("OPENAI_BASE_URL") or "").strip(),
        "model": (os.getenv("OPENAI_MODEL") or "").strip(),
    }


def _public_llm_config() -> dict[str, Any]:
    """返回给前端的配置视图：永远不含明文 Key。"""
    config = _current_llm_config()
    llm = get_llm()
    source = "runtime" if _RUNTIME_CONFIG else ("env" if config["api_key"] else "none")
    return {
        "configured": bool(config["api_key"]),
        "mode": llm.mode,
        "model": llm.model,
        "base_url": llm.base_url or "",
        "masked_key": _mask_key(config["api_key"]),
        "source": source,
    }


def _validate_llm_config(req: LLMConfigRequest) -> tuple[str, str, str]:
    """校验并合并配置；api_key 留空表示沿用当前已保存的 Key。"""
    current = _current_llm_config()
    key = (req.api_key or "").strip()
    if key and ("\n" in key or "\r" in key or len(key) > 300):
        raise HTTPException(status_code=400, detail="API Key 格式不合法")
    key = key or current["api_key"]
    if not key:
        raise HTTPException(status_code=400, detail="请填写 API Key（当前没有可用配置）")

    base = (req.base_url or "").strip().rstrip("/")
    if not base and not (req.api_key or "").strip():
        base = current["base_url"]
    if base:
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Base URL 必须是 http(s):// 开头的完整地址")

    model = (req.model or "").strip() or current["model"] or "gpt-4o-mini"
    if len(model) > 120 or any(char.isspace() for char in model):
        raise HTTPException(status_code=400, detail="模型名不能包含空格，且长度不能超过 120")
    return key, base, model


def _apply_llm_config(key: str, base: str, model: str) -> None:
    """保存到进程内存，并让后续节点使用新客户端（不写 .env）。"""
    global _RUNTIME_CONFIG
    llm = LLMClient(api_key=key, base_url=base or None, model=model, mode="openai")
    with _CONFIG_LOCK:
        llm_client.set_llm(llm)
        _RUNTIME_CONFIG = {"api_key": key, "base_url": base, "model": model}
        os.environ["OPENAI_API_KEY"] = key
        os.environ["OPENAI_MODEL"] = model
        os.environ["AGENT_LLM_MODE"] = "openai"
        if base:
            os.environ["OPENAI_BASE_URL"] = base
        else:
            os.environ.pop("OPENAI_BASE_URL", None)


def _clear_runtime_config() -> None:
    """清除运行时配置，恢复为启动时 .env 的配置（重启效果）。"""
    global _RUNTIME_CONFIG
    with _CONFIG_LOCK:
        _RUNTIME_CONFIG = None
        for key, value in _STARTUP_ENV.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        llm_client.reset_llm()


def _safe_llm_error(exc: Exception, key: str) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if key:
        text = text.replace(key, "***")
    return text[:500]


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
def health() -> dict[str, Any]:
    llm = get_llm()
    return {
        "status": "ok",
        "llm_mode": llm.mode,
        "model": llm.model,
        "configured": bool(llm.api_key),
    }


@app.get("/api/config")
def get_llm_config() -> dict[str, Any]:
    """返回当前 LLM 配置视图（masked_key，永远不含明文 Key）。"""
    return _public_llm_config()


@app.post("/api/config")
def set_llm_config(req: LLMConfigRequest) -> dict[str, Any]:
    """把页面填写的配置保存到进程内存；不写 .env，重启失效。"""
    key, base, model = _validate_llm_config(req)
    _apply_llm_config(key, base, model)
    return _public_llm_config()


@app.post("/api/config/test")
def test_llm_config(req: LLMConfigRequest) -> dict[str, Any]:
    """用页面填写的配置做一次最小真实调用；成功不代表配置已保存。"""
    key = ""
    try:
        key, base, model = _validate_llm_config(req)
        client = LLMClient(api_key=key, base_url=base or None, model=model, mode="openai")
        result = client.ping()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成前端可读的 400
        raise HTTPException(status_code=400, detail="连接失败：" + _safe_llm_error(exc, key)) from exc
    return {**result, "masked_key": _mask_key(key), "base_url": base}


@app.delete("/api/config")
def clear_llm_config() -> dict[str, Any]:
    """清除运行时配置，回到启动时 .env 的配置。"""
    _clear_runtime_config()
    return _public_llm_config()


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
