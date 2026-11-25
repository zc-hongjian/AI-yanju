"""P1 问题类型模板：争议/市场识别、指令注入、校验、意图查询、证据结构化。"""

from __future__ import annotations

import graph
import llm_client
import prompts
import reporting

CONTROVERSY_Q = "AI Agent 会取代 SaaS 吗？2027 年这个赛道的争议点与趋势是什么？"
MARKET_Q = "2027 年中国 AI Agent 市场规模有多大？主要玩家和增长驱动因素是什么？"


def test_question_type_detection():
    assert prompts.is_controversy_question(CONTROVERSY_Q)
    assert not prompts.is_market_question(CONTROVERSY_Q), "争议/趋势问题不能被误判为市场分析"
    assert prompts.is_market_question(MARKET_Q)
    assert not prompts.is_controversy_question(MARKET_Q)


def test_template_instructions_are_injected():
    evidence = [{"claim": "c", "source_url": "https://e.test/a", "quote": "q", "confidence": 0.5}]
    sources = [{"url": "https://e.test/a", "title": "A"}]
    controversy_user = prompts.draft_user(CONTROVERSY_Q, evidence, sources)
    assert "争议/趋势报告专属指令" in controversy_user
    assert "支持方观点" in controversy_user and "反对方观点" in controversy_user
    market_user = prompts.outline_user(MARKET_Q, ["市场规模"], evidence)
    assert "市场分析报告专属指令" in market_user
    assert "年份" in market_user and "口径" in market_user


def test_validate_controversy_requires_opposing_views():
    draft = (
        "## 摘要\n\nAI Agent 可能取代部分 SaaS[S1]。\n\n"
        "## 关键发现\n\n- 观点一：AI Agent 更灵活[S1]\n\n"
        "## 分析与推演\n\n- 【推断】取决于场景[S1]。\n\n"
        "## 局限与未证实\n\n- 未覆盖。\n"
    )
    evidence = [{"claim": "AI Agent 更灵活", "source_url": "https://e.test/a", "quote": "AI Agent 更灵活", "confidence": 0.8}]
    result = reporting.validate_draft(draft, evidence, question=CONTROVERSY_Q, controversy=True, market=False)
    assert any(issue["code"] == "missing_opposing_view" for issue in result["issues"])

    balanced = (
        "## 摘要\n\nAI Agent 可能取代部分 SaaS[S1]。\n\n"
        "## 关键发现\n\n- 支持方：AI Agent 更灵活[S1]\n- 反对方：SaaS 更成熟稳定[S1]\n\n"
        "## 分析与推演\n\n- 【推断】取决于场景[S1]。\n\n"
        "## 局限与未证实\n\n- 未覆盖。\n"
    )
    checked = reporting.validate_draft(balanced, evidence, question=CONTROVERSY_Q, controversy=True, market=False)
    assert not any(issue["code"] == "missing_opposing_view" for issue in checked["issues"])


def test_validate_market_requires_data_table():
    draft = (
        "## 摘要\n\n市场规模增长[S1]。\n\n"
        "## 关键发现\n\n- 市场在增长[S1]\n\n"
        "## 分析与推演\n\n- 【推断】会继续增长[S1]。\n\n"
        "## 局限与未证实\n\n- 未覆盖。\n"
    )
    evidence = [{"claim": "市场增长", "source_url": "https://e.test/a", "quote": "市场增长", "confidence": 0.8}]
    result = reporting.validate_draft(draft, evidence, question=MARKET_Q, market=True)
    assert any(issue["code"] == "missing_market_table" for issue in result["issues"])

    with_table = draft.replace(
        "## 关键发现",
        "| 指标 | 数值 | 年份 | 口径 | 来源 |\n| --- | --- | --- | --- | --- |\n"
        "| 市场规模 | 593.5亿元 | 2030 | 中国网络安全 | [S1] |\n\n## 关键发现",
    )
    checked = reporting.validate_draft(with_table, evidence, question=MARKET_Q, market=True)
    codes = {issue["code"] for issue in checked["issues"]}
    assert "missing_market_table" not in codes
    assert "market_table_missing_year" not in codes
    assert "market_table_missing_scope" not in codes


def test_uncited_market_claims_are_scrubbed():
    draft = (
        "## 摘要\n\n市场规模增长[S1]。\n\n"
        "## 主要玩家与竞争格局\n\n2027年中国AI Agent市场的主要玩家包括百度、阿里巴巴、腾讯和华为。\n"
        "腾讯云也提供 Agent 平台[S1]。\n\n"
        "## 增长驱动因素\n\n技术进步推动增长[S1]。\n\n"
        "## 局限与未证实\n\n- 未覆盖。\n"
    )
    cleaned, fixes = reporting.scrub_uncited_claims(draft, market=True)
    assert "百度、阿里巴巴" not in cleaned, "无引用的玩家断言应被删除"
    assert "腾讯云也提供 Agent 平台[S1]" in cleaned, "有引用的断言要保留"
    assert any(fix["code"] == "dropped_uncited_claim" for fix in fixes)


def test_market_table_is_generated_from_evidence():
    evidence = [
        {
            "claim": "IDC预测2030年中国网络安全AI Agent市场规模593.5亿元",
            "quote": "IDC预测2030年中国网络安全AI Agent市场规模593.5亿元。",
            "source_url": "https://finance.sina.com.cn/a",
            "confidence": 0.8,
        }
    ]
    draft = (
        "## 摘要\n\n市场规模增长[S1]。\n\n"
        "## 市场规模与数据\n\n公开证据不足，本节不作结论。\n\n"
        "## 分析与推演\n\n- 【推断】增长[S1]。\n\n## 局限与未证实\n\n- 未覆盖。\n"
    )
    fixed, fixes = reporting.ensure_market_table(draft, evidence)
    assert "| 指标 | 数值 | 年份 | 口径/说明 | 来源 |" in fixed
    assert "593.5亿" in fixed and "[S1]" in fixed
    assert "| 2030 |" in fixed, "表格年份要取证据原文的 2030，不能被问题里的 2027 覆盖"
    assert fixed.count("## 市场规模与数据") == 1, "不应生成重复小节"
    assert any(fix["code"] == "inserted_market_table" for fix in fixes)
    result = reporting.validate_draft(fixed, evidence, question=MARKET_Q, market=True)
    assert "missing_market_table" not in {issue["code"] for issue in result["issues"]}


def test_controversy_balance_adds_honest_gap_note():
    draft = (
        "## 摘要\n\n存在争议[S1]。\n\n"
        "## 关键发现\n\n- 支持方：AI Agent 更灵活[S1]\n\n"
        "## 分析与推演\n\n- 【推断】取决于场景[S1]。\n\n"
        "## 局限与未证实\n\n- 未覆盖。\n"
    )
    fixed, fixes = reporting.ensure_controversy_balance(draft)
    assert "未找到另一方" in fixed and "反对方" in fixed
    assert any(fix["code"] == "inserted_controversy_gap" for fix in fixes)
    result = reporting.validate_draft(fixed, [], question=CONTROVERSY_Q, controversy=True)
    assert not any(issue["code"] == "missing_opposing_view" for issue in result["issues"])


def test_market_table_without_year_is_flagged():
    draft = (
        "## 摘要\n\n市场规模增长[S1]。\n\n"
        "## 关键发现\n\n| 指标 | 数值 | 来源 |\n| --- | --- | --- |\n| 规模 | 593.5亿元 | [S1] |\n\n"
        "## 分析与推演\n\n- 【推断】增长[S1]。\n\n## 局限与未证实\n\n- 未覆盖。\n"
    )
    result = reporting.validate_draft(draft, [], question=MARKET_Q, market=True)
    codes = {issue["code"] for issue in result["issues"]}
    assert "missing_market_table" not in codes
    assert "market_table_missing_year" in codes


def test_intent_queries_added_and_chinese_preferred():
    ensured = graph._ensure_intent_queries(
        CONTROVERSY_Q,
        [{"query": "AI Agent vs SaaS 2027"}, {"query": "controversies AI Agent SaaS 2027"}],
    )
    texts = [item["query"] for item in ensured]
    assert len(texts) <= graph.MAX_QUERIES
    assert any(any("\u4e00" <= ch <= "\u9fff" for ch in text) for text in texts), "中文问题至少一条中文查询"
    assert any("争议" in text or "趋势" in text for text in texts), "争议类问题应补观点/趋势意图词"

    market = graph._ensure_intent_queries(MARKET_Q, [{"query": "2027 中国 AI Agent 市场"}])
    market_texts = [item["query"] for item in market]
    assert any("市场规模" in text or "份额" in text for text in market_texts)


def test_filter_evidence_preserves_stance_and_numbers():
    sources = [
        {
            "url": "https://e.test/a",
            "title": "A",
            "content": "IDC预测2030年中国网络安全AI Agent市场规模593.5亿元。",
        }
    ]
    raw = [
        {
            "claim": "IDC预测2030年中国网络安全AI Agent市场规模593.5亿元",
            "quote": "IDC预测2030年中国网络安全AI Agent市场规模593.5亿元。",
            "source_url": "https://e.test/a",
            "stance": "中立",
            "numbers": ["593.5亿元|2030|中国网络安全"],
            "confidence": 0.9,
        }
    ]
    items, dropped = llm_client.filter_evidence(sources, raw)
    assert dropped == 0 and items
    assert items[0]["stance"] == "中立"
    assert items[0]["numbers"] == ["593.5亿元|2030|中国网络安全"]


def test_source_type_classification():
    assert graph._source_type("https://www.idc.com/report") == "research"
    assert graph._source_type("https://finance.sina.com.cn/a") == "media"
    assert graph._source_type("https://www.python.org/doc") == "official"
    assert graph._source_type("kb://rag-design.md#1") == "project"
