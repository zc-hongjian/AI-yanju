"""耗时拆分：总耗时 = 节点计算耗时 + 人工确认等待；trace 缺失时不编造数据。"""

from __future__ import annotations

from datetime import datetime, timedelta

import graph
from state import new_state


def test_timing_split_subtracts_waits(monkeypatch):
    spans = [{"latency_ms": 90_000}, {"latency_ms": 80_900}, {"latency_ms": 0}]
    monkeypatch.setattr(graph.tracing, "read_trace", lambda _tid: {"spans": spans})
    compute, wait = graph._timing_split(200_000, "t")
    assert compute == 170_900
    assert wait == 29_100
    assert compute + wait == 200_000


def test_timing_split_without_trace_keeps_total(monkeypatch):
    monkeypatch.setattr(graph.tracing, "read_trace", lambda _tid: None)
    assert graph._timing_split(5000, "t") == (5000, 0)


def test_timing_split_never_returns_negative_wait(monkeypatch):
    monkeypatch.setattr(graph.tracing, "read_trace", lambda _tid: {"spans": [{"latency_ms": 9000}]})
    assert graph._timing_split(5000, "t") == (5000, 0)


def test_finalize_exposes_split_fields(monkeypatch, tmp_path):
    state = new_state("测试问题", trace_id="split-test", log_dir=str(tmp_path / "logs"))
    state["started_at"] = (
        datetime.now().astimezone() - timedelta(seconds=3)
    ).isoformat(timespec="milliseconds")
    state["evidence"] = [{"claim": "c", "source_url": "https://e.test/a", "quote": "q"}]
    final = graph.finalize(state)["final_output"]
    assert final["duration_ms"] >= 2500
    assert final["compute_ms"] + final["hitl_wait_ms"] == final["duration_ms"]
    assert final["hitl_wait_ms"] == 0  # 没有 trace 时不能编造等待时间
    assert "cost_note" in final


def test_finalize_marks_free_model_cost(monkeypatch, tmp_path):
    class _FakeLLM:
        model = "glm-4-flash"

        def usage_snapshot(self):
            return {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500, "calls": 1}

    monkeypatch.setattr(graph, "get_llm", lambda: _FakeLLM())
    state = new_state("测试问题", trace_id="free-model", log_dir=str(tmp_path / "logs"))
    state["evidence"] = [{"claim": "c", "source_url": "https://e.test/a", "quote": "q"}]
    final = graph.finalize(state)["final_output"]
    assert final["cost_cny"] == 0
    assert final["cost_note"] == "免费模型（价目表按 0 元计价）"


def test_duration_text_formatting():
    from web import _duration_text

    assert _duration_text(0) == "0 ms"
    assert _duration_text(23_800) == "23.8 秒"
    assert _duration_text(419_302) == "6 分 59 秒"
    assert _duration_text(None) == "0 ms"
