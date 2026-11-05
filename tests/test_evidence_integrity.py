"""来源完整性测试：编造 URL / 改写引文会被过滤，无来源时必须明确声明。"""

from __future__ import annotations

import tools
from llm_client import filter_evidence, heuristic_draft, heuristic_evidence

SOURCE = {
    "url": "https://real.example/a",
    "title": "真实来源",
    "content": (
        "人工中断需要 checkpointer 持久化状态，否则无法恢复。"
        "LangGraph 用 interrupt 函数暂停图，并把控制权交还给调用方。"
    ),
}


def test_filter_evidence_keeps_only_verbatim_citations():
    raw = [
        {
            "claim": "中断需要持久化",
            "source_url": "https://real.example/a",
            "quote": "人工中断需要 checkpointer 持久化状态，否则无法恢复。",
            "confidence": 0.8,
        },
        # 编造 URL → 丢弃
        {"claim": "编造", "source_url": "https://fake.example/b", "quote": "不存在的句子", "confidence": 0.9},
        # 真实 URL 但引文被改写 → 丢弃
        {"claim": "改写", "source_url": "https://real.example/a", "quote": "人工中断完全不需要状态持久化", "confidence": 0.9},
    ]
    items, dropped = filter_evidence([SOURCE], raw)
    assert len(items) == 1
    assert dropped == 2
    assert items[0]["source_url"] == SOURCE["url"]


def test_filter_evidence_rejects_half_sentences():
    raw = [
        {
            "claim": "半截句子",
            "source_url": SOURCE["url"],
            "quote": "人工中断需要 checkpointer 持久化状态，",
            "confidence": 0.8,
        },
        {
            "claim": "英文半截",
            "source_url": SOURCE["url"],
            "quote": "human in the loop needs a checkpointer and the",
            "confidence": 0.8,
        },
    ]
    items, dropped = filter_evidence([SOURCE], raw)
    assert items == []
    assert dropped == 2


def test_filter_evidence_maps_evidence_text_and_keeps_location_fields():
    raw = [
        {
            "evidence_text": "人工中断需要 checkpointer 持久化状态，否则无法恢复。",
            "quote": "人工中断需要 checkpointer 持久化状态，否则无法恢复。",
            "source_url": SOURCE["url"],
            "source_snippet": "小标题二：持久化机制",
            "relevance_reason": "直接说明中断依赖 checkpointer",
            "confidence": 0.9,
        }
    ]
    items, dropped = filter_evidence([SOURCE], raw)
    assert dropped == 0
    assert len(items) == 1
    assert items[0]["claim"].startswith("人工中断")
    assert items[0]["source_snippet"] == "小标题二：持久化机制"
    assert items[0]["relevance_reason"] == "直接说明中断依赖 checkpointer"


def test_heuristic_evidence_only_keeps_related_sentences():
    items = heuristic_evidence("checkpointer 人工中断", [SOURCE])
    assert items
    for item in items:
        assert item["quote"] in SOURCE["content"]
        assert item["source_url"] == SOURCE["url"]
        assert item["source_snippet"]
        assert item["relevance_reason"]
        assert 0.0 <= item["confidence"] <= 1.0
    # 宁缺毋滥：与问题完全无关时返回空数组
    assert heuristic_evidence("量子计算 芯片 制程", [SOURCE]) == []


def test_heuristic_evidence_filters_navigation_and_table_noise():
    noisy_source = {
        "url": "https://real.example/noise",
        "title": "噪声页面",
        "content": (
            "AI Agent 的核心能力是规划并调用工具完成任务。"
            "AI / 智能开发 AI Agent（智能体） AI（人工智能） Codex 教程 Vibe Coding 教程 "
            "Claude Code 教程 Ollama 教程 TensorFlow 教程 PyTorch 教程。"
            "2025 年规模以上工业主要产品产量及增长速度 产品名称 单位 本月 同比增长 % "
            "纱 2215.8 -0.1 布 306.7 0.2 化学纤维 8701.1 4.9 产量 1621.0 9.0。"
        ),
    }
    items = heuristic_evidence("AI Agent 的核心能力是什么？", [noisy_source])
    claims = [item["claim"] for item in items]
    assert any("规划并调用工具" in claim for claim in claims)
    assert not any("Codex 教程" in claim for claim in claims), "导航菜单不能当证据"
    assert not any("同比增长" in claim for claim in claims), "表格数据不能当证据"


def test_draft_without_evidence_declares_no_reliable_source():
    draft = heuristic_draft("一个没有来源的问题", [], [])
    assert "未找到可靠来源" in draft
    assert "http://" not in draft
    assert "https://" not in draft


def test_draft_with_evidence_cites_known_urls_only():
    evidence = [
        {
            "claim": "人工中断需要 checkpointer 持久化状态",
            "source_url": SOURCE["url"],
            "quote": "人工中断需要 checkpointer 持久化状态，否则无法恢复。",
            "confidence": 0.72,
        }
    ]
    draft = heuristic_draft("人工中断如何实现", evidence, [SOURCE])
    assert SOURCE["url"] in draft
    assert "fake.example" not in draft
    assert "## 来源清单" in draft


def test_summarize_sources_tool_uses_offline_extraction():
    result = tools.call_tool(
        "summarize_sources",
        {"sources": [SOURCE], "research_question": "人工中断需要什么？"},
    )
    assert result.ok is True
    assert result.data["evidence"], "离线模式也应提取出可回溯的逐句证据"
    for item in result.data["evidence"]:
        assert item["source_url"] == SOURCE["url"]


def test_sanitize_report_removes_unfetched_urls_and_rebuilds_sources():
    from graph import _sanitize_report

    sources = [{"url": "https://real.example/a", "title": "真实来源"}]
    evidence = [
        {
            "claim": "真实结论",
            "source_url": "https://real.example/a",
            "quote": "真实来源里的句子",
            "confidence": 0.6,
        }
    ]
    draft = (
        "# 报告\n\n## 关键发现\n\n"
        "- 确认见 [额外来源](https://fake.example/b)。\n"
        "- 另一条裸链接 https://fake2.example/c 。\n\n"
        "## 来源清单\n\n1. [伪造来源](https://fake.example/b)\n"
    )
    text, dropped = _sanitize_report(draft, sources, evidence)
    assert "fake.example/b" not in text
    assert "fake2.example/c" not in text
    assert "https://real.example/a" in text
    assert text.count("## 来源清单") == 1
    assert any("fake2.example" in url for url in dropped)


def test_sanitize_report_declares_no_source_and_strips_urls():
    from graph import _sanitize_report

    text, dropped = _sanitize_report("一段没有来源的结论 https://fake.example/x", [], [])
    assert "未找到可靠来源" in text
    assert "fake.example" not in text
    assert any("fake.example" in url for url in dropped)


def test_heuristic_draft_title_is_single_line_for_multiline_question():
    from llm_client import heuristic_draft

    question = "项目周会纪要\n| 议题 | 负责人 |\n| 调频 | 张明 |"
    draft = heuristic_draft(question, [], [])
    first_line = draft.splitlines()[0]
    assert first_line.startswith("# 研究报告：")
    assert "\n" not in first_line
    assert "|" not in first_line, "多行问题里的表格行不能进标题"


def test_sanitize_report_handles_uppercase_scheme_and_heading_variants():
    from graph import _sanitize_report

    draft = (
        "# 报告\n\n## 关键发现\n\n"
        "见 HTTPS://fake.example/upper 和 ftp://fake.example/file 。\n\n"
        "- 编造结论见HTTPS://fake.example/upper （紧邻中文也要清洗）\n\n"
        "#### 参考来源：\n\n1. HTTPS://fake.example/upper\n"
    )
    text, dropped = _sanitize_report(draft, [], [])
    assert "fake.example" not in text.lower(), "大写 scheme / ftp 链接也必须被清除"
    assert "参考来源" not in text, "带冒号/加粗/h4 的来源章节也要整段删除"
    assert "未找到可靠来源" in text
    assert dropped
