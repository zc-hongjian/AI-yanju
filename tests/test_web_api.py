"""Web API 测试：三次人工中断 → 完成；写操作只在确认后产生。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import web


def test_web_index_served():
    """主界面在 /index.html（根路径 / 是一键启用页，见 test_launch_page.py）。"""
    client = TestClient(web.app)
    resp = client.get("/index.html")
    assert resp.status_code == 200
    assert "LangGraph 研究助手" in resp.text
    assert 'id="progress-card"' in resp.text


def test_web_api_rejects_empty_question():
    client = TestClient(web.app)
    resp = client.post("/api/research", json={"question": "   "})
    assert resp.status_code == 400


def test_web_api_full_flow_three_interrupts(tmp_path, monkeypatch, fake_web):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DEFAULT_EMAIL_TO", "web@example.com")
    client = TestClient(web.app)

    resp = client.post("/api/research", json={"question": "LangGraph 如何实现人工中断？"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "interrupt"
    assert data["interrupt"]["type"] == "draft_review"
    thread_id = data["thread_id"]
    assert not outbox.exists(), "草稿确认前不得有任何写操作"

    resp = client.post(
        "/api/resume",
        json={"thread_id": thread_id, "value": {"decision": "approve", "comment": ""}},
    )
    data = resp.json()
    assert data["interrupt"]["type"] == "email_confirmation"
    assert data["interrupt"]["email"]["to"] == "web@example.com"
    assert not outbox.exists(), "邮件确认前不得写文件"

    resp = client.post(
        "/api/resume",
        json={"thread_id": thread_id, "value": {"send": True, "email_payload": data["interrupt"]["email"]}},
    )
    data = resp.json()
    assert data["interrupt"]["type"] == "notion_confirmation"
    assert not outbox.exists(), "Notion 确认前不得写文件"

    resp = client.post(
        "/api/resume",
        json={"thread_id": thread_id, "value": {"save": False}},
    )
    data = resp.json()
    assert data["status"] == "completed"
    final = data["final_output"]
    assert final["status"] == "completed"
    assert [action["action"] for action in final["actions"]] == ["send_email"]
    assert (outbox / "emails").exists()
    assert not (outbox / "notion").exists()
    assert final["log_summary"]["total"] == len(data["logs"])


def test_web_stream_pushes_logs_before_result(tmp_path, monkeypatch, fake_web):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    client = TestClient(web.app)

    with client.stream(
        "POST",
        "/api/research/stream",
        json={"question": "LangGraph 如何实现人工中断？"},
    ) as resp:
        assert resp.status_code == 200
        lines = [line for line in resp.iter_lines() if line.strip()]

    events = [json.loads(line) for line in lines]
    types = [event["type"] for event in events]
    assert "log" in types, "流式接口必须先把日志推给前端"
    assert types[0] == "start", "流一开始要先把 thread_id 推给前端（中断时才知道标哪个会话）"
    assert events[0]["thread_id"]
    assert types[-1] == "result"
    logs = [event["entry"] for event in events if event["type"] == "log"]
    assert any(entry["node_name"] == "search_web" for entry in logs)
    assert all("step_id" in entry for entry in logs)
    data = events[-1]["data"]
    assert data["status"] == "interrupt"
    assert data["interrupt"]["type"] == "draft_review"


def test_web_stream_resume_requires_known_session():
    client = TestClient(web.app)
    resp = client.post(
        "/api/resume/stream",
        json={"thread_id": "not-exist", "value": {"send": False}},
    )
    assert resp.status_code == 400
