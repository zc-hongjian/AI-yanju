"""抓取全失败时的降级链：过滤垃圾结果 → 搜索摘要做「摘要级证据」→ 报告显式标注。"""

from __future__ import annotations

import graph
import reporting
import tools
from state import new_state

QUESTION = "2027 年 AI Agent 在企业落地的主要挑战是什么？"
SNIPPETS = [
    (
        "https://blog.example/a",
        "企业部署 AI Agent 的首要挑战是数据治理与权限边界，超过六成企业卡在安全评审阶段。",
    ),
    (
        "https://cloud.example/b",
        "多步任务的失败率随步数上升，缺乏可观测性与回滚机制时，业务方不敢把关键流程交给 Agent 上线。",
    ),
    (
        "https://www.example/c",
        "推理成本随调用量线性增长，超过六成企业在概念验证阶段停滞，主要原因是无法证明 ROI。",
    ),
]


def _hit(url: str, snippet: str = "", *, score: float = 0.9) -> dict:
    return {"url": url, "title": url, "snippet": snippet, "score": score}


def _search_result(hits: list[dict]) -> tools.ToolResult:
    return tools.ToolResult(tool="search_web", ok=True, data=hits, attempts=1, latency_ms=5)


def _failed_fetch() -> tools.ToolResult:
    return tools.ToolResult(
        tool="fetch_page", ok=False, error="HTTP 521", attempts=1, latency_ms=5
    )


def _state(tmp_path, urls: list[dict]) -> dict:
    state = new_state(QUESTION, trace_id="snippet-test", log_dir=str(tmp_path / "logs"))
    state["plan"] = {"sub_questions": ["数据治理挑战", "流程可靠性挑战", "成本与 ROI 挑战"]}
    state["search_results"] = urls
    return state


def test_junk_search_pages_are_filtered(monkeypatch, tmp_path):
    junk = [
        _hit("https://tv.360kan.com/s?q=ai+agent"),
        _hit("https://ai.so.com/search/so123?search=x"),
        _hit("https://www.example/real"),
    ]

    def fake_call(name, payload, human_approved=False):
        assert name == "search_web"
        return _search_result(junk)

    monkeypatch.setattr(tools, "call_tool", fake_call)
    state = _state(tmp_path, [])
    state["plan"]["queries"] = [{"query": "2027 AI Agent 挑战", "max_results": 5}]
    updates = graph.search_web_node(state)
    urls = [hit["url"] for hit in updates["search_results"]]
    assert urls == ["https://www.example/real"]
    assert any("过滤" in entry["output_summary"] for entry in updates["logs"])


def test_fetch_falls_back_to_search_snippets(monkeypatch, tmp_path):
    def fake_call(name, payload, human_approved=False):
        assert name == "fetch_page"
        return _failed_fetch()

    monkeypatch.setattr(tools, "call_tool", fake_call)
    state = _state(tmp_path, [_hit(url, snippet) for url, snippet in SNIPPETS])
    updates = graph.fetch_pages_node(state)
    pages = updates["fetched_pages"]
    assert len(pages) == 3, "正文全失败时应降级为搜索摘要，而不是交白卷"
    assert all(page["snippet_only"] is True for page in pages)
    assert [page["url"] for page in pages] == [url for url, _ in SNIPPETS]
    assert any("降级使用" in entry["output_summary"] for entry in updates["logs"])


def test_no_snippets_keeps_honest_empty_result(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tools, "call_tool", lambda name, payload, human_approved=False: _failed_fetch()
    )
    state = _state(tmp_path, [_hit("https://blog.example/x", "")])
    updates = graph.fetch_pages_node(state)
    assert updates["fetched_pages"] == []


def test_partial_success_fills_remaining_slots_with_snippets(monkeypatch, tmp_path):
    real_call = tools.call_tool

    def fake_call(name, payload, human_approved=False):
        if name != "fetch_page":
            return real_call(name, payload, human_approved=human_approved)
        if payload["url"] == "https://ok.example/full":
            return tools.ToolResult(
                tool="fetch_page",
                ok=True,
                data={
                    "url": payload["url"],
                    "title": "可抓取正文",
                    "content": "企业落地 AI Agent 需要同时解决数据治理、流程可靠性与成本 ROI 三类问题。" * 3,
                    "chars": 120,
                },
                attempts=1,
                latency_ms=5,
            )
        return _failed_fetch()

    monkeypatch.setattr(tools, "call_tool", fake_call)
    state = _state(
        tmp_path,
        [_hit("https://ok.example/full")] + [_hit(url, snippet) for url, snippet in SNIPPETS],
    )
    pages = graph.fetch_pages_node(state)["fetched_pages"]
    full = [page for page in pages if not page.get("snippet_only")]
    snippets = [page for page in pages if page.get("snippet_only")]
    expected_fill = graph._page_target(state) - 1
    assert len(full) == 1 and len(snippets) == expected_fill, "正文页不足目标页数时应补入搜索摘要"
    assert any("补入" in entry["output_summary"] for entry in graph.fetch_pages_node(state)["logs"])


def test_snippet_pages_are_marked_in_evidence_and_report(monkeypatch, tmp_path):
    real_call = tools.call_tool

    def fake_call(name, payload, human_approved=False):
        if name == "fetch_page":
            return _failed_fetch()
        return real_call(name, payload, human_approved=human_approved)

    monkeypatch.setattr(tools, "call_tool", fake_call)
    state = _state(tmp_path, [_hit(url, snippet) for url, snippet in SNIPPETS])
    state["fetched_pages"] = graph.fetch_pages_node(state)["fetched_pages"]
    updates = graph.summarize_sources_node(state)
    evidence = updates["evidence"]
    assert evidence, "摘要级页面也应能提取出可用证据"
    assert all(item.get("snippet_only") for item in evidence)
    assert "搜索引擎摘要" in updates["evidence_note"]
    limits = "\n".join(reporting.limits_lines(evidence))
    assert "只来自搜索引擎摘要" in limits
