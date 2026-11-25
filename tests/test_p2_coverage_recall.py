"""P2：覆盖率按“方面”判定（修 1 条证据 5/5 假阳性）+ LLM 漏提取时的证据召回兜底。"""

from __future__ import annotations

import llm_client
import quality
import reporting

MARKET_SUB_QUESTIONS = [
    "2027 年中国 AI Agent 市场的关键数据与最新进展",
    "中国 AI Agent 市场的主要参与者与竞争格局",
    "中国 AI Agent 市场的驱动因素",
    "中国 AI Agent 市场的主要风险与挑战",
    "中国 AI Agent 市场的落地案例与典型场景",
]

# 真实复测里唯一一条市场证据：IDC 报告 + 2030 年 593.5 亿
MARKET_EVIDENCE = {
    "claim": "IDC 报告预计 2030 年中国 AI Agent 市场规模将达到 593.5 亿元。",
    "quote": "IDC 报告预计，2030 年中国 AI Agent 市场规模将达到 593.5 亿元，年复合增长率超过 30%。",
    "source_url": "https://example.org/idc",
    "numbers": ["593.5亿元|2030|中国 AI Agent 市场"],
    "source_type": "research",
}

QUESTION = "LangGraph 如何实现人工中断？"

OFFICIAL_PAGE = {
    "url": "https://example.org/a",
    "title": "LangGraph 官方文档：人工中断",
    "content": (
        "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。"
        "当节点调用 interrupt 函数时，图会暂停并把控制权交还给调用方。"
        "调用方随后使用 Command 的 resume 参数恢复执行，节点会从 interrupt 处继续。"
    ),
}

PRACTICE_PAGE = {
    "url": "https://example.org/b",
    "title": "实践：人工中断的常见坑",
    "content": (
        "在实践中，开发者需要在编译图时提供 checkpointer，否则人工中断无法暂停。"
        "另一个常见问题是为每次运行指定 thread_id，否则恢复执行时会读到旧状态。"
        "官方文档建议把写操作前的人工确认放在独立节点里，并在 resume 时传入审批结果。"
        "如果节点里有副作用（发邮件、写数据库），还需要保证节点可重入，避免恢复后重复执行。"
        "教程里给出了 interrupt 与 Command(resume=) 的最小示例，可以直接照着改。"
        "此外，人工中断的确认点最好只读不写，把真正的写入放到 resume 之后。"
    ),
}

DEDUPE_PAGE = {
    "url": "https://example.org/c",
    "title": "重复观点页",
    "content": (
        "人工中断需要 checkpointer，否则图无法暂停。"
        "页面里还有一些介绍性文字，用来模拟真实文章的长段落和背景铺垫。"
        "这些文字不包含研究问题的关键词，因此不会被启发式摘录选中。"
        "它们只是为了让正文长度超过阈值，方便验证去重逻辑是否生效。"
        "真实网页往往有大量导航和背景内容，这里用无害文字模拟这种情况。"
    ),
}

EXISTING_EVIDENCE = {
    "claim": "LangGraph 通过 checkpointer 持久化状态，从而支持人工中断。",
    "quote": "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。",
    "source_url": "https://example.org/a",
}


# ---------------------------------------------------------------- 覆盖率（P2-1）
def test_market_coverage_does_not_treat_topic_only_evidence_as_full_coverage():
    coverage, summary = quality.compute_coverage(MARKET_SUB_QUESTIONS, [MARKET_EVIDENCE])

    assert summary["total"] == 5
    assert summary["covered"] <= 2, "1 条市场规模证据不能覆盖玩家/驱动/风险/案例"
    assert summary["rate"] < 0.75
    assert coverage[0]["covered"] is True, "关键数据与最新进展可由这条证据覆盖"
    assert coverage[0]["aspects"] == ["数据", "趋势"]
    assert coverage[1]["covered"] is False, "竞争格局需要玩家/竞争信号"
    assert coverage[1]["missing_aspects"] == ["参与者"]


def test_generic_topic_word_alone_does_not_cover_a_subquestion():
    coverage, _ = quality.compute_coverage(
        ["AI Agent 市场的竞争格局与主要参与者"],
        [
            {
                "claim": "AI Agent 市场规模持续扩大，行业保持增长。",
                "quote": "AI Agent 市场规模持续扩大，行业保持增长。",
                "source_url": "https://example.org/size",
            }
        ],
    )

    assert coverage[0]["covered"] is False
    assert "参与者" in coverage[0]["missing_aspects"]


def test_data_aspect_requires_a_metric_number():
    coverage, _ = quality.compute_coverage(
        ["LangGraph 的关键数据与最新进展"],
        [
            {
                "claim": "LangGraph 的最新进展值得关注。",
                "quote": "LangGraph 的最新进展值得关注。",
                "source_url": "https://example.org/trend",
            }
        ],
    )

    assert coverage[0]["covered"] is False, "只有趋势词、没有数值，不能算覆盖“关键数据”"
    assert coverage[0]["missing_aspects"] == ["数据"]


def test_repair_draft_lists_uncovered_in_limits():
    """覆盖率判了未覆盖，报告“局限”不能还写“子问题均已覆盖”。"""
    draft = (
        "## 摘要\n\n结论[S1]。依据[S2]。\n\n"
        "## 局限与未证实\n\n"
        "- 子问题均至少有一条证据，但“已覆盖”判定基于词面重叠，可能高估实际覆盖。\n"
        "- 标有【推断】的内容是基于证据的推理，不是原文结论。\n"
    )
    fixed, fixes = reporting.repair_draft(draft, [], uncovered=["2027年中国AI Agent市场增长驱动因素"])

    assert "未覆盖：2027年中国AI Agent市场增长驱动因素" in fixed
    assert "子问题均至少有一条证据" not in fixed
    assert any(item["code"] == "inserted_uncovered_limits" for item in fixes)


def test_draft_review_limits_follow_coverage_uncovered(tmp_path):
    """大纲给“证据不足”的子问题也分了小节时，局限仍要按覆盖率指标列未覆盖项。"""
    from graph import draft_review
    from state import new_state

    state = new_state("LangGraph 如何实现人工中断？", trace_id="cov-limits", log_dir=str(tmp_path))
    state["evidence"] = [
        {
            "claim": "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。",
            "quote": "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。",
            "source_url": "https://e.test/a",
            "confidence": 0.8,
        }
    ]
    state["fetched_pages"] = [
        {"url": "https://e.test/a", "title": "A", "content": "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。"}
    ]
    state["coverage_summary"] = {"total": 2, "covered": 1, "rate": 0.5, "uncovered": ["常见问题与最佳实践"]}
    state["outline"] = {"uncovered": []}
    state["draft_raw"] = (
        "## 摘要\n\nLangGraph 通过 checkpointer 持久化状态，从而支持人工中断[S1]。\n\n"
        "## 关键发现\n\n- LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断[S1]\n\n"
        "## 分析与推演\n\n- 【推断】人工中断依赖持久化层，缺少 checkpointer 时无法恢复[S1]。\n\n"
        "## 局限与未证实\n\n- 子问题均至少有一条证据，但“已覆盖”判定基于词面重叠，可能高估实际覆盖。\n"
    )

    updates = draft_review(state)
    report = updates["draft_report"]

    assert "未覆盖：常见问题与最佳实践" in report
    assert "子问题均至少有一条证据" not in report


# ---------------------------------------------------------------- 证据召回兜底（P2-2）
def test_supplement_evidence_fills_pages_llm_skipped():
    items, stats = llm_client.supplement_evidence(
        QUESTION, [OFFICIAL_PAGE, PRACTICE_PAGE], [EXISTING_EVIDENCE]
    )

    assert stats["pages_missed"] == 1, "官方文档已有证据，只有实践页需要兜底"
    assert 1 <= stats["supplemented"] <= 2, "兜底条数受单页上限约束"
    assert all(item["source_url"] == PRACTICE_PAGE["url"] for item in items)
    assert all(item.get("extraction") == "heuristic_supplement" for item in items)
    assert all(item["claim"] == item["quote"] for item in items), "兜底必须是原句摘录，不改写"
    assert all(item["confidence"] <= 0.55 for item in items), "兜底证据置信度低于模型提取"


def test_supplement_evidence_dedupes_and_can_be_disabled(monkeypatch):
    duplicate = {
        "claim": "人工中断需要 checkpointer，否则图无法暂停。",
        "quote": "人工中断需要 checkpointer，否则图无法暂停。",
        "source_url": "https://example.org/a",
    }
    items, stats = llm_client.supplement_evidence(QUESTION, [DEDUPE_PAGE], [duplicate])
    assert stats["supplemented"] == 0, "和已有证据同义的句子不再重复补入"
    assert items == []

    monkeypatch.setenv("AGENT_EVIDENCE_SUPPLEMENT", "0")
    items, stats = llm_client.supplement_evidence(QUESTION, [PRACTICE_PAGE], [])
    assert items == []
    assert stats["supplemented"] == 0


def test_supplement_evidence_keeps_language_aligned():
    english_page = {
        "url": "https://example.org/en",
        "title": "What is an AI agent?",
        "content": (
            "An AI agent is an autonomous program that can accomplish goals on behalf of a user. "
            "Unlike chatbots, agents plan their own workflow and call tools when needed. "
            "This definition comes from a general AI overview page rather than a SaaS comparison."
        ),
    }
    items, stats = llm_client.supplement_evidence(QUESTION, [english_page], [])
    assert items == [], "中文问题不把英文定义页的句子补进证据"
    assert stats["pages_missed"] == 1


def test_supplement_evidence_skips_directory_and_weak_hits():
    directory_page = {
        "url": "https://example.org/tools",
        "title": "AI 工具集导航大全",
        "content": PRACTICE_PAGE["content"],
    }
    items, _ = llm_client.supplement_evidence(QUESTION, [directory_page], [])
    assert items == [], "工具导航页即使正文够长也不补证据"

    weak_page = {
        "url": "https://example.org/weak",
        "title": "LangGraph 概述",
        "content": (
            "LangGraph 是一个用于构建有状态智能体的编排框架。"
            "这段文字只是背景介绍，没有涉及具体的技术细节。"
            "页面里还有一些无关的说明，用来保证正文长度超过阈值。"
            "最后再补一句填充内容，避免页面因为过短而被直接跳过。"
        ),
    }
    items, _ = llm_client.supplement_evidence(QUESTION, [weak_page], [])
    assert items == [], "只命中 1 个问题关键词的句子不能进兜底证据"


def test_summarize_evidence_supplements_after_llm_extraction(monkeypatch):
    client = llm_client.LLMClient(mode="openai", api_key="test-key")

    def fake_chat_json(*args, **kwargs):
        return {
            "evidence": [
                {
                    "claim": "LangGraph 用 checkpointer 持久化状态以支持人工中断。",
                    "quote": "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。",
                    "source_url": OFFICIAL_PAGE["url"],
                    "source_snippet": "第一段",
                    "confidence": 0.8,
                }
            ],
            "note": "",
        }

    monkeypatch.setattr(client, "chat_json", fake_chat_json)
    result = client.summarize_evidence(QUESTION, [OFFICIAL_PAGE, PRACTICE_PAGE])

    assert result["mode"] == "openai"
    assert len(result["evidence"]) >= 2, "LLM 只提取了 1 条，兜底要把漏掉的来源补上"
    assert any(item.get("extraction") == "heuristic_supplement" for item in result["evidence"])
    assert "启发式摘录" in result["note"]
    assert {item["source_url"] for item in result["evidence"]} == {
        OFFICIAL_PAGE["url"],
        PRACTICE_PAGE["url"],
    }
