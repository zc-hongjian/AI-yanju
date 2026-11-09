"""报告质量层（reporting.py）测试：证据卡片化、大纲、引用硬校验与程序修复。

四个症状对应四组用例：
- 引用堆叠错误 → validate_draft / repair_draft
- 逻辑割裂     → build_outline（按子问题分节）
- 摘要太水     → validate_draft 的 summary 规则 + 草稿重写入口
- 缺乏推演     → analysis_lines / 缺节补齐
"""

from __future__ import annotations

import reporting
from state import new_state

SOURCES = [
    {"url": "https://example.org/a", "title": "官方文档"},
    {"url": "https://example.org/b", "title": "实践文章"},
]

SUB_QUESTIONS = [
    "LangGraph 如何实现人工中断？",
    "人工中断为什么需要 checkpointer？",
]

EVIDENCE = [
    {
        "claim": "LangGraph 通过 interrupt 函数在节点内部暂停执行。",
        "quote": "LangGraph 通过 interrupt 函数在节点内部暂停执行，并把控制权交还给调用方。",
        "source_url": "https://example.org/a",
        "source_snippet": "机制",
        "confidence": 0.9,
    },
    {
        "claim": "恢复执行需要 Command 的 resume 参数与 thread_id。",
        "quote": "调用方用 Command 的 resume 参数恢复执行，并为每次运行指定 thread_id。",
        "source_url": "https://example.org/a",
        "source_snippet": "恢复",
        "confidence": 0.8,
    },
    {
        "claim": "没有 checkpointer 时 interrupt 会直接报错。",
        "quote": "如果没有 checkpointer，interrupt 会直接报错，流程无法暂停。",
        "source_url": "https://example.org/b",
        "source_snippet": "前提",
        "confidence": 0.85,
    },
]

GOOD_DRAFT = """# 研究报告：LangGraph 人工中断

## 摘要
LangGraph 用 interrupt 在节点内部暂停执行，控制权交还给调用方[S1]。恢复依赖 Command 的 resume 参数与 thread_id，两者都建立在 checkpointer 之上[S2][S3]。该结论以官方文档的机制描述为准，未覆盖生产环境的并发场景。

## 关键发现
- interrupt 是节点级暂停原语，不是全局开关，暂停点由运行时数据决定 [S1]。
- 恢复执行必须提供 thread_id，否则 checkpoint 无法定位 [S2]。
- 缺少 checkpointer 时 interrupt 会直接报错，这是设计上的强制依赖 [S3]。

## 分析与推演
- 【推断】由 [S1] 的暂停点与 [S3] 的强制依赖可推知：人工中断不是"可选功能"，而是需要状态持久化支撑的机制；若部署时没有持久化层，该能力不成立。
- 【推断】若并发写入同一 thread_id，resume 可能读到旧状态；现有证据没有覆盖并发场景。

## 局限与未证实
- 未覆盖：并发/多 worker 场景下的 thread_id 隔离没有证据。
- 证据共 3 条，来自 2 个来源，均为一手文档或实践文章，时效性需复核。
"""


def _evidence_with_cards() -> list[dict]:
    cards, _ = reporting.enrich_evidence(EVIDENCE, SUB_QUESTIONS)
    return cards


# ---------------------------------------------------------------- 证据卡片化


def test_classify_kind_recognises_definition_and_mechanism():
    assert reporting.classify_kind("LangGraph 是一个编排框架") == "定义"
    assert reporting.classify_kind("通过 interrupt 暂停节点") == "机制"
    assert reporting.classify_kind("延迟降低 32% 到 120ms") == "数据"


def test_is_promo_flags_marketing_and_testimonials():
    assert reporting.is_promo("受 Klarna、Replit 等公司信任")
    assert reporting.is_promo('"LangGraph is central to our multi-actor agentic workflows."')
    assert not reporting.is_promo("LangGraph 通过 checkpointer 持久化状态")


def test_enrich_evidence_assigns_sub_question_and_kind():
    cards, stats = reporting.enrich_evidence(EVIDENCE, SUB_QUESTIONS)
    assert len(cards) == 3
    assert all(card["sub_question"] for card in cards)
    assert cards[0]["kind"] in {"定义", "机制", "事实"}
    assert stats["total"] == 3


def test_enrich_evidence_dedupes_same_claim_same_source():
    duplicated = EVIDENCE + [dict(EVIDENCE[0])]
    cards, stats = reporting.enrich_evidence(duplicated, SUB_QUESTIONS)
    assert len(cards) == 3
    assert stats["deduped"] == 1
    assert cards[0]["merged_count"] == 2


def test_enrich_evidence_keeps_cross_source_same_claim_for_cross_check():
    cross = EVIDENCE + [dict(EVIDENCE[0], source_url="https://example.org/b")]
    cards, _ = reporting.enrich_evidence(cross, SUB_QUESTIONS)
    same_claim = [card for card in cards if card["claim"] == EVIDENCE[0]["claim"]]
    assert len(same_claim) == 1, "跨来源的同一结论合并为一张卡，并记录 merged_urls"
    assert len(same_claim[0]["merged_urls"]) == 2


# ---------------------------------------------------------------- 大纲


def test_build_outline_groups_evidence_by_sub_question():
    cards = _evidence_with_cards()
    outline = reporting.build_outline(
        "LangGraph 如何实现人工中断？", SUB_QUESTIONS, cards, uncovered=["并发场景"]
    )
    titles = [section["title"] for section in outline["sections"]]
    assert titles[0] == "摘要"
    assert "分析与推演" in titles and "局限与未证实" in titles
    content = [section for section in outline["sections"] if section["key"].startswith("content")]
    assert content, "必须按子问题分出内容小节"
    assert all(section["evidence_ids"] for section in content), "每节都要分配证据编号"
    assert outline["uncovered"] == ["并发场景"]


def test_normalize_llm_outline_drops_unknown_ids_and_appends_missing_sections():
    data = {
        "sections": [
            {"title": "机制", "goal": "讲清暂停机制", "evidence_ids": [1, 99]},
            {"title": "恢复", "goal": "讲清恢复", "evidence_ids": [2]},
        ]
    }
    outline = reporting.normalize_llm_outline(data, _evidence_with_cards())
    assert outline is not None
    sections = outline["sections"]
    mechanism = next(section for section in sections if section["title"] == "机制")
    assert mechanism["evidence_ids"] == [1], "不存在的编号必须被丢掉"
    titles = [section["title"] for section in sections]
    assert "关键发现" in titles and "分析与推演" in titles and "局限与未证实" in titles


# ---------------------------------------------------------------- 引用硬校验


def test_validate_draft_passes_a_clean_report():
    result = reporting.validate_draft(GOOD_DRAFT, _evidence_with_cards())
    assert result["passed"], result["issues"]
    assert result["checked_citations"] >= 5


def test_validate_draft_flags_citation_stacking():
    cards = _evidence_with_cards()
    draft = GOOD_DRAFT.replace("[S2][S3]", "[S1][S2][S3]")
    result = reporting.validate_draft(draft, cards)
    assert any(issue["code"] == "citation_stacking" for issue in result["issues"])


def test_validate_draft_flags_out_of_range_and_mismatch():
    cards = _evidence_with_cards()
    draft = (
        "## 摘要\n"
        "LangGraph 用 interrupt 暂停执行[S1]。恢复需要 resume 参数[S2]。边界未覆盖。\n\n"
        "## 关键发现\n"
        "- 中断机制成立 [S9]。\n"
        "- 天气很好，适合出门散步 [S1]。\n\n"
        "## 局限与未证实\n"
        "- 未覆盖并发场景。\n"
    )
    result = reporting.validate_draft(draft, cards)
    codes = {issue["code"] for issue in result["issues"]}
    assert "citation_out_of_range" in codes
    assert "citation_mismatch" in codes
    assert "missing_section" in codes  # 缺「分析与推演」


def test_validate_draft_flags_summary_without_citation():
    head, rest = GOOD_DRAFT.split("## 关键发现", 1)
    naked_summary = head.replace("[S1]", "").replace("[S2][S3]", "")
    result = reporting.validate_draft(naked_summary + "## 关键发现" + rest, _evidence_with_cards())
    assert any(issue["code"] == "summary_without_citation" for issue in result["issues"])


def test_validate_draft_flags_missing_citations_and_paragraph_findings():
    cards = _evidence_with_cards()
    draft = (
        "## 摘要\n"
        "LangGraph 用 interrupt 暂停执行。恢复需要 resume 参数。边界未覆盖。\n\n"
        "## 关键发现\n"
        "没有引用的一大段话，说了很多但没有证据。\n\n"
        "## 分析与推演\n"
        "- 【推断】这是推演但没有引用。\n\n"
        "## 局限与未证实\n"
        "- 未覆盖并发场景。\n"
    )
    result = reporting.validate_draft(draft, cards)
    codes = {issue["code"] for issue in result["issues"]}
    assert "no_citations" in codes
    assert "findings_not_bulleted" in codes
    assert "summary_without_citation" in codes


def test_validate_draft_flags_vague_summary_opener():
    cards = _evidence_with_cards()
    draft = GOOD_DRAFT.replace(
        "LangGraph 用 interrupt 在节点内部暂停执行，控制权交还给调用方[S1]。",
        "LangGraph 和 AutoGen 在人工介入设计上存在差异[S1]。",
    )
    result = reporting.validate_draft(draft, cards)
    assert any(issue["code"] == "summary_vague" for issue in result["issues"])


# ---------------------------------------------------------------- 程序修复


def test_repair_draft_trims_stacking_and_removes_invalid_citations():
    cards = _evidence_with_cards()
    broken = "## 关键发现\n\n- LangGraph 人工中断依赖 checkpointer、resume 参数与 thread_id [S1][S2][S3][S9]\n"
    fixed, fixes = reporting.repair_draft(broken, cards)
    assert "[S9]" not in fixed
    assert fixed.count("[S") <= 2, "单句最多保留 2 个引用"
    assert any(item["code"] == "trimmed_stacking" for item in fixes)


def test_repair_draft_is_noop_on_a_clean_report():
    """回归：修复流程不能把合规报告里的角标搬位置（曾导致引用丢失+堆叠）。"""
    cards = _evidence_with_cards()
    fixed, fixes = reporting.repair_draft(GOOD_DRAFT, cards)
    assert fixes == [], fixes
    result = reporting.validate_draft(fixed, cards)
    assert result["passed"], result["issues"]
    assert fixed.count("[S1]") == GOOD_DRAFT.count("[S1]")


def test_repair_draft_inserts_missing_analysis_section():
    cards = _evidence_with_cards()
    draft = "## 摘要\n\n结论[S1]。依据[S2]。边界未覆盖。\n\n## 局限与未证实\n\n- 未覆盖并发。\n"
    fixed, fixes = reporting.repair_draft(draft, cards, uncovered=["并发场景"])
    assert "## 分析与推演" in fixed
    assert "【推断】" in fixed
    assert any(item["code"] == "inserted_analysis" for item in fixes)
    # 分析小节必须插在局限之前
    assert fixed.index("## 分析与推演") < fixed.index("## 局限与未证实")


def test_limits_lines_mentions_uncovered_and_source_mix():
    lines = "\n".join(reporting.limits_lines(_evidence_with_cards(), ["并发场景"]))
    assert "并发场景" in lines
    assert "证据共 3 条" in lines
    assert "【推断】" in lines


# ---------------------------------------------------------------- 节点级联调


def _state_with_evidence(tmp_path) -> dict:
    state = new_state("LangGraph 如何实现人工中断？", trace_id="t-report", log_dir=str(tmp_path))
    state["plan"] = {"sub_questions": SUB_QUESTIONS, "queries": [{"query": "q"}]}
    state["evidence"] = _evidence_with_cards()
    state["fetched_pages"] = [
        {"url": source["url"], "title": source["title"], "content": "正文"} for source in SOURCES
    ]
    state["coverage_summary"] = {"total": 2, "covered": 2, "rate": 1.0, "uncovered": []}
    return state


def test_plan_outline_node_falls_back_to_rule_outline(tmp_path):
    from graph import plan_outline

    state = _state_with_evidence(tmp_path)
    updates = plan_outline(state)
    outline = updates["outline"]
    assert outline["source"] == "rule", "离线模式必须走规则大纲"
    assert len(outline["sections"]) >= 4


def test_draft_and_review_nodes_produce_cited_structured_report(tmp_path):
    from graph import draft_report, draft_review, plan_outline

    state = _state_with_evidence(tmp_path)
    state.update(plan_outline(state))
    state.update(draft_report(state))
    assert "## 分析与推演" in state["draft_raw"], "离线草稿也要有推演小节"
    state.update(draft_review(state))

    review = state["draft_review"]
    assert review["status"] in {"passed", "repaired", "rewritten"}
    assert review["checks"]["remaining_issues"] <= len(review["issues"])
    report = state["draft_report"]
    for title in ("摘要", "关键发现", "分析与推演", "局限与未证实", "来源清单"):
        assert f"## {title}" in report
    # 关键发现每一条都必须带引用
    findings = report.split("## 关键发现", 1)[1].split("## ", 1)[0]
    bullets = [line for line in findings.splitlines() if line.startswith("- ")]
    assert bullets and all("[S" in line for line in bullets)


def test_draft_review_disabled_by_env(tmp_path, monkeypatch):
    from graph import draft_report, draft_review, plan_outline

    monkeypatch.setenv("AGENT_DRAFT_REVIEW", "0")
    state = _state_with_evidence(tmp_path)
    state.update(plan_outline(state))
    state.update(draft_report(state))
    state.update(draft_review(state))
    assert state["draft_review"]["status"] == "skipped"


def test_evidence_cards_disabled_keeps_legacy_evidence(tmp_path, monkeypatch):
    """消融开关：AGENT_EVIDENCE_CARDS=0 时证据不加卡片字段，回到旧行为。"""
    from graph import summarize_sources_node

    monkeypatch.setenv("AGENT_EVIDENCE_CARDS", "0")
    state = new_state("LangGraph 如何实现人工中断？", trace_id="t-cards", log_dir=str(tmp_path))
    state["plan"] = {"sub_questions": SUB_QUESTIONS}
    state["fetched_pages"] = [
        {
            "url": SOURCES[0]["url"],
            "title": SOURCES[0]["title"],
            "content": EVIDENCE[0]["quote"],
        }
    ]
    updates = summarize_sources_node(state)
    assert updates["evidence"], "离线也要能抽到证据"
    assert "kind" not in updates["evidence"][0]


class _BadRewriter:
    """模拟“修引用时把角标全删掉”的模型：这种重写必须被丢弃。"""

    mode = "openai"

    def rewrite_draft(self, question, draft, issues):
        return (
            "## 摘要\n\nLangGraph 和 AutoGen 的人工介入设计存在差异。\n\n"
            "## 关键发现\n\n没有引用的一大段话，说了很多但没有证据。\n\n"
            "## 局限与未证实\n\n- 未覆盖并发场景。\n"
        )


def test_draft_review_rejects_rewrite_that_drops_citations(tmp_path, monkeypatch):
    import graph as graph_module
    from graph import draft_review

    monkeypatch.setattr(graph_module, "get_llm", lambda: _BadRewriter())
    state = _state_with_evidence(tmp_path)
    state["draft_raw"] = GOOD_DRAFT.replace("[S2][S3]", "[S1][S2][S3]")
    updates = draft_review(state)
    review = updates["draft_review"]
    assert review["rejected_rewrites"] >= 1, "重写后问题更多就必须丢弃"
    assert "[S" in updates["draft_report"], "草稿的引用不能因为一次失败重写而丢光"
