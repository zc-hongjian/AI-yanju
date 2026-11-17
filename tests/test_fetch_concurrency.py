"""并发抓取：并发度受控、结果按排名归并、达到页数即停、预算到点提前结束。"""

from __future__ import annotations

import threading
import time

import graph
import tools
from state import new_state


def _ok_result(url: str, *, cached: bool = False) -> tools.ToolResult:
    data = {"url": url, "title": url, "content": f"{url} 的正文" + "内容" * 80, "chars": 200}
    if cached:
        data["cached"] = True
    return tools.ToolResult(tool="fetch_page", ok=True, data=data, attempts=1, latency_ms=10)


def _state(urls: list[str], trace_id: str) -> dict:
    st = new_state("LangGraph 是什么？", trace_id=trace_id, log_dir=".tmp/pytest-logs")
    st["search_results"] = [
        {"url": url, "title": url, "snippet": "", "score": round(0.9 - index * 0.05, 2)}
        for index, url in enumerate(urls)
    ]
    st["plan"] = {"sub_questions": ["LangGraph 是什么"]}
    return st


def test_concurrency_env_is_clamped(monkeypatch):
    monkeypatch.setenv("AGENT_FETCH_CONCURRENCY", "0")
    assert graph._fetch_concurrency() == 1
    monkeypatch.setenv("AGENT_FETCH_CONCURRENCY", "99")
    assert graph._fetch_concurrency() == 6
    monkeypatch.setenv("AGENT_FETCH_CONCURRENCY", "abc")
    assert graph._fetch_concurrency() == 3
    monkeypatch.delenv("AGENT_FETCH_CONCURRENCY", raising=False)
    assert graph._fetch_concurrency() == 3


def test_batch_runs_in_parallel_and_preserves_order(monkeypatch):
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def fake_call(name, payload, human_approved=False):
        assert name == "fetch_page"
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return _ok_result(payload["url"])

    monkeypatch.setattr(tools, "call_tool", fake_call)
    batch = [{"url": f"https://site{i}.test/p", "host": f"site{i}.test", "title": ""} for i in range(4)]

    results = graph._fetch_batch(batch, workers=4)
    assert [item.data["url"] for item in results] == [item["url"] for item in batch]
    assert state["max"] >= 2, "并发度 >1 时应当有重叠请求"


def test_batch_with_workers_one_is_serial(monkeypatch):
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def fake_call(name, payload, human_approved=False):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.01)
        with lock:
            state["now"] -= 1
        return _ok_result(payload["url"])

    monkeypatch.setattr(tools, "call_tool", fake_call)
    batch = [{"url": f"https://site{i}.test/p", "host": f"site{i}.test", "title": ""} for i in range(3)]
    graph._fetch_batch(batch, workers=1)
    assert state["max"] == 1


def test_node_picks_same_pages_as_serial_and_stops_at_target(monkeypatch):
    urls = [f"https://site{i}.test/p" for i in range(6)]
    calls: list[str] = []

    def fake_call(name, payload, human_approved=False):
        calls.append(payload["url"])
        return _ok_result(payload["url"])

    monkeypatch.setattr(tools, "call_tool", fake_call)

    monkeypatch.setenv("AGENT_FETCH_CONCURRENCY", "1")
    serial = graph.fetch_pages_node(_state(urls, "trace-serial"))
    serial_urls = [page["url"] for page in serial["fetched_pages"]]
    serial_calls = len(calls)

    calls.clear()
    monkeypatch.setenv("AGENT_FETCH_CONCURRENCY", "3")
    parallel = graph.fetch_pages_node(_state(urls, "trace-parallel"))
    parallel_urls = [page["url"] for page in parallel["fetched_pages"]]

    assert serial_urls == parallel_urls, "并发不能改变最终选中的页面集合"
    assert len(serial_urls) == graph.MAX_PAGES
    assert serial_calls == len(serial_urls), "达到页数就停，不应多抓"
    assert len(parallel["errors"]) == 0


def test_node_reports_cached_hit(monkeypatch):
    url = "https://site0.test/p"

    def fake_call(name, payload, human_approved=False):
        return _ok_result(payload["url"], cached=True)

    monkeypatch.setattr(tools, "call_tool", fake_call)
    monkeypatch.setenv("AGENT_FETCH_CONCURRENCY", "1")
    result = graph.fetch_pages_node(_state([url], "trace-cached"))
    logs = result["logs"]
    assert any("命中缓存" in entry["output_summary"] for entry in logs)


def test_budget_exhausted_stops_before_fetching(monkeypatch):
    calls: list[str] = []

    def fake_call(name, payload, human_approved=False):
        calls.append(payload["url"])
        return _ok_result(payload["url"])

    monkeypatch.setattr(tools, "call_tool", fake_call)
    monkeypatch.setenv("AGENT_FETCH_NODE_BUDGET", "0")
    try:
        result = graph.fetch_pages_node(_state(["https://a.test/1", "https://b.test/2"], "trace-budget"))
    finally:
        monkeypatch.delenv("AGENT_FETCH_NODE_BUDGET", raising=False)
    assert calls == [], "预算已耗尽时不该再发起抓取"
    assert result["fetched_pages"] == []
    assert any(err.get("type") == "budget_exceeded" for err in result["errors"])
