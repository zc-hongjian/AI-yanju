"""Prompt 护栏：知识库防污染、对比报告指令、篇幅/重复/表格校验。"""

from __future__ import annotations

import graph
import prompts
import reporting
from state import new_state


def test_comparison_detection_and_instructions():
    assert prompts.is_comparison_question("Chroma vs Milvus 有什么区别？")
    assert prompts.is_comparison_question("两个向量库对比哪个更适合我们")
    assert not prompts.is_comparison_question("LangGraph 如何实现人工中断？")
    block = prompts.comparison_instructions("Chroma vs Milvus 区别")
    for phrase in ("强制表格", "严禁身份混淆", "冲突消解", "拒绝废话"):
        assert phrase in block
    assert prompts.comparison_instructions("LangGraph 如何实现人工中断？") == ""


def test_comparison_instructions_are_injected_only_for_comparison():
    evidence = [{"claim": "c", "source_url": "https://e.test/a", "quote": "q", "confidence": 0.5}]
    sources = [{"url": "https://e.test/a", "title": "A"}]
    compare_user = prompts.draft_user("Chroma vs Milvus 区别", evidence, sources)
    normal_user = prompts.draft_user("LangGraph 如何实现人工中断", evidence, sources)
    assert "对比报告专属指令" in compare_user
    assert "对比报告专属指令" not in normal_user
    outline_compare = prompts.outline_user("Chroma vs Milvus 区别", ["q"], evidence)
    assert "对比报告专属指令" in outline_compare


def test_system_prompts_carry_length_and_kb_scope_rules():
    assert "≤1000 字" in prompts.DRAFT_SYSTEM or "1000 字" in prompts.DRAFT_SYSTEM
    assert "最多出现两次" in prompts.DRAFT_SYSTEM
    for prompt in (
        prompts.DRAFT_SYSTEM,
        prompts.OUTLINE_SYSTEM,
        prompts.DRAFT_SECTION_SYSTEM,
        prompts.DRAFT_SUMMARY_SYSTEM,
        prompts.DRAFT_ANALYSIS_SYSTEM,
        prompts.REWRITE_SYSTEM,
    ):
        assert "本地知识库使用规则" in prompt
        assert "不能当作外部产品" in prompt


def test_kb_evidence_is_labeled_as_project_document():
    block = prompts.numbered_evidence_block(
        [{"claim": "本项目分块 800", "source_url": "kb://rag-design.md#1", "quote": "分块 800"}]
    )
    assert "本地项目文档" in block


def _kb_state(question: str, tmp_path) -> dict:
    state = new_state(question, trace_id="kb-scope", log_dir=str(tmp_path / "logs"))
    state["plan"] = {
        "queries": [{"query": question}],
        "sub_questions": ["Chroma 的数据规模上限", "Milvus 的数据规模上限"],
    }
    return state


def _fake_kb(monkeypatch):
    hit = {
        "url": "kb://rag-design.md#1",
        "title": "研究助手的混合检索（RAG）设计",
        "heading": "分块策略",
        "text": "本项目分块 800 字符、重叠 120，使用 BM25 + 向量 RRF。",
        "score": 0.9,
        "retriever": "bm25",
        "doc_id": "rag-design.md",
        "chunk_id": 1,
    }
    monkeypatch.setattr(graph.rag, "search_knowledge", lambda queries, k=6: [dict(hit)])
    monkeypatch.setattr(
        graph.rag,
        "knowledge_stats",
        lambda: {"documents": 1, "chunks": 2, "vector": False, "embedding_model": "", "dir": "knowledge", "top_k": 6},
    )


def test_external_comparison_skips_local_knowledge_base(monkeypatch, tmp_path):
    _fake_kb(monkeypatch)
    state = _kb_state("Chroma vs Milvus 有什么区别？", tmp_path)
    updates = graph.retrieve_knowledge(state)
    assert updates["kb_hits"] == []
    assert all(not page.get("is_kb") for page in updates["fetched_pages"])


def test_project_context_keeps_knowledge_base_but_marks_scope(monkeypatch, tmp_path):
    _fake_kb(monkeypatch)
    state = _kb_state("我们的项目在 Chroma vs Milvus 上怎么选？分块和重叠怎么配？", tmp_path)
    updates = graph.retrieve_knowledge(state)
    assert updates["kb_hits"], "带项目上下文时允许召回本地文档"
    kb_pages = [page for page in updates["fetched_pages"] if page.get("is_kb")]
    assert kb_pages and kb_pages[0]["source_scope"] == "project"
    evidence = graph._kb_evidence_items(kb_pages)
    assert evidence and evidence[0]["source_scope"] == "project"


def test_validate_draft_requires_comparison_table():
    draft = (
        "## 摘要\n\nChroma 适合小团队[S1]。Milvus 适合大规模[S2]。边界待复核。\n\n"
        "## 关键发现\n\n- Chroma 轻量[S1]\n- Milvus 支持十亿级[S2]\n\n"
        "## 分析与推演\n\n- 【推断】规模不同选择不同[S1]。\n\n"
        "## 局限与未证实\n\n- 未覆盖运维成本。\n"
    )
    evidence = [
        {"claim": "Chroma 轻量", "source_url": "https://e.test/a", "quote": "Chroma 轻量", "confidence": 0.8},
        {"claim": "Milvus 支持十亿级", "source_url": "https://e.test/b", "quote": "Milvus 支持十亿级", "confidence": 0.8},
    ]
    plain = reporting.validate_draft(draft, evidence, question="Chroma vs Milvus 区别", comparison=True)
    assert "missing_comparison_table" in {issue["code"] for issue in plain["issues"]}
    with_table = draft.replace(
        "## 关键发现",
        "| 维度 | Chroma | Milvus |\n| --- | --- | --- |\n| 规模 | 轻量[S1] | 十亿级[S2] |\n\n## 关键发现",
    )
    checked = reporting.validate_draft(with_table, evidence, question="Chroma vs Milvus 区别", comparison=True)
    assert "missing_comparison_table" not in {issue["code"] for issue in checked["issues"]}


def test_validate_draft_flags_length_and_repeated_fact(monkeypatch):
    monkeypatch.setenv("AGENT_REPORT_MAX_CHARS", "200")
    draft = (
        "## 摘要\n\n"
        + "同一个事实被反复复述而且没有任何新信息。\n" * 12
        + "\n## 关键发现\n\n- 事实[S1]\n\n## 分析与推演\n\n- 【推断】事实[S1]\n\n## 局限与未证实\n\n- 未覆盖。\n"
    )
    result = reporting.validate_draft(draft, [], question="某个问题")
    codes = {issue["code"] for issue in result["issues"]}
    assert "report_too_long" in codes
    assert "repeated_fact" in codes


def test_compact_report_keeps_required_sections_and_one_table(monkeypatch):
    monkeypatch.setenv("AGENT_REPORT_MAX_CHARS", "300")
    long_draft = (
        "## 摘要\n\n"
        + "Chroma 适合小团队[S1]。Milvus 适合大规模[S2]。两者定位不同[S1][S2]。多说一句废话。\n\n"
        "## 关键发现\n\n"
        + "".join(f"- 发现{i}：这是同一条事实的重复表述[S1]\n" for i in range(8))
        + "\n## 对比矩阵\n\n| 维度 | Chroma | Milvus |\n| --- | --- | --- |\n| 规模 | 轻量[S1] | 十亿级[S2] |\n| 部署 | 简单[S1] | 较重[S2] |\n"
        "\n## 应用场景\n\n| 维度 | Chroma | Milvus |\n| --- | --- | --- |\n| 规模 | 轻量[S1] | 十亿级[S2] |\n"
        "\n## 分析与推演\n\n"
        + "".join(f"- 【推断】推演{i}：同一条推断反复说[S1][S2]。\n" for i in range(4))
        + "\n## 局限与未证实\n\n- 未覆盖运维成本。\n"
    )
    assert reporting.report_chars(long_draft) > 300
    compacted, fixes = reporting.compact_report(long_draft)
    assert reporting.report_chars(compacted) <= 300
    for title in ("## 摘要", "## 关键发现", "## 对比矩阵", "## 分析与推演", "## 局限与未证实"):
        assert title in compacted
    separators = [line for line in compacted.splitlines() if reporting._is_table_separator(line)]
    assert len(separators) == 1, "重复的对比表格小节应被删除"
    assert any(fix["code"].startswith(("dropped", "deduped", "trimmed")) for fix in fixes)


def test_draft_review_marks_comparison_report_without_table(monkeypatch, tmp_path):
    from graph import draft_report, draft_review, plan_outline

    state = new_state("Chroma vs Milvus 有什么区别？", trace_id="cmp-review", log_dir=str(tmp_path / "logs"))
    state["plan"] = {"sub_questions": ["Chroma 规模", "Milvus 规模"]}
    state["evidence"] = [
        {"claim": "Chroma 轻量", "source_url": "https://e.test/a", "quote": "Chroma 轻量", "confidence": 0.8},
        {"claim": "Milvus 支持十亿级", "source_url": "https://e.test/b", "quote": "Milvus 支持十亿级", "confidence": 0.8},
    ]
    state["fetched_pages"] = [
        {"url": "https://e.test/a", "title": "A", "content": "Chroma 轻量"},
        {"url": "https://e.test/b", "title": "B", "content": "Milvus 支持十亿级"},
    ]
    state["coverage_summary"] = {"total": 2, "covered": 2, "rate": 1.0, "uncovered": []}
    state.update(plan_outline(state))
    state.update(draft_report(state))
    updates = draft_review(state)
    review = updates["draft_review"]
    assert review["checks"]["comparison"] is True
    assert any(issue["code"] == "missing_comparison_table" for issue in review["issues"])
