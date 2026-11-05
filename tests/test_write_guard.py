"""写操作门禁测试：没有 human_approved=True 时必须拒绝，且绝不产生副作用。"""

from __future__ import annotations

from pathlib import Path

import pytest

import tools


def test_send_email_without_approval_raises(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))

    with pytest.raises(tools.WriteActionNotApproved):
        tools.send_email("boss@example.com", "主题", "正文")

    assert not outbox.exists(), "未确认时不应写入任何文件"


def test_send_email_via_call_tool_without_approval_is_rejected(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))

    result = tools.call_tool(
        "send_email",
        {"to": "boss@example.com", "subject": "主题", "body": "正文"},
    )

    assert result.ok is False
    assert result.rejected is True
    assert result.attempts == 0
    assert "人工确认" in (result.error or "")
    assert not outbox.exists()


def test_save_to_notion_without_approval_raises(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))

    with pytest.raises(tools.WriteActionNotApproved):
        tools.save_to_notion("标题", "内容", ["tag"])

    result = tools.call_tool("save_to_notion", {"title": "标题", "content": "内容"})
    assert result.rejected is True
    assert not outbox.exists()


def test_caller_cannot_smuggle_human_approved_into_args(tmp_path, monkeypatch):
    """即使把 human_approved 塞进工具参数，call_tool 也会剥离后按显式入参判断。"""
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))

    result = tools.call_tool(
        "send_email",
        {
            "to": "boss@example.com",
            "subject": "主题",
            "body": "正文",
            "human_approved": True,
        },
    )

    assert result.rejected is True
    assert not outbox.exists()


def test_approved_send_email_uses_dry_run_outbox(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    monkeypatch.delenv("SMTP_HOST", raising=False)

    result = tools.call_tool(
        "send_email",
        {"to": "boss@example.com", "subject": "研究报告", "body": "正文"},
        human_approved=True,
    )

    assert result.ok is True
    assert result.data["mode"] == "dry_run"
    path = Path(result.data["path"])
    assert path.exists()
    assert "boss@example.com" in path.read_text(encoding="utf-8")


def test_approved_save_to_notion_uses_dry_run_outbox(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    result = tools.call_tool(
        "save_to_notion",
        {"title": "研究报告", "content": "# 正文", "tags": ["research"]},
        human_approved=True,
    )

    assert result.ok is True
    assert result.data["mode"] == "dry_run"
    assert Path(result.data["path"]).exists()


def test_invalid_recipient_fails_validation_after_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    result = tools.call_tool(
        "send_email",
        {"to": "not-an-email", "subject": "s", "body": "b"},
        human_approved=True,
    )
    assert result.ok is False
    assert result.attempts == 0
    assert "格式不合法" in (result.error or "")


def test_tool_schemas_cover_the_five_required_tools():
    assert set(tools.tool_names()) == {
        "search_web",
        "fetch_page",
        "summarize_sources",
        "send_email",
        "save_to_notion",
    }
    for schema in tools.TOOL_SCHEMAS:
        function = schema["function"]
        assert function["description"], "每个工具必须有描述"
        assert function["parameters"]["type"] == "object"
        assert "required" in function["parameters"]
    assert tools.WRITE_TOOLS == {"send_email", "save_to_notion"}
