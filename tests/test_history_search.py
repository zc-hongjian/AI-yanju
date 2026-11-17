"""历史会话检索：按关键词（问题 / 会话号）与状态过滤。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import persistence
import web

INDEX = Path(__file__).resolve().parent.parent / "index.html"


def test_store_filters_by_keyword(tmp_path):
    store = persistence.SessionStore(tmp_path / "sessions.sqlite")
    store.upsert("web-a1", trace_id="t1", question="LangGraph 的人工中断怎么实现？", status="completed")
    store.upsert("web-b2", trace_id="t2", question="RAG 的召回策略有哪些？", status="completed")
    store.upsert("web-c3", trace_id="t3", question="BM25 和向量检索的区别", status="failed")

    assert [row["thread_id"] for row in store.list(query="人工中断")] == ["web-a1"]
    assert [row["thread_id"] for row in store.list(query="RAG")] == ["web-b2"]
    assert [row["thread_id"] for row in store.list(query="web-c3")] == ["web-c3"], "也应能按会话号搜索"
    assert store.list(query="不存在的关键词") == []


def test_store_filters_by_status(tmp_path):
    store = persistence.SessionStore(tmp_path / "sessions.sqlite")
    store.upsert("web-a1", trace_id="t1", question="问题一", status="completed")
    store.upsert("web-b2", trace_id="t2", question="问题二", status="failed")
    store.upsert("web-c3", trace_id="t3", question="问题三", status="completed")

    completed = store.list(status="completed")
    assert {row["thread_id"] for row in completed} == {"web-a1", "web-c3"}
    assert [row["thread_id"] for row in store.list(status="failed")] == ["web-b2"]
    assert len(store.list()) == 3, "不传过滤条件时行为不变"


def test_api_passes_q_and_status():
    store = web._STORE
    assert store is not None
    store.upsert("web-filter-1", trace_id="tf1", question="唯一关键词：量子纠错", status="completed")
    store.upsert("web-filter-2", trace_id="tf2", question="另一个问题", status="failed")
    try:
        client = TestClient(web.app)
        hit = client.get("/api/sessions", params={"q": "量子纠错"}).json()
        assert [row["thread_id"] for row in hit["sessions"]] == ["web-filter-1"]
        assert hit["filters"]["q"] == "量子纠错"

        failed = client.get("/api/sessions", params={"q": "过滤测试", "status": "failed"}).json()
        assert failed["sessions"] == []
        status_only = client.get("/api/sessions", params={"status": "failed"}).json()
        assert all(row["status"] == "failed" for row in status_only["sessions"])
    finally:
        store.delete("web-filter-1")
        store.delete("web-filter-2")


def test_frontend_has_history_filters():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="history-search"' in html
    assert 'id="history-status"' in html
    assert "historyStatusFilter" in html and "historySearch" in html
