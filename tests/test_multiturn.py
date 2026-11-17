"""多轮追问：后端已支持 parent_thread_id，本组测试保证它被落库/回显，且前端有入口。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import web

INDEX = Path(__file__).resolve().parent.parent / "index.html"


def test_research_records_parent_thread_id(tmp_path, monkeypatch, fake_web):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    client = TestClient(web.app)

    resp = client.post(
        "/api/research",
        json={"question": "LangGraph 如何实现人工中断？", "parent_thread_id": "web-parent01"},
    )
    assert resp.status_code == 200
    data = resp.json()
    thread_id = data["thread_id"]
    assert data["status"] == "interrupt"

    listing = client.get("/api/sessions").json()["sessions"]
    row = next(item for item in listing if item["thread_id"] == thread_id)
    assert row["parent_thread_id"] == "web-parent01", "历史列表要能展示追问链"

    detail = client.get(f"/api/sessions/{thread_id}").json()
    assert detail["parent_thread_id"] == "web-parent01"


def test_research_without_parent_keeps_empty(tmp_path, monkeypatch, fake_web):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    client = TestClient(web.app)

    data = client.post("/api/research", json={"question": "什么是 RAG？"}).json()
    detail = client.get(f"/api/sessions/{data['thread_id']}").json()
    assert detail["parent_thread_id"] == ""


def test_unknown_parent_degrades_to_single_round(tmp_path, monkeypatch, fake_web):
    """父会话不存在时不能报错：只是退化成单轮（历史上下文为空）。"""
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    client = TestClient(web.app)

    resp = client.post(
        "/api/research",
        json={"question": "LangGraph 的中断怎么恢复？", "parent_thread_id": "web-does-not-exist"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in {"interrupt", "completed"}


def test_frontend_has_follow_up_entrypoints():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="follow-up"' in html, "终稿卡要有「继续追问」"
    assert 'id="follow-hint"' in html, "追问模式要在输入框上方提示基于哪一轮"
    assert 'id="follow-clear"' in html, "要能退出追问模式"
    assert 'id="report-follow"' in html, "报告页也要能继续追问"
    assert "parent_thread_id" in html, "开始研究时要把它发给后端"
    assert "followOnLoad" in html and 'urlParams.get("follow")' in html, "要支持 /app?follow=<tid> 深链"
    assert "追问自" in html, "历史列表要展示追问链"
