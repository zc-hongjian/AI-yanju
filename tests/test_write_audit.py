"""写操作审计 + 幂等：同一次运行里重复执行不会重复发邮件/重复写 Notion。"""

from __future__ import annotations

import pytest

import graph
import persistence
import tools
from state import new_state


@pytest.fixture()
def audit_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WRITE_AUDIT_DB", str(tmp_path / "write_audit.sqlite"))
    monkeypatch.setenv("AGENT_WRITE_IDEMPOTENT", "1")
    persistence.reset_write_audit_store()
    yield tmp_path
    persistence.reset_write_audit_store()


def _email_state(trace_id: str = "trace-audit-1") -> dict:
    st = new_state("LangGraph 如何实现人工中断？", trace_id=trace_id, log_dir=".tmp/pytest-logs")
    st["approved_actions"] = ["send_email"]
    st["email_payload"] = {"to": "a@example.com", "subject": "研究结论", "body": "正文"}
    return st


def _count_calls(monkeypatch, *, ok: bool = True) -> dict:
    stats = {"calls": 0}

    def fake_call(name, payload, human_approved=False):
        assert human_approved is True, "写操作必须经过人工确认门禁"
        stats["calls"] += 1
        return tools.ToolResult(
            tool=name,
            ok=ok,
            data={"mode": "dry-run", "path": f"outbox/{stats['calls']}.eml"},
            error=None if ok else "SMTP 失败",
            attempts=1,
            latency_ms=5,
        )

    monkeypatch.setattr(tools, "call_tool", fake_call)
    return stats


def test_store_roundtrip(audit_env):
    store = persistence.get_write_audit_store()
    assert store is not None
    payload = {"to": "a@example.com", "subject": "s", "body": "b"}
    assert store.lookup("run-1", "send_email", payload) is None
    store.record("run-1", "send_email", payload, ok=True, mode="dry-run", detail={"path": "x.eml"})
    hit = store.lookup("run-1", "send_email", payload)
    assert hit and hit["ok"] is True and hit["mode"] == "dry-run"
    assert store.lookup("run-2", "send_email", payload) is None, "不同运行之间不共享幂等键"
    assert len(store.entries("run-1")) == 1


def test_execute_actions_deduplicates_same_payload(audit_env, monkeypatch):
    stats = _count_calls(monkeypatch)
    state = _email_state()

    first = graph.execute_actions(state)
    second = graph.execute_actions(state)

    assert stats["calls"] == 1, "同样参数的写操作只应真正执行一次"
    assert first["action_results"][0]["ok"] is True
    assert second["action_results"][0]["deduplicated"] is True
    assert any(entry["status"] == "skipped" and "跳过" in entry["output_summary"]
               for entry in second["logs"])


def test_changed_payload_is_not_deduplicated(audit_env, monkeypatch):
    stats = _count_calls(monkeypatch)
    state = _email_state()
    graph.execute_actions(state)

    state["email_payload"] = {"to": "b@example.com", "subject": "研究结论", "body": "正文"}
    graph.execute_actions(state)
    assert stats["calls"] == 2, "收件人改了就是新的写操作"


def test_failed_action_can_retry(audit_env, monkeypatch):
    stats = {"calls": 0}

    def fake_call(name, payload, human_approved=False):
        stats["calls"] += 1
        ok = stats["calls"] > 1
        return tools.ToolResult(
            tool=name,
            ok=ok,
            data={"mode": "dry-run"} if ok else None,
            error=None if ok else "SMTP 失败",
            attempts=1,
        )

    monkeypatch.setattr(tools, "call_tool", fake_call)
    state = _email_state()
    failed = graph.execute_actions(state)
    assert failed["action_results"][0]["ok"] is False

    retried = graph.execute_actions(state)
    assert stats["calls"] == 2, "失败的写操作必须允许重试"
    assert retried["action_results"][0]["ok"] is True
    store = persistence.get_write_audit_store()
    assert store.entries("trace-audit-1")[0]["status"] == "ok"


def test_idempotency_can_be_disabled(audit_env, monkeypatch):
    stats = _count_calls(monkeypatch)
    monkeypatch.setenv("AGENT_WRITE_IDEMPOTENT", "0")
    state = _email_state()
    graph.execute_actions(state)
    graph.execute_actions(state)
    assert stats["calls"] == 2


def test_dry_run_outbox_written_once(audit_env, monkeypatch):
    """不 mock 工具：直接落 outbox，验证同一运行重跑只产生一封 .eml。"""
    outbox = audit_env / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    state = _email_state()
    graph.execute_actions(state)
    graph.execute_actions(state)
    files = list((outbox / "emails").glob("*.eml"))
    assert len(files) == 1
