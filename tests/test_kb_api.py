"""知识库（RAG）管理接口：文档列表 / 统计 / 重建索引。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import rag
import web

INDEX = Path(__file__).resolve().parent.parent / "index.html"


@pytest.fixture()
def kb_tmp(tmp_path, monkeypatch):
    root = tmp_path / "kb"
    root.mkdir()
    (root / "alpha.md").write_text("# Alpha\n\nLangGraph 用状态机编排节点。\n", encoding="utf-8")
    (root / "beta.txt").write_text("BM25 是词频检索算法。\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_KB_DIR", str(root))
    monkeypatch.setenv("AGENT_KB_INDEX", str(tmp_path / "kb_index.json"))
    rag.reset_knowledge_base()
    yield root
    rag.reset_knowledge_base()


def test_kb_stats_and_documents(kb_tmp):
    client = TestClient(web.app)
    data = client.get("/api/kb").json()

    stats = data["stats"]
    assert stats["documents"] == 2
    assert stats["chunks"] >= 2
    assert stats["top_k"] >= 1
    assert stats["dir"].endswith("kb")

    paths = {doc["path"] for doc in data["documents"]}
    assert paths == {"alpha.md", "beta.txt"}
    alpha = next(doc for doc in data["documents"] if doc["path"] == "alpha.md")
    assert alpha["chunks"] >= 1
    assert alpha["size"] > 0
    assert alpha["mtime"] > 0
    assert alpha["title"]


def test_kb_reindex_removes_and_rebuilds(kb_tmp):
    client = TestClient(web.app)
    first = client.get("/api/kb").json()
    index_path = Path(first["index"]["path"])
    assert index_path.exists(), "首次查询会构建索引缓存"

    second = client.post("/api/kb/reindex").json()
    assert second["ok"] is True
    assert second["removed_index"] is True
    assert second["stats"]["documents"] == 2
    assert index_path.exists(), "重建后索引文件应重新生成"
    assert second["index"]["exists"] is True


def test_kb_empty_dir_is_ok(tmp_path, monkeypatch):
    empty = tmp_path / "empty-kb"
    empty.mkdir()
    monkeypatch.setenv("AGENT_KB_DIR", str(empty))
    monkeypatch.setenv("AGENT_KB_INDEX", str(tmp_path / "empty-index.json"))
    rag.reset_knowledge_base()
    try:
        client = TestClient(web.app)
        data = client.get("/api/kb").json()
        assert data["stats"]["documents"] == 0
        assert data["stats"]["chunks"] == 0
        assert data["documents"] == []
        reindexed = client.post("/api/kb/reindex").json()
        assert reindexed["stats"]["documents"] == 0
    finally:
        rag.reset_knowledge_base()


def test_frontend_has_kb_panel():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="kb-panel"' in html
    assert 'id="kb-docs"' in html
    assert 'id="kb-stats"' in html
    assert 'id="kb-reindex"' in html
    assert "/api/kb/reindex" in html
