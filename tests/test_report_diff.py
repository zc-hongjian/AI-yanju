"""多轮报告 diff：纯函数统计 + 接口语义 + 前端入口。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import web

INDEX = Path(__file__).resolve().parent.parent / "index.html"

OLD = """# 研究报告：问题

## 摘要

旧结论一段。

## 关键发现

- 旧发现一 [S1](https://a.test)
- 旧发现二

## 来源清单

- [A](https://a.test)
"""

NEW = """# 研究报告：问题

## 摘要

新结论一段，更长。

## 关键发现

- 旧发现一 [S1](https://a.test)
- 新发现三

## 来源清单

- [B](https://b.test)
"""


def test_diff_reports_counts_added_and_removed():
    stats = web._diff_reports(OLD, NEW)
    assert stats["added"] >= 1
    assert stats["removed"] >= 1
    assert any(line["op"] == "+" and "新结论" in line["text"] for line in stats["lines"])
    assert any(line["op"] == "-" and "旧结论" in line["text"] for line in stats["lines"])
    assert not any("来源清单" in line["text"] for line in stats["lines"]), "来源列表不参与对比"


def test_diff_reports_truncates():
    old = "\n".join(f"行 {index}" for index in range(300))
    new = "\n".join(f"改 {index}" for index in range(300))
    stats = web._diff_reports(old, new, limit=10)
    assert len(stats["lines"]) == 10
    assert stats["truncated"] is True


def test_diff_endpoint_with_parent(monkeypatch):
    def fake_get_session(thread_id):
        if thread_id == "web-child":
            return {
                "thread_id": "web-child", "parent_thread_id": "web-parent",
                "question": "追问：那局限呢？", "final_output": {"report": NEW},
            }
        if thread_id == "web-parent":
            return {
                "thread_id": "web-parent", "parent_thread_id": "",
                "question": "原问题", "final_output": {"report": OLD},
            }
        raise web.HTTPException(status_code=404, detail="会话不存在")

    monkeypatch.setattr(web, "get_session", fake_get_session)
    client = TestClient(web.app)
    data = client.get("/api/sessions/web-child/diff").json()
    assert data["available"] is True
    assert data["base_thread_id"] == "web-parent"
    assert data["added"] >= 1 and data["removed"] >= 1


def test_diff_endpoint_without_parent(monkeypatch):
    monkeypatch.setattr(web, "get_session", lambda tid: {
        "thread_id": tid, "parent_thread_id": "", "question": "q",
        "final_output": {"report": OLD},
    })
    client = TestClient(web.app)
    data = client.get("/api/sessions/web-solo/diff").json()
    assert data["available"] is False
    assert data["reason"] == "no_parent"


def test_diff_endpoint_parent_deleted(monkeypatch):
    def fake_get_session(thread_id):
        if thread_id == "web-child2":
            return {"thread_id": thread_id, "parent_thread_id": "web-gone",
                    "question": "q", "final_output": {"report": NEW}}
        raise web.HTTPException(status_code=404, detail="会话不存在")

    monkeypatch.setattr(web, "get_session", fake_get_session)
    client = TestClient(web.app)
    data = client.get("/api/sessions/web-child2/diff").json()
    assert data["available"] is False
    assert data["reason"] == "parent_not_found"


def test_frontend_has_report_diff():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="report-diff"' in html
    assert 'id="report-diff-panel"' in html
    assert "/diff" in html
