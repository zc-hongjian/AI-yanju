"""P0 防幻觉/防污染：数字 grounding、表格引用、知识库召回门、来源过滤、硬字数上限。"""

from __future__ import annotations

import graph
import reporting
import tools
from state import new_state

EVIDENCE = [
    {
        "claim": "IDC预测2030年中国网络安全AI Agent市场规模593.5亿元",
        "quote": "IDC预测2030年中国网络安全AI Agent市场规模593.5亿元。",
        "source_url": "https://finance.sina.com.cn/stock/estate/x.shtml",
        "confidence": 0.8,
    }
]
QUESTION = "2027 年中国 AI Agent 市场规模有多大？"


def _draft(summary: str, findings: str, table: str = "", analysis: str = "") -> str:
    return (
        f"## 摘要\n\n{summary}\n\n"
        f"## 关键发现\n\n{findings}\n\n"
        f"{table}"
        f"## 分析与推演\n\n{analysis or '- 【推断】市场规模会增长[S1]。'}\n\n"
        "## 局限与未证实\n\n- 未覆盖。\n"
    )


def test_unsupported_number_is_flagged_and_scrubbed():
    draft = _draft(
        summary="2027年中国AI Agent市场规模预计超过500亿元[S1]。",
        findings="- IDC预测2030年中国网络安全AI Agent市场规模593.5亿元[S1]",
    )
    issues = reporting.number_issues(draft, EVIDENCE, QUESTION)
    assert any(issue["code"] == "number_not_in_evidence" and "500亿" in issue["detail"] for issue in issues)
    cleaned, fixes = reporting.scrub_unsupported_numbers(draft, EVIDENCE, QUESTION)
    assert "500亿" not in cleaned
    assert "593.5亿元" in cleaned
    assert any(fix["code"] == "dropped_unsupported_number" for fix in fixes)


def test_supported_number_is_kept():
    draft = _draft(
        summary="IDC 预测 2030 年中国网络安全 AI Agent 市场规模 593.5 亿元[S1]。",
        findings="- 市场规模 593.5 亿元[S1]",
    )
    assert reporting.unsupported_numbers(draft, EVIDENCE, QUESTION) == []


def test_year_number_mismatch_is_flagged():
    draft = _draft(
        summary="2027年中国AI Agent市场规模预计达到593.5亿元[S1]。",
        findings="- IDC预测2030年中国网络安全AI Agent市场规模593.5亿元[S1]",
    )
    issues = reporting.number_issues(draft, EVIDENCE, QUESTION)
    assert any(issue["code"] == "year_number_mismatch" for issue in issues)
    cleaned, fixes = reporting.scrub_year_number_mismatches(draft, EVIDENCE)
    assert "2030年中国AI Agent市场规模预计达到593.5亿元" in cleaned
    assert any(fix["code"] == "fixed_year_mismatch" for fix in fixes)


def test_unsupported_year_is_flagged():
    draft = _draft(
        summary="2026年中国AI Agent市场规模达到593.5亿元[S1]。",
        findings="- 市场规模 593.5 亿元[S1]",
    )
    issues = reporting.number_issues(draft, EVIDENCE, QUESTION)
    assert any(issue["code"] == "year_not_in_evidence" for issue in issues)


def test_table_rows_without_citation_are_dropped():
    table = (
        "## 对比矩阵\n\n| 维度 | A | B |\n| --- | --- | --- |\n"
        "| 规模 | 大[S1] | 小 |\n"
        "| 成本 | 低 | 高 |\n\n"
    )
    draft = _draft("摘要[S1]。", "- 发现[S1]", table=table)
    cleaned, fixes = reporting.scrub_uncited_table_rows(draft)
    assert "| 成本 | 低 | 高 |" not in cleaned
    assert "| 规模 | 大[S1] | 小 |" in cleaned
    assert any(fix["code"] == "dropped_uncited_table_row" for fix in fixes)
    result = reporting.validate_draft(
        _draft("摘要[S1]。", "- 发现[S1]", table=table), EVIDENCE, question=QUESTION
    )
    assert any(issue["code"] == "table_row_without_citation" for issue in result["issues"])


def test_external_question_only_uses_title_matched_knowledge(monkeypatch, tmp_path):
    hit = {
        "url": "kb://rag-design.md#1",
        "title": "研究助手的混合检索（RAG）设计",
        "heading": "索引缓存",
        "text": "市场规模与 AI Agent 的关系……",
        "score": 0.9,
        "retriever": "bm25",
        "doc_id": "rag-design.md",
        "chunk_id": 1,
    }
    monkeypatch.setattr(graph.rag, "search_knowledge", lambda queries, k=6: [dict(hit)])
    monkeypatch.setattr(
        graph.rag,
        "knowledge_stats",
        lambda: {"documents": 1, "chunks": 1, "vector": False, "embedding_model": "", "dir": "knowledge", "top_k": 6},
    )
    state = new_state("2027 年中国 AI Agent 市场规模有多大？", trace_id="kb-ext", log_dir=str(tmp_path / "logs"))
    state["plan"] = {"queries": [{"query": "AI Agent 市场规模"}], "sub_questions": ["市场规模"]}
    updates = graph.retrieve_knowledge(state)
    assert updates["kb_hits"] == [], "外部市场问题不能召回项目文档（正文词面命中也不行）"

    matching = dict(hit, title="AI Agent 市场规模", heading="市场规模")
    monkeypatch.setattr(graph.rag, "search_knowledge", lambda queries, k=6: [matching])
    updates = graph.retrieve_knowledge(state)
    assert updates["kb_hits"], "标题/小节命中问题词时允许保留"


def test_dictionary_and_translation_sources_are_junk():
    for url in (
        "https://www.iciba.com/word?w=controversy",
        "https://dictionary.cambridge.org/zhs/controversy",
        "https://fanyi.youdao.com/?text=x",
        "https://translate.google.com/?sl=en",
        "https://www.zhihu.com/search?q=ai",
        "https://www.so.com/s?q=ai",
    ):
        assert graph._is_junk_result_url(url), url
    assert not graph._is_junk_result_url("https://www.ibm.com/think/topics/artificial-intelligence")


def test_compact_report_honors_hard_limit(monkeypatch):
    monkeypatch.setenv("AGENT_REPORT_MAX_CHARS", "400")
    long_draft = (
        "## 摘要\n\n" + "市场规模持续增长，竞争格局尚未稳定[S1]。" * 8 + "\n\n"
        "## 关键发现\n\n" + "".join(f"- 发现{i}：市场规模在扩大[S1]\n" for i in range(12)) + "\n"
        "## 对比矩阵\n\n| 维度 | A | B |\n| --- | --- | --- |\n"
        + "".join(f"| 指标{i} | 值{i}[S1] | 值{i} |\n" for i in range(8))
        + "\n## 分析与推演\n\n" + "".join(f"- 【推断】增长会持续[S1]。\n" for _ in range(6))
        + "\n## 局限与未证实\n\n" + "".join(f"- 未覆盖项{i}。\n" for i in range(6))
    )
    assert reporting.report_chars(long_draft) > 400
    compacted, fixes = reporting.compact_report(long_draft)
    assert reporting.report_chars(compacted) <= 400, (reporting.report_chars(compacted), fixes)
    assert "## 摘要" in compacted and "## 局限与未证实" in compacted
    assert fixes, "压缩必须留下可审计的 fixes"
