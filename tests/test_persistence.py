"""会话持久化测试：SQLite checkpointer 重启恢复、历史会话 API、多轮上下文。

覆盖的验收点（对应 RESUME_ROADMAP.md §6）：
1. 换一个新图实例（模拟服务重启）后，仍能从人工审批中断处 resume；
2. /api/sessions 能列出刚创建的会话，/api/sessions/{id} 能加载中断载荷；
3. 重启后 /api/resume/stream 不再 400（trace_id 从会话索引回源）；
4. parent_thread_id 会把上一轮结论注入本轮 history_context。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from langgraph.types import Command

import web
from graph import build_graph
from persistence import SessionStore, build_checkpointer
from state import new_state


def _isolate(tmp_path, monkeypatch, *, fake_web) -> TestClient:
    """把 web 的图与索引换成测试私有的 SQLite 文件，避免污染其他用例。"""
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("DEFAULT_EMAIL_TO", "web@example.com")
    store = SessionStore(tmp_path / "sessions.sqlite")
    graph = build_graph(build_checkpointer(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setattr(web, "GRAPH", graph)
    monkeypatch.setattr(web, "_STORE", store)
    web._SESSIONS.clear()
    return TestClient(web.app)


def _resume(client: TestClient, thread_id: str, value: dict) -> dict:
    resp = client.post("/api/resume", json={"thread_id": thread_id, "value": value})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_sqlite_checkpoint_resumes_after_graph_rebuild(tmp_path, monkeypatch, fake_web):
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("DEFAULT_EMAIL_TO", "web@example.com")
    db = tmp_path / "checkpoints.sqlite"
    config = {"configurable": {"thread_id": "restart-case"}}

    graph1 = build_graph(build_checkpointer(db))
    result1 = graph1.invoke(new_state("LangGraph 如何实现人工中断？"), config)
    assert result1["__interrupt__"][0].value["type"] == "draft_review"

    # 模拟服务重启：新连接 + 新图实例，只有磁盘上的 DB 是共享的
    graph2 = build_graph(build_checkpointer(db))
    snapshot = graph2.get_state(config)
    assert snapshot.values["draft_report"], "重启后必须能读回草稿"
    assert snapshot.values["research_question"].startswith("LangGraph")

    result2 = graph2.invoke(Command(resume={"decision": "approve", "comment": ""}), config)
    assert result2["__interrupt__"][0].value["type"] == "email_confirmation"


def test_sessions_api_lists_and_loads_interrupt(tmp_path, monkeypatch, fake_web):
    client = _isolate(tmp_path, monkeypatch, fake_web=fake_web)

    created = client.post("/api/research", json={"question": "LangGraph 如何实现人工中断？"}).json()
    thread_id = created["thread_id"]
    assert created["status"] == "interrupt"

    listing = client.get("/api/sessions").json()
    assert listing["enabled"] is True
    row = next(item for item in listing["sessions"] if item["thread_id"] == thread_id)
    assert row["status"] == "interrupt"
    assert row["question"].startswith("LangGraph")

    loaded = client.get(f"/api/sessions/{thread_id}").json()
    assert loaded["status"] == "interrupt"
    assert loaded["interrupt"]["type"] == "draft_review"
    assert loaded["recoverable"] is True
    assert loaded["trace_id"] == created["thread_id"].removeprefix("web-")


def test_resume_stream_survives_restart(tmp_path, monkeypatch, fake_web):
    client = _isolate(tmp_path, monkeypatch, fake_web=fake_web)
    created = client.post("/api/research", json={"question": "LangGraph 如何实现人工中断？"}).json()
    thread_id = created["thread_id"]

    # 进程重启：内存缓存清空、图实例重建（checkpoint 与索引都在磁盘上）
    web._SESSIONS.clear()
    monkeypatch.setattr(web, "GRAPH", build_graph(build_checkpointer(tmp_path / "checkpoints.sqlite")))

    resp = client.post(
        "/api/resume/stream",
        json={"thread_id": thread_id, "value": {"decision": "approve", "comment": ""}},
    )
    assert resp.status_code == 200, resp.text
    events = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert events[-1]["type"] == "result"
    assert events[-1]["data"]["interrupt"]["type"] == "email_confirmation"


def test_follow_up_injects_parent_history(tmp_path, monkeypatch, fake_web):
    client = _isolate(tmp_path, monkeypatch, fake_web=fake_web)
    first = client.post("/api/research", json={"question": "LangGraph 如何实现人工中断？"}).json()
    thread_id = first["thread_id"]

    step = _resume(client, thread_id, {"decision": "approve", "comment": ""})
    assert step["interrupt"]["type"] == "email_confirmation"
    step = _resume(client, thread_id, {"send": False})
    assert step["interrupt"]["type"] == "notion_confirmation"
    step = _resume(client, thread_id, {"save": False})
    assert step["status"] == "completed"
    assert step["final_output"]["report"]

    follow_up = client.post(
        "/api/research",
        json={"question": "那 checkpointer 具体解决什么问题？", "parent_thread_id": thread_id},
    ).json()
    snapshot = web.GRAPH.get_state({"configurable": {"thread_id": follow_up["thread_id"]}})
    assert "上一轮结论摘要" in snapshot.values["history_context"]
    assert snapshot.values["parent_thread_id"] == thread_id

    listing = client.get("/api/sessions").json()
    child_row = next(item for item in listing["sessions"] if item["thread_id"] == follow_up["thread_id"])
    assert child_row["parent_thread_id"] == thread_id


def test_get_unknown_session_returns_404(tmp_path, monkeypatch, fake_web):
    client = _isolate(tmp_path, monkeypatch, fake_web=fake_web)
    resp = client.get("/api/sessions/not-exist")
    assert resp.status_code == 404
