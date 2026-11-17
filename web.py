"""Web 版入口：FastAPI + 单页 HTML，复用同一张 LangGraph 图与同一套工具/人工确认逻辑。

启动：
    python web.py
    # 或
    uvicorn web:app --host 127.0.0.1 --port 8000

如果当前解释器缺少依赖（常见原因：在根目录 uv 环境里运行了本文件），会优先自动切换到
项目自带的 .venv；找不到时给出可直接复制的安装命令。

API：
    POST /api/research          {"question": "...", "parent_thread_id": "可选"}  -> 第一次中断或最终结果
    POST /api/resume            {"thread_id": "...", "value": {...}} -> 下一次中断或最终结果
    POST /api/research/stream   同上，但用 NDJSON 流式返回实时日志 + 最终结果
    POST /api/resume/stream     同上
    GET  /api/sessions          历史会话列表（默认 20 条）
    GET  /api/sessions/{id}     加载单个会话（含中断载荷，可用于“继续审批”）
    DELETE /api/sessions/{id}   删除单个会话（索引 + 该 thread 的 checkpoint/writes）
    POST /api/sessions/{id}/interrupt  前端发现流中断时把 running 会话标成 interrupted
    GET  /api/health

会话状态存放在 SQLite checkpointer（``data/checkpoints.sqlite``）里，按 thread_id 隔离；
进程重启后从同一 thread_id 继续 ``Command(resume=...)`` 即可恢复人工审批中断。
会话索引（问题、状态、更新时间）单独存 ``data/sessions.sqlite``，供历史列表查询。
服务启动时会把上一个进程遗留的 running 会话标记为 interrupted（进程都换了，不可能还在跑）。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
LAUNCH_FILE = BASE_DIR / "launch.html"
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

    import cancel
    from graph import build_graph
    from llm_client import LLMClient, get_llm
    import llm_client
    from logging_utils import register_log_sink, unregister_log_sink
    from persistence import (
        SessionStore,
        build_checkpointer,
        checkpoint_db_path,
        delete_thread_data,
    )
    from state import new_state
    import rag
    import tracing
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

app = FastAPI(title="LangGraph 研究助手", version="0.3.0")
# 持久化：SQLite checkpointer（服务重启后仍能从人工审批中断处恢复）
CHECKPOINTER = build_checkpointer()
GRAPH = build_graph(CHECKPOINTER)
try:
    _STORE: SessionStore | None = SessionStore()
except Exception as exc:  # noqa: BLE001 - 索引库失败不应拖垮研究主流程
    _STORE = None
    print(f"[研究助手] 会话索引不可用（单次研究不受影响）：{exc}", flush=True)
if _STORE is not None:
    # 上个进程被杀/崩溃时，未完成的会话会一直停留在 running；启动时统一标成 interrupted，
    # 历史列表才不会把“已经没人在跑的会话”显示成“进行中”。
    try:
        _stale = _STORE.mark_running_as_interrupted()
        if _stale:
            print(f"[研究助手] 启动清理：{_stale} 个遗留 running 会话已标记为 interrupted", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[研究助手] 遗留会话状态清理失败：{exc}", flush=True)
try:
    _startup_llm = get_llm()
    print(
        f"[研究助手] LLM 模式={_startup_llm.mode} | 模型={_startup_llm.model} | "
        f"Base={_startup_llm.base_url or 'OpenAI 默认'}；也可在网页右上角「模型设置」直接填写 Key（仅存内存，重启失效）",
        flush=True,
    )
except Exception as exc:  # noqa: BLE001 - 启动日志失败不影响服务
    print(f"[研究助手] LLM 配置读取失败：{exc}", flush=True)
# thread_id -> trace_id 的进程内缓存；重启后从 _STORE 回源，流式 resume 仍可用
_SESSIONS: dict[str, str] = {}


class ResearchRequest(BaseModel):
    question: str
    # 多轮追问：可选，指向上一轮的 thread_id；会把其结论摘要注入本轮规划
    parent_thread_id: str = ""


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


def _session_upsert(
    thread_id: str,
    trace_id: str,
    question: str,
    status: str,
    parent_thread_id: str = "",
) -> None:
    """写会话索引；索引失败只影响“历史列表”，不能让研究流程失败。"""
    if _STORE is None:
        return
    try:
        _STORE.upsert(
            thread_id,
            trace_id=trace_id,
            question=question,
            status=status,
            parent_thread_id=parent_thread_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[研究助手] 会话索引写入失败：{exc}", flush=True)


def _session_set_status(thread_id: str, status: str) -> None:
    if _STORE is None:
        return
    try:
        _STORE.set_status(thread_id, status)
    except Exception as exc:  # noqa: BLE001
        print(f"[研究助手] 会话状态更新失败：{exc}", flush=True)


def _session_get(thread_id: str) -> dict[str, Any] | None:
    if _STORE is None:
        return None
    try:
        return _STORE.get(thread_id)
    except Exception:  # noqa: BLE001
        return None


def _session_list(limit: int = 20) -> list[dict[str, Any]]:
    if _STORE is None:
        return []
    try:
        return _STORE.list(limit)
    except Exception:  # noqa: BLE001
        return []


def _trace_for(thread_id: str) -> str:
    """取 trace_id：进程内缓存优先，重启后回源会话索引。"""
    trace = _SESSIONS.get(thread_id)
    if trace:
        return trace
    if _STORE is None:
        return ""
    try:
        return _STORE.trace_id(thread_id)
    except Exception:  # noqa: BLE001
        return ""


def _snapshot_interrupt(snapshot: Any) -> Any:
    """从 checkpointer 的 StateSnapshot 里取出等待中的人工确认载荷。"""
    for task in getattr(snapshot, "tasks", ()) or ():
        for interrupt_obj in getattr(task, "interrupts", ()) or ():
            return getattr(interrupt_obj, "value", None)
    return None


def _history_context(parent_thread_id: str) -> str:
    """把父会话的结论压成一段短上下文，供本轮规划“避免重复检索”。"""
    parent = (parent_thread_id or "").strip()
    if not parent:
        return ""
    try:
        snapshot = GRAPH.get_state({"configurable": {"thread_id": parent}})
    except Exception:  # noqa: BLE001 - 父会话不存在时静默降级为单轮
        return ""
    values = dict(getattr(snapshot, "values", {}) or {})
    final = values.get("final_output") or {}
    question = str(final.get("research_question") or values.get("research_question") or "").strip()
    report = str(final.get("report") or values.get("draft_report") or "").strip()
    sub_questions = list(final.get("sub_questions") or (values.get("plan") or {}).get("sub_questions") or [])
    parts: list[str] = []
    if question:
        parts.append(f"上一轮问题：{question[:160]}")
    if sub_questions:
        parts.append("已覆盖子问题：" + "；".join(str(item)[:60] for item in sub_questions[:5]))
    if report:
        cut = report.find("## 来源")
        body = report[:cut] if cut > 0 else report
        parts.append("上一轮结论摘要：" + re.sub(r"\s+", " ", body).strip()[:600])
    return "\n".join(parts).strip()


def _pack(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """把图的一次 invoke 结果包装成前端需要的形状，并同步会话状态。"""
    interrupts = result.get("__interrupt__") or []
    payload = interrupts[0].value if interrupts else None
    status = "interrupt" if payload is not None else "completed"
    _session_set_status(thread_id, status)
    return {
        "thread_id": thread_id,
        "status": status,
        "interrupt": payload,
        "logs": result.get("logs") or [],
        "final_output": result.get("final_output") or None,
    }


def _start_run(question: str, parent_thread_id: str = "") -> tuple[str, str, Any, dict[str, Any]]:
    trace_id = uuid.uuid4().hex[:8]
    thread_id = f"web-{trace_id}"
    parent = (parent_thread_id or "").strip()
    _SESSIONS[thread_id] = trace_id
    state = new_state(
        question,
        trace_id=trace_id,
        history_context=_history_context(parent),
        parent_thread_id=parent,
    )
    _session_upsert(thread_id, trace_id, question, "running", parent)
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
        except cancel.ResearchCancelled:
            # 主动取消：不是失败，也不是“连接中断”；用独立终态告诉前端
            outcome["cancelled"] = True
            _session_set_status(thread_id, "cancelled")
        except Exception as exc:  # noqa: BLE001 - 转成流里的 error 事件
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            _session_set_status(thread_id, "failed")
        finally:
            events.put({"type": "__done__"})

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    completed = False
    # 先把 thread_id 告诉前端：中途断开时前端才知道该给哪个会话标“已中断”
    yield json.dumps(
        {"type": "start", "thread_id": thread_id, "trace_id": trace_id},
        ensure_ascii=False,
    ) + "\n"
    try:
        while True:
            event = events.get()
            if event.get("type") == "__done__":
                break
            yield json.dumps(event, ensure_ascii=False) + "\n"
        completed = True
        if outcome.get("cancelled"):
            yield json.dumps(
                {"type": "cancelled", "thread_id": thread_id},
                ensure_ascii=False,
            ) + "\n"
        elif "error" in outcome:
            yield json.dumps({"type": "error", "message": outcome["error"]}, ensure_ascii=False) + "\n"
        else:
            payload = _pack(thread_id, outcome["result"])
            yield json.dumps({"type": "result", "data": payload}, ensure_ascii=False) + "\n"
    finally:
        unregister_log_sink(trace_id, sink)
        cancel.clear(trace_id)
        if not completed:
            # 浏览器断开 / 进程被中断：会话不能一直显示“进行中”
            _session_set_status(thread_id, "interrupted")


def _file_response(path: Path, missing: str) -> FileResponse:
    if not path.exists():
        raise HTTPException(status_code=500, detail=missing)
    return FileResponse(path)


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """启动后首页 = 一键启用页；主界面在 /index.html（或 /app）。

    之前 / 直接返回 index.html，启动 web.py 后看不到任何变化，
    用户也无从知道「一键启用」页的存在——所以把启动页放到根路径。
    """
    return _file_response(LAUNCH_FILE, "缺少 launch.html")


@app.get("/launch", include_in_schema=False)
def launch() -> FileResponse:
    """一键启用启动页：检查后端/模型状态，一键进入主界面（默认先弹模型设置）。

    页面本身是独立的 launch.html，双击用 file:// 打开时自动降级为「一键进入静态演示」。
    """
    return _file_response(LAUNCH_FILE, "缺少 launch.html")


@app.get("/index.html", include_in_schema=False)
def index() -> FileResponse:
    """主界面（单页前端）。启动页与深链都用这个路径，file:// 下是同一个文件。"""
    return _file_response(INDEX_FILE, "缺少 index.html")


@app.get("/report/{thread_id}", include_in_schema=False)
def report_page(thread_id: str) -> FileResponse:
    """报告阅读页：返回同一个 index.html，前端按路径进入 report 模式。

    页面是只读视图：数据来自 GET /api/sessions/{thread_id} 的 final_output，
    不触发任何图执行，也不会写 outbox。
    """
    del thread_id  # 路由参数只用于前端判断模式，服务端不需要
    return _file_response(INDEX_FILE, "缺少 index.html")


@app.get("/app", include_in_schema=False)
def app_home() -> FileResponse:
    """主界面短链接：/app?settings=1 等价于 /index.html?settings=1。"""
    return index()


@app.get("/api/health")
def health() -> dict[str, Any]:
    llm = get_llm()
    return {
        "status": "ok",
        "version": app.version,
        "persistence": "sqlite",
        "checkpoint_db": str(checkpoint_db_path()),
        "llm_mode": llm.mode,
        "model": llm.model,
        "configured": bool(llm.api_key),
    }


@app.get("/api/sessions")
def list_sessions(limit: int = 20) -> dict[str, Any]:
    """历史会话列表：问题、状态、更新时间；重启后仍可查（索引落 SQLite）。"""
    rows = _session_list(limit)
    return {"sessions": rows, "count": len(rows), "enabled": _STORE is not None}


@app.get("/api/sessions/{thread_id}")
def get_session(thread_id: str) -> dict[str, Any]:
    """加载单个会话：返回与 /api/research 同构的结果，可直接渲染“继续审批”。"""
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="缺少 thread_id")
    row = _session_get(tid)
    snapshot: Any = None
    try:
        snapshot = GRAPH.get_state({"configurable": {"thread_id": tid}})
    except Exception:  # noqa: BLE001 - 会话不存在或 checkpoint 损坏时回退到索引表
        snapshot = None
    values = dict(getattr(snapshot, "values", {}) or {})
    if not values and row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    interrupt_payload = _snapshot_interrupt(snapshot)
    final_output = values.get("final_output") or None
    if interrupt_payload is not None:
        status = "interrupt"
    elif final_output:
        status = "completed"
    else:
        status = str((row or {}).get("status") or "unknown")
    return {
        "thread_id": tid,
        "status": status,
        "interrupt": interrupt_payload,
        "logs": values.get("logs") or [],
        "final_output": final_output,
        "question": str((row or {}).get("question") or values.get("research_question") or ""),
        "trace_id": str((row or {}).get("trace_id") or values.get("trace_id") or ""),
        "parent_thread_id": str(
            (row or {}).get("parent_thread_id") or values.get("parent_thread_id") or ""
        ),
        "created_at": str((row or {}).get("created_at") or ""),
        "updated_at": str((row or {}).get("updated_at") or ""),
        "recoverable": interrupt_payload is not None,
    }


@app.post("/api/sessions/{thread_id}/interrupt")
def mark_session_interrupted(thread_id: str) -> dict[str, Any]:
    """前端发现流中断（后端退出/断网）时调用：把还在 running 的会话标成 interrupted。"""
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="缺少 thread_id")
    row = _session_get(tid)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if str(row.get("status") or "") == "running":
        _session_set_status(tid, "interrupted")
    updated = _session_get(tid) or row
    return {"ok": True, "thread_id": tid, "status": str(updated.get("status") or "")}


@app.post("/api/sessions/{thread_id}/cancel")
def cancel_session(thread_id: str) -> dict[str, Any]:
    """请求取消正在运行的研究。

    只登记取消标志，由 ``graph._instrument`` 在下一个节点边界生效；
    接口立刻返回 ``cancelling``，前端应展示「正在取消…」而不是直接当成已停止。
    """
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="缺少 thread_id")
    row = _session_get(tid)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if str(row.get("status") or "") != "running":
        raise HTTPException(status_code=409, detail="当前没有运行中的任务")
    trace = _trace_for(tid)
    if not trace:
        raise HTTPException(status_code=409, detail="找不到该会话的运行标识，无法取消")
    cancel.request(trace)
    return {"ok": True, "thread_id": tid, "status": "cancelling"}


@app.delete("/api/sessions/{thread_id}")
def delete_session(thread_id: str) -> dict[str, Any]:
    """删除单条历史会话：会话索引 + 该 thread 的 checkpoint/writes。

    logs/ 与 trace 文件不删（保留审计线索）；删除后无法再从历史列表恢复。
    """
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="缺少 thread_id")
    row = _session_get(tid)
    removed: dict[str, int] = {"sessions": 0, "checkpoints": 0, "writes": 0}
    if _STORE is not None:
        try:
            removed["sessions"] = 1 if _STORE.delete(tid) else 0
        except Exception as exc:  # noqa: BLE001 - 删索引失败要汇报，不能假装成功
            raise HTTPException(status_code=500, detail=f"删除会话索引失败：{exc}") from exc
    try:
        removed.update(delete_thread_data(tid))
    except Exception as exc:  # noqa: BLE001 - checkpoint 清理失败不影响索引已删
        print(f"[研究助手] 清理 checkpoint 失败（{tid}）：{exc}", flush=True)
    _SESSIONS.pop(tid, None)
    if row is None and removed["sessions"] == 0:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "thread_id": tid, "removed": removed}


@app.get("/api/trace/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    """节点级调用链：耗时 / token / 成本 / 错误，按 started_at 排序。"""
    data = tracing.read_trace(trace_id)
    if data is None:
        raise HTTPException(status_code=404, detail="trace 不存在（可能未开启或已被清理）")
    return data


@app.get("/api/cost/summary")
def get_cost_summary(days: int = 7) -> dict[str, Any]:
    """按天/按模型聚合 trace 成本（数据来自 logs/*.trace.jsonl）。"""
    return tracing.aggregate_costs(days)


def _kb_payload() -> dict[str, Any]:
    """知识库面板的统一返回：统计 + 文档列表 + 索引文件状态。"""
    # 先让 stats 触发（必要时）索引构建，再统计索引文件状态，否则重建后仍会显示“未缓存”
    stats = {**rag.knowledge_stats(), "top_k": rag.TOP_K}
    index_path = rag.index_path()
    exists = index_path.exists()
    mtime = 0
    if exists:
        try:
            mtime = int(index_path.stat().st_mtime)
        except OSError:
            mtime = 0
    return {
        "ok": True,
        "stats": stats,
        "documents": rag.knowledge_documents(),
        "index": {"path": str(index_path), "exists": exists, "mtime": mtime},
    }


@app.get("/api/kb")
def get_kb() -> dict[str, Any]:
    """知识库（RAG）现状：文档数/分块数/是否启用向量/索引文件。"""
    return _kb_payload()


@app.post("/api/kb/reindex")
def reindex_kb() -> dict[str, Any]:
    """删除索引缓存并重建：改了 knowledge/ 里的文档后用它立即生效。"""
    path = rag.index_path()
    removed = False
    try:
        if path.exists():
            path.unlink()
            removed = True
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除知识库索引失败：{exc}") from exc
    rag.reset_knowledge_base()
    payload = _kb_payload()
    payload["removed_index"] = removed
    return payload


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
    trace_id, thread_id, state, config = _start_run(question, req.parent_thread_id)
    try:
        result = GRAPH.invoke(state, config)
    except cancel.ResearchCancelled as exc:
        _session_set_status(thread_id, "cancelled")
        raise HTTPException(status_code=409, detail="已取消本次研究") from exc
    except Exception as exc:  # noqa: BLE001 - 接口层统一转成 500，前端展示
        _session_set_status(thread_id, "failed")
        raise HTTPException(status_code=500, detail=f"研究流程失败：{exc}") from exc
    finally:
        cancel.clear(trace_id)
    return _pack(thread_id, result)


@app.post("/api/research/stream")
def start_research_stream(req: ResearchRequest) -> StreamingResponse:
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="研究问题不能为空")
    trace_id, thread_id, state, config = _start_run(question, req.parent_thread_id)
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
    run_trace = _trace_for(thread_id)
    try:
        result = GRAPH.invoke(Command(resume=req.value), config)
    except cancel.ResearchCancelled as exc:
        _session_set_status(thread_id, "cancelled")
        raise HTTPException(status_code=409, detail="已取消本次研究") from exc
    except Exception as exc:  # noqa: BLE001
        _session_set_status(thread_id, "failed")
        raise HTTPException(status_code=500, detail=f"恢复执行失败：{exc}") from exc
    finally:
        if run_trace:
            cancel.clear(run_trace)
    return _pack(thread_id, result)


@app.post("/api/resume/stream")
def resume_stream(req: ResumeRequest) -> StreamingResponse:
    thread_id = (req.thread_id or "").strip()
    if not thread_id:
        raise HTTPException(status_code=400, detail="缺少 thread_id")
    # 重启后 _SESSIONS 为空，但会话索引与 SQLite checkpoint 仍在 → 从索引回源
    trace_id = _trace_for(thread_id)
    if not trace_id:
        raise HTTPException(
            status_code=400,
            detail="会话不存在（可能已过期或从未创建）",
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
