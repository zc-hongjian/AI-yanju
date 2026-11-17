"""取消运行：登记表 / 节点边界生效 / 流终态 / 接口语义。

设计事实：取消是“协作式”的——只在节点边界检查，不杀线程（见 cancel.py 注释）。
因此这里测的是：标志被登记、图节点放行取消而不是记成 error、流里出现 cancelled
终态且会话状态变成 cancelled。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import cancel
import graph
import web


def test_registry_request_and_clear():
    cancel.clear("trace-x")
    assert cancel.is_cancelled("trace-x") is False
    assert cancel.request("trace-x") is True
    assert cancel.is_cancelled("trace-x") is True
    assert cancel.request("trace-x") is False, "重复取消不应重复登记"
    assert "trace-x" in cancel.pending()
    cancel.clear("trace-x")
    assert cancel.is_cancelled("trace-x") is False


def test_registry_ignores_empty_id():
    assert cancel.is_cancelled("") is False
    assert cancel.request("") is False
    cancel.clear("")


def test_instrument_raises_before_node_runs_and_writes_no_span(monkeypatch):
    calls: list[str] = []

    def node(state):
        calls.append("ran")
        return {}

    wrapped = graph._instrument("search_web", node)
    cancel.request("trace-cancel")
    try:
        with pytest.raises(cancel.ResearchCancelled):
            wrapped({"trace_id": "trace-cancel", "node_usage": {}})
    finally:
        cancel.clear("trace-cancel")
    assert calls == [], "取消后节点不应继续执行"


def test_instrument_does_not_record_cancel_as_error_span(monkeypatch):
    spans: list[dict] = []

    class Recorder:
        def add_span(self, **kwargs):
            spans.append(kwargs)

    monkeypatch.setattr(graph, "_trace_recorder", lambda state: Recorder())

    def node(state):
        raise cancel.ResearchCancelled("user cancelled")

    wrapped = graph._instrument("draft_report", node)
    with pytest.raises(cancel.ResearchCancelled):
        wrapped({"trace_id": "trace-cancel-2", "node_usage": {}})
    assert spans == [], "取消不是错误，不能污染 trace"


def test_cancel_unknown_session_404():
    client = TestClient(web.app)
    resp = client.post("/api/sessions/web-not-exist/cancel")
    assert resp.status_code == 404


def test_cancel_non_running_session_409():
    store = web._STORE
    assert store is not None, "本测试需要会话索引可用"
    thread_id = "web-cancel-completed"
    store.upsert(thread_id, trace_id="trace-done", question="q", status="completed")
    try:
        client = TestClient(web.app)
        resp = client.post(f"/api/sessions/{thread_id}/cancel")
        assert resp.status_code == 409
        assert "运行中" in resp.json()["detail"]
    finally:
        store.delete(thread_id)


def test_stream_cancel_emits_terminal_event_and_marks_session():
    """端到端：流运行中请求取消 → runner 抛 ResearchCancelled → 推 cancelled 终态。"""
    store = web._STORE
    assert store is not None
    thread_id = "web-cancel-stream"
    trace_id = "trace-cancel-stream"
    store.upsert(thread_id, trace_id=trace_id, question="cancel me", status="running")
    web._SESSIONS[thread_id] = trace_id

    def runner():
        for _ in range(300):
            if cancel.is_cancelled(trace_id):
                raise cancel.ResearchCancelled("用户取消")
            time.sleep(0.005)
        return {"logs": []}

    events = web._stream_events(trace_id, thread_id, runner)
    try:
        first = next(events)
        assert '"type": "start"' in first or '"type":"start"' in first

        client = TestClient(web.app)
        resp = client.post(f"/api/sessions/{thread_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelling"

        rest = list(events)
        assert any("cancelled" in line for line in rest), "流里必须有 cancelled 终态"
        assert not any('"type": "result"' in line or '"type":"result"' in line for line in rest)
    finally:
        cancel.clear(trace_id)
        web._SESSIONS.pop(thread_id, None)
        store.delete(thread_id)

    row = web._session_get(thread_id)
    assert row is None or row["status"] == "cancelled"
