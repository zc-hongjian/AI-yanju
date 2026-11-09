"""P0-3 混合检索 RAG 测试：分块、BM25 召回、离线降级、图级接入与引用。"""

from __future__ import annotations

from pathlib import Path

import pytest

import graph
import rag
from state import new_state

DOC_A = """# 断点恢复

## checkpointer

LangGraph 用 checkpointer 持久化每一步状态。编译图时没有挂 checkpointer，interrupt 会直接报错。

## resume

恢复时被中断的节点会从头重新执行，interrupt 返回 resume 传入的值。
"""

DOC_B = """# 评测指标

## 覆盖率

子问题证据覆盖率 51.2%，主要卡在对比题只抓到一方。

## 支持率

证据断言支持率 99.4%，是规则代理，不是语义蕴含。
"""


def _make_kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "hitl.md").write_text(DOC_A, encoding="utf-8")
    (root / "metrics.md").write_text(DOC_B, encoding="utf-8")
    return root


def _activate_kb(monkeypatch, tmp_path: Path) -> Path:
    root = _make_kb(tmp_path)
    monkeypatch.setenv("AGENT_KB_DIR", str(root))
    monkeypatch.setenv("AGENT_KB_INDEX", str(tmp_path / "kb_index.json"))
    rag.reset_knowledge_base()
    return root


def test_split_sections_and_chunk_overlap(tmp_path):
    root = _make_kb(tmp_path)
    chunks = rag.load_chunks(root)
    assert {chunk.doc_id for chunk in chunks} == {"hitl.md", "metrics.md"}
    assert all(chunk.url.startswith("kb://") for chunk in chunks)
    assert any(chunk.heading == "checkpointer" for chunk in chunks)

    long_text = "第一句话。" * 400
    pieces = rag.chunk_section(long_text, size=200, overlap=40)
    assert len(pieces) > 2
    assert all(len(piece) <= 220 for piece in pieces)


def test_bm25_retrieves_relevant_chunk(tmp_path, monkeypatch):
    _activate_kb(monkeypatch, tmp_path)
    hits = rag.search_knowledge(["checkpointer interrupt 报错"], k=3)
    assert hits
    assert hits[0]["doc_id"] == "hitl.md"
    assert hits[0]["retriever"] == "bm25"


def test_knowledge_stats_without_embedding(tmp_path, monkeypatch):
    _activate_kb(monkeypatch, tmp_path)
    stats = rag.knowledge_stats()
    assert stats["documents"] == 2
    assert stats["chunks"] >= 2
    assert stats["vector"] is False


def test_retrieve_node_appends_kb_pages(tmp_path, monkeypatch):
    _activate_kb(monkeypatch, tmp_path)
    state = new_state("checkpointer 断点恢复原理", trace_id="kb-1", log_dir=str(tmp_path / "logs"))
    state["plan"] = {"queries": [{"query": "checkpointer 断点恢复"}]}
    state["fetched_pages"] = [
        {"url": "https://example.org/web", "title": "网页", "content": "网页正文"}
    ]
    out = graph.retrieve_knowledge(state)
    pages = out["fetched_pages"]
    assert len(pages) >= 2
    assert pages[0]["url"] == "https://example.org/web"
    added = [page for page in pages if page.get("is_kb")]
    assert out["kb_hits"]

    logs = [entry for entry in out["logs"] if entry["node_name"] == "retrieve_knowledge"]
    assert logs and logs[0]["status"] == "ok"


def test_retrieve_node_skips_when_empty(tmp_path, monkeypatch):
    empty = tmp_path / "empty-kb"
    empty.mkdir()
    monkeypatch.setenv("AGENT_KB_DIR", str(empty))
    monkeypatch.setenv("AGENT_KB_INDEX", str(tmp_path / "idx.json"))
    rag.reset_knowledge_base()
    state = new_state("问题", trace_id="kb-2", log_dir=str(tmp_path / "logs"))
    state["plan"] = {"queries": [{"query": "问题"}]}
    state["fetched_pages"] = []
    out = graph.retrieve_knowledge(state)
    assert out["fetched_pages"] == []
    assert out["kb_hits"] == []
    assert out["logs"][0]["status"] == "skipped"


def test_kb_sources_survive_sanitize():
    sources = [
        {"url": "https://example.org/a", "title": "网页 A"},
        {"url": "kb://hitl.md#2", "title": "hitl.md", "is_kb": True},
    ]
    evidence = [
        {"claim": "checkpointer 持久化状态", "source_url": "kb://hitl.md#2", "quote": "持久化"},
        {"claim": "网页结论", "source_url": "https://example.org/a", "quote": "正文"},
    ]
    draft = "## 摘要\n\n断点恢复依赖 checkpointer[S1]，网页补充[S2]。"
    cleaned, dropped = graph._sanitize_report(draft, sources, evidence)
    assert dropped == []
    assert "📚 知识库 · hitl.md（[S1]）" in cleaned
    assert "kb://hitl.md#2" not in cleaned  # 知识库 URL 不写进正文
    assert "[S1](kb://" not in cleaned  # 知识库编号不做成可点击链接
    assert "[S2](https://example.org/a)" in cleaned


def test_graph_flow_includes_kb_source(tmp_path, monkeypatch, fake_web):
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    _activate_kb(monkeypatch, tmp_path)
    g = graph.build_graph()
    result = g.invoke(
        new_state("checkpointer 如何支持断点恢复？", trace_id="kb-3", log_dir=str(tmp_path / "logs")),
        {"configurable": {"thread_id": "kb-flow"}},
    )
    assert result["__interrupt__"][0].value["type"] == "draft_review"
    pages = result["fetched_pages"]
    assert any(page.get("is_kb") for page in pages)
    payload = result["__interrupt__"][0].value
    assert any(source.get("is_kb") for source in payload["sources"])
    assert "📚 知识库" in payload["draft_report"]
