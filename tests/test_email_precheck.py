"""写操作门禁：收件人格式错误应在人工确认点就拦住，而不是执行时才失败。"""

from __future__ import annotations

import graph
from state import new_state


def _decisions(*items):
    iterator = iter(items)
    return lambda _payload: next(iterator)


def test_invalid_email_is_rejected_at_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        graph,
        "interrupt",
        _decisions({"send": True, "email_payload": {"to": "not-an-email"}}, {"save": False}),
    )
    state = new_state("测试问题", trace_id="email-bad", log_dir=str(tmp_path / "logs"))
    updates = graph.human_confirm_actions(state)
    assert "send_email" not in updates["approved_actions"]
    assert updates["email_payload"] is None
    assert any(err["type"] == "invalid_email_to" for err in updates["errors"])


def test_valid_email_still_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(
        graph,
        "interrupt",
        _decisions({"send": True, "email_payload": {"to": "research@example.com"}}, {"save": False}),
    )
    state = new_state("测试问题", trace_id="email-ok", log_dir=str(tmp_path / "logs"))
    updates = graph.human_confirm_actions(state)
    assert "send_email" in updates["approved_actions"]
    assert updates["email_payload"]["to"] == "research@example.com"
    assert not any(err["type"] == "invalid_email_to" for err in updates["errors"])
