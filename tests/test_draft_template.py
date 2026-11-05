"""报告模板「资深技术研究员」测试：三段式结构、S 编号引用、同义证据合并。"""

from __future__ import annotations

import re

import prompts
from graph import _sanitize_report
from llm_client import heuristic_draft

SOURCES = [
    {"url": "https://real.example/a", "title": "来源 A"},
    {"url": "https://real.example/b", "title": "来源 B"},
]
EVIDENCE = [
    {
        "claim": "LangGraph 通过 checkpointer 持久化状态，从而支持人工中断。",
        "source_url": "https://real.example/a",
        "quote": "LangGraph 通过 checkpointer 持久化状态，从而支持人工中断。",
        "source_snippet": "小节一",
        "confidence": 0.8,
    },
    {
        "claim": "调用方通过 Command 的 resume 参数恢复执行。",
        "source_url": "https://real.example/a",
        "quote": "调用方通过 Command 的 resume 参数恢复执行。",
        "source_snippet": "小节二",
        "confidence": 0.7,
    },
    {
        # 与 S1 表达同一意思，应合并为 [S1][S3]
        "claim": "LangGraph 通过 checkpointer 持久化状态，从而支持人工中断。",
        "source_url": "https://real.example/b",
        "quote": "两种方式都依赖 checkpoint 才能恢复，否则中断后的状态会丢失。",
        "source_snippet": "小节三",
        "confidence": 0.6,
    },
]


def _section(draft: str, name: str) -> str:
    return draft.split(f"## {name}", 1)[1].split("## ", 1)[0]


def test_draft_has_required_sections_and_no_evidence_table():
    draft = heuristic_draft("LangGraph 如何实现人工中断？", EVIDENCE, SOURCES)
    for name in ("摘要", "关键发现", "局限与未证实"):
        assert f"## {name}" in draft
    assert "## 证据表" not in draft, "模板只要求三段式结构"
    assert "今天给大家" not in draft
    assert "我们来看看" not in draft


def test_summary_is_two_to_three_sentences_with_citations():
    draft = heuristic_draft("LangGraph 如何实现人工中断？", EVIDENCE, SOURCES)
    summary = _section(draft, "摘要")
    sentences = [item for item in re.split(r"[。！？]", summary) if item.strip()]
    assert 2 <= len(sentences) <= 3
    assert "[S" in summary, "摘要同样要标注来源角标"


def test_key_findings_merge_duplicates_and_cite():
    draft = heuristic_draft("LangGraph 如何实现人工中断？", EVIDENCE, SOURCES)
    findings = _section(draft, "关键发现")
    bullets = [line for line in findings.splitlines() if line.startswith("- ")]
    assert len(bullets) == 2, "S1 与 S3 是同一结论，应合并为一条"
    assert all(re.search(r"\[S\d+\]", line) for line in bullets), "每条关键发现都必须带引用角标"
    assert "[S1][S3]" in draft


def test_no_evidence_draft_declares_uncovered_aspects():
    draft = heuristic_draft("一个没有来源的问题", [], [])
    assert "未找到可靠来源" in draft
    assert "## 局限与未证实" in draft
    assert "当前资料未覆盖" in draft
    assert "http://" not in draft
    assert "https://" not in draft


def test_prompt_template_keeps_research_analyst_rules():
    for phrase in ("资深的技术研究员", "先总结", "消除重复", "强制引用", "语气专业"):
        assert phrase in prompts.DRAFT_SYSTEM
    user = prompts.draft_user("LangGraph 如何实现人工中断？", EVIDENCE, SOURCES)
    assert "【用户问题】：LangGraph 如何实现人工中断？" in user
    assert "[S1]" in user
    assert "[S3]" in user


def test_source_list_uses_evidence_order_and_labels():
    evidence = [
        {"claim": "B 的结论", "source_url": "https://real.example/b", "quote": "q", "confidence": 0.5},
        {"claim": "A 的结论", "source_url": "https://real.example/a", "quote": "q", "confidence": 0.5},
    ]
    text, _ = _sanitize_report(
        "## 关键发现\n\n- B 的结论 [S1]\n- A 的结论 [S2]\n", SOURCES, evidence
    )
    lines = text.split("## 来源清单", 1)[1].strip().splitlines()
    assert "real.example/b" in lines[0] and "[S1]" in lines[0]
    assert "real.example/a" in lines[1] and "[S2]" in lines[1]


def test_source_list_merges_same_url_and_keeps_labels():
    lines = prompts.source_list_lines(EVIDENCE, SOURCES)
    assert any("[S1][S2]" in line and "real.example/a" in line for line in lines)
    assert any("[S3]" in line and "real.example/b" in line for line in lines)


def test_source_list_lists_fetched_pages_without_evidence():
    extra = SOURCES + [{"url": "https://real.example/c", "title": "来源 C"}]
    text, _ = _sanitize_report("## 关键发现\n\n- 结论 [S1]\n", extra, EVIDENCE[:1])
    assert "real.example/c" in text, "抓取到但未被引用的页面也要出现在来源清单里"


def test_citations_become_clickable_links():
    text, _ = _sanitize_report("## 关键发现\n\n- 结论 [S1]\n- 结论二 [S2][S3]\n", SOURCES, EVIDENCE)
    assert "[S1](https://real.example/a)" in text
    assert "[S2](https://real.example/a)" in text
    assert "[S3](https://real.example/b)" in text


def test_invalid_citation_index_is_kept_as_plain_text():
    text, _ = _sanitize_report("## 关键发现\n\n- 结论 [S9]\n", SOURCES, EVIDENCE)
    assert "[S9]" in text
    assert "[S9](https://" not in text, "不存在的编号不能链接到任何来源"


def test_link_citations_can_be_disabled_for_ablation(monkeypatch):
    monkeypatch.setenv("AGENT_LINK_CITATIONS", "0")
    text, _ = _sanitize_report("## 关键发现\n\n- 结论 [S1]\n", SOURCES, EVIDENCE)
    assert "[S1](https://real.example/a)" not in text
    assert "[S1]" in text


def test_report_sanitize_is_idempotent_for_clickable_citations():
    once, _ = _sanitize_report("## 关键发现\n\n- 结论 [S1]\n", SOURCES, EVIDENCE[:1])
    twice, _ = _sanitize_report(once, SOURCES, EVIDENCE[:1])
    assert twice.count("(https://real.example/a)") == once.count("(https://real.example/a)")
    assert "[[S1]" not in twice
