"""P0 研究质量闭环测试：子问题覆盖、缺口补检、claim 支持校验。"""

from __future__ import annotations

import pytest

import graph as graph_module
import quality
import tools
from langgraph.types import Command
from state import new_state

SUB_QUESTIONS = [
    "LangGraph 的核心结论与主要事实",
    "LangGraph 的关键数据与最新进展",
    "LangGraph 的主要挑战与原因",
    "LangGraph 有哪些权威来源与案例",
]

GENERAL_EVIDENCE = {
    "claim": "LangGraph 是一个用于构建有状态智能体的编排框架。",
    "quote": "LangGraph 是一个用于构建有状态智能体的编排框架。",
    "source_url": "https://example.org/general",
    "confidence": 0.7,
}


def test_coverage_only_counts_covered_subquestions():
    coverage, summary = quality.compute_coverage(SUB_QUESTIONS, [GENERAL_EVIDENCE])

    assert summary["total"] == 4
    assert 0 < summary["rate"] < 1, "只讲主题概况不能算覆盖了数据/挑战/案例"
    assert coverage[0]["covered"] is True, "“核心结论与主要事实”可由概况覆盖"
    assert coverage[1]["covered"] is False
    assert coverage[1]["score"] < 0.6


def test_coverage_rises_when_evidence_covers_more_aspects():
    evidence = [
        GENERAL_EVIDENCE,
        {
            "claim": "LangGraph 的关键数据：2025 年采用率增长 40%，最新进展值得关注。",
            "quote": "LangGraph 的关键数据：2025 年采用率增长 40%，最新进展值得关注。",
            "source_url": "https://example.org/data",
        },
    ]
    coverage, summary = quality.compute_coverage(SUB_QUESTIONS, evidence)

    assert summary["rate"] > 0.25
    assert coverage[1]["covered"] is True


def test_gap_queries_skip_first_round_queries():
    coverage, _ = quality.compute_coverage(SUB_QUESTIONS, [GENERAL_EVIDENCE])
    gaps = quality.gap_queries_for(
        coverage,
        question="LangGraph 是什么？它主要解决什么问题？",
        existing_queries=["LangGraph 的核心结论与主要事实"],
        limit=3,
    )

    assert gaps
    assert all(gap["query"] != "LangGraph 的核心结论与主要事实" for gap in gaps)
    assert any("最新" in gap["query"] or "数据" in gap["query"] for gap in gaps)


def test_verify_evidence_drops_unrelated_claim_but_keeps_paraphrase():
    quote = "人工中断需要 checkpointer 持久化状态，否则无法恢复。"
    raw = [
        {
            "claim": "人工中断需要 checkpointer 持久化状态，否则无法恢复。",
            "quote": quote,
            "source_url": "https://real.example/a",
        },
        {
            "claim": "人工中断依赖状态持久化",
            "quote": quote,
            "source_url": "https://real.example/a",
        },
        {
            "claim": "量子计算芯片的制程进展",
            "quote": quote,
            "source_url": "https://real.example/a",
        },
    ]

    kept, summary = quality.verify_evidence(raw)

    assert summary["total"] == 3
    assert summary["supported"] == 1
    assert summary["unsupported"] == 1
    assert summary["dropped"] == 1
    assert len(kept) == 2
    assert all(item["support"] in {"supported", "partial"} for item in kept)
    assert all(item.get("support_score") is not None for item in kept)


def test_claim_with_number_missing_in_quote_is_partial():
    score, reason = quality.claim_support(
        "LangGraph 在 2025 年采用率增长 40%",
        "LangGraph 是一个用于构建有状态智能体的编排框架，被多家公司采用。",
    )
    assert score <= 0.5
    assert "数字" in reason


def test_check_coverage_triggers_gap_and_respects_round_limit(tmp_path):
    state = new_state(
        "LangGraph 是什么？它主要解决什么问题？",
        trace_id="cov-1",
        log_dir=str(tmp_path / "logs"),
    )
    state["plan"] = {"sub_questions": SUB_QUESTIONS, "queries": [{"query": "LangGraph"}]}
    state["evidence"] = [GENERAL_EVIDENCE]
    state["retrieval_round"] = 0

    updates = graph_module.check_coverage(state)
    assert updates["gap_queries"], "覆盖率不足时必须生成补检 query"
    assert updates["retrieval_round"] == 1
    assert updates["coverage_summary"]["rate"] < 1

    state["retrieval_round"] = 1
    updates = graph_module.check_coverage(state)
    assert updates["gap_queries"] == [], "达到最大轮次后不能继续补检"


def test_graph_gap_round_merges_new_pages_and_reports_coverage(tmp_path, monkeypatch):
    """首轮只有概况页；补检返回含步骤/数据的第二页，最终来源应包含两页。"""
    alpha = {
        "url": "https://example.org/alpha",
        "title": "LangGraph 人工中断概述",
        "content": (
            "LangGraph 是一个用于构建有状态智能体的编排框架。"
            "它通过 interrupt 暂停图执行，并用 checkpointer 保存状态。"
        ),
    }
    gamma = {
        "url": "https://example.org/gamma",
        "title": "LangGraph 人工中断实践",
        "content": (
            "LangGraph 人工中断的实现步骤与代码示例如下。"
            "前置条件与依赖包括 langgraph 与 checkpointer。"
            "常见问题与最佳实践是保持节点可重入。"
            "权威来源与案例可参考官方文档。"
        ),
    }
    pages = {alpha["url"]: alpha, gamma["url"]: gamma}
    calls = {"search": [], "fetch": []}

    def fake_search(query: str, max_results: int = 5):
        calls["search"].append(query)
        hit = alpha if len(calls["search"]) == 1 else gamma
        return [
            {
                "title": hit["title"],
                "url": hit["url"],
                "snippet": hit["content"][:60],
                "score": 0.9,
                "query": query,
            }
        ]

    def fake_fetch(url: str):
        calls["fetch"].append(url)
        page = pages[str(url)]
        return {
            "url": page["url"],
            "title": page["title"],
            "content": page["content"],
            "chars": len(page["content"]),
            "encoding": "utf-8",
        }

    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "search_web", fake_search)
    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "fetch_page", fake_fetch)
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AGENT_MAX_RETRIEVAL_ROUNDS", "2")

    graph = graph_module.build_graph()
    config = {"configurable": {"thread_id": "gap-round"}}
    state = new_state(
        "LangGraph 如何实现人工中断？",
        trace_id="gap-round",
        log_dir=str(tmp_path / "logs"),
    )

    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume={"decision": "approve", "comment": ""}), config)
    assert result["__interrupt__"][0].value["type"] == "email_confirmation"
    result = graph.invoke(Command(resume={"send": False}), config)
    result = graph.invoke(Command(resume={"save": False}), config)

    final = result["final_output"]
    assert len(calls["search"]) >= 2, "覆盖不足时应触发第二轮检索"
    assert alpha["url"] in {source["url"] for source in final["sources"]}
    assert gamma["url"] in {source["url"] for source in final["sources"]}
    assert final["retrieval_rounds"] >= 2
    assert final["coverage_summary"]["total"] == len(final["sub_questions"])
    assert 0 <= final["coverage_summary"]["rate"] <= 1
    assert "verification" in final
    assert final["verification"]["total"] >= 1
