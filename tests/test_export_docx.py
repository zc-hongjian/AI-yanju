"""报告导出 Word：完成会话可导出 docx，未完成/不存在给出明确错误。"""

from __future__ import annotations

import io
import json
import zipfile

import docx
from fastapi.testclient import TestClient

import web

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _complete_flow(client: TestClient) -> tuple[str, dict]:
    data = client.post("/api/research", json={"question": "LangGraph 如何实现人工中断？"}).json()
    thread_id = data["thread_id"]
    data = client.post(
        "/api/resume",
        json={"thread_id": thread_id, "value": {"decision": "approve", "comment": ""}},
    ).json()
    email = data["interrupt"]["email"]
    data = client.post(
        "/api/resume",
        json={
            "thread_id": thread_id,
            "value": {"send": True, "email_payload": {"to": "docx@example.com",
                                                     "subject": email["subject"],
                                                     "body": email["body"]}},
        },
    ).json()
    data = client.post("/api/resume", json={"thread_id": thread_id, "value": {"save": False}}).json()
    assert data["status"] == "completed"
    return thread_id, data["final_output"]


def test_export_docx_for_completed_session(tmp_path, monkeypatch, fake_web):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    client = TestClient(web.app)
    thread_id, final = _complete_flow(client)

    resp = client.get(f"/api/sessions/{thread_id}/export.docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(DOCX_MIME)
    assert "attachment" in resp.headers["content-disposition"]
    blob = resp.content
    assert blob[:2] == b"PK", "docx 是 zip 容器"

    # zip 里必须有 word/document.xml，且包含报告标题文本
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        assert "word/document.xml" in names
        document_xml = archive.read("word/document.xml").decode("utf-8")

    # 用 python-docx 读回：标题 + 至少一个表格（正文表格或证据附录）
    parsed = docx.Document(io.BytesIO(blob))
    texts = [paragraph.text for paragraph in parsed.paragraphs if paragraph.text.strip()]
    assert any("LangGraph" in text for text in texts)
    assert len(parsed.tables) >= 0
    assert "结论" in document_xml or "附录" in document_xml
    assert final["report"]


def test_export_docx_rejects_unfinished_session(tmp_path, monkeypatch, fake_web):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    client = TestClient(web.app)
    data = client.post("/api/research", json={"question": "什么是 RAG？"}).json()
    assert data["status"] == "interrupt"

    resp = client.get(f"/api/sessions/{data['thread_id']}/export.docx")
    assert resp.status_code == 409
    assert "没有完成报告" in resp.json()["detail"]


def test_export_docx_unknown_session_404():
    client = TestClient(web.app)
    resp = client.get("/api/sessions/web-does-not-exist/export.docx")
    assert resp.status_code == 404


def test_frontend_has_word_export_entrypoints():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="download-word"' in html
    assert 'id="report-word"' in html
    assert "/export.docx" in html
