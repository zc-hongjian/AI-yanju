"""检索规划「技术搜索专家」规则测试。"""

from __future__ import annotations

import pytest

from llm_client import (
    LLMClient,
    clean_question,
    extract_sub_questions,
    framework_site,
    is_how_question,
    parse_json_list,
)


def test_clean_question_removes_colloquial_phrases():
    cleaned = clean_question("请问我想知道 LangGraph 怎么弄 人工中断？")
    assert "请问" not in cleaned
    assert "我想知道" not in cleaned
    assert "怎么弄" not in cleaned
    assert "LangGraph" in cleaned


def test_how_question_detection():
    assert is_how_question("LangGraph 如何实现人工中断？") is True
    assert is_how_question("LangGraph 的 interrupt 原理是什么") is True
    assert is_how_question("2025 年 AI Agent 在企业落地的主要挑战是什么？") is False


def test_framework_site_qualifier():
    assert framework_site("LangGraph 如何实现人工中断") == "langchain.com"
    assert framework_site("FastAPI 怎么做鉴权") == "fastapi.tiangolo.com"
    assert framework_site("2025 年 AI Agent 企业落地挑战") is None


def test_offline_plan_applies_how_and_framework_rules():
    plan = LLMClient(mode="offline").plan_research("LangGraph 如何实现人工中断？")
    queries = [item["query"] for item in plan["queries"]]
    assert 2 <= len(queries) <= 3
    assert any("site:langchain.com" in query for query in queries), "框架问题应加官方文档限定词"
    assert any(("教程" in query) or ("tutorial" in query.lower()) for query in queries), (
        "How 问题应加技术词"
    )
    assert plan["provider"] == "offline-heuristic"


def test_offline_plan_for_non_how_question_has_no_site_qualifier():
    plan = LLMClient(mode="offline").plan_research("2025 年 AI Agent 在企业落地的主要挑战是什么？")
    queries = [item["query"] for item in plan["queries"]]
    assert 2 <= len(queries) <= 3
    assert not any("site:" in query for query in queries)
    assert all("我想知道" not in query for query in queries)


def test_parse_json_list_accepts_plain_and_fenced_json():
    assert parse_json_list('["搜索词1", "搜索词2"]') == ["搜索词1", "搜索词2"]
    assert parse_json_list('```json\n["a", "b"]\n```') == ["a", "b"]
    assert parse_json_list('说明：["x"]') == ["x"]


def test_parse_json_list_rejects_non_json():
    with pytest.raises(Exception):
        parse_json_list("这不是 JSON")


def test_offline_plan_includes_three_to_five_sub_questions():
    plan = LLMClient(mode="offline").plan_research("2025 年 AI Agent 在企业落地的主要挑战是什么？")
    sub_questions = plan.get("sub_questions") or []
    assert 3 <= len(sub_questions) <= 5
    assert all(isinstance(item, str) and item.strip() for item in sub_questions)
    assert len(set(sub_questions)) == len(sub_questions), "子问题不能重复"


def test_offline_plan_can_disable_sub_questions_for_ablation(monkeypatch):
    monkeypatch.setenv("AGENT_PLAN_SUB_QUESTIONS", "0")
    plan = LLMClient(mode="offline").plan_research("LangGraph 如何实现人工中断？")
    assert plan.get("sub_questions") == []


def test_extract_sub_questions_parses_model_json():
    text = '{"sub_questions": ["子问题A", "子问题B", "子问题C"]}\n现在调用 search_web。'
    assert extract_sub_questions(text) == ["子问题A", "子问题B", "子问题C"]
    assert extract_sub_questions("没有任何 JSON") == []
    assert extract_sub_questions('{"sub_questions": "不是数组"}') == []
