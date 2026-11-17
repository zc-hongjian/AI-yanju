"""知识库相关性门槛：2 字中文词只在标题/小节名里认（防既有回归）。

背景：门槛最初把「≥3 字中文」当唯一判据，导致「知识库分块的大小和重叠是多少？」
这类问题命中「引用怎么处理 / 降级路径」，却把真正讲 800/120 的「分块策略」过滤掉
（`evaluation/kb_check.py` 的 kb6 回归）。修复后：2 字词只在标题/小节名匹配，
既不漏掉「分块」，也不让「多少 / 的是」把无关片段放进来。
"""

from __future__ import annotations

import pytest

import graph
import rag
from state import new_state


@pytest.fixture()
def kb_dir(tmp_path, monkeypatch):
    root = tmp_path / "kb"
    root.mkdir()
    (root / "rag-design.md").write_text(
        "# 研究助手的混合检索（RAG）设计\n\n"
        "## 分块策略\n\n"
        "按 Markdown 标题切段，再按 800 字滑窗、120 字重叠；每块保留 heading 路径。\n"
        "重叠用于避免定义句被切断。\n\n"
        "## 引用怎么处理\n\n"
        "知识库片段被转成 kb:// 伪 URL，复用现有证据抽取与引用编号。\n\n"
        "## 降级路径\n\n"
        "没有 embedding Key 时纯 BM25，检索仍可用。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_KB_DIR", str(root))
    monkeypatch.setenv("AGENT_KB_INDEX", str(tmp_path / "kb_index.json"))
    rag.reset_knowledge_base()
    yield root
    rag.reset_knowledge_base()


def _state(question: str, trace_id: str) -> dict:
    state = new_state(question, trace_id=trace_id, log_dir=".tmp/pytest-logs")
    state["research_question"] = question
    state["plan"] = {"queries": [{"query": question}], "sub_questions": []}
    return state


def test_two_char_heading_term_keeps_chunk(kb_dir):
    result = graph.retrieve_knowledge(_state("知识库分块的大小和重叠是多少？", "kb-gate-1"))
    pages = [page for page in result["fetched_pages"] if page.get("is_kb")]
    headings = {str(page.get("title") or "") for page in pages}
    assert pages, "应当命中知识库片段"
    assert any("分块" in heading for heading in headings), f"分块策略片段被误过滤：{headings}"


def test_unrelated_question_still_filters_project_docs(kb_dir):
    result = graph.retrieve_knowledge(_state("LangGraph 如何实现人工中断？", "kb-gate-2"))
    pages = [page for page in result["fetched_pages"] if page.get("is_kb")]
    assert pages == [], f"与问题无词面交集的片段不应进入证据链：{pages}"
