"""P0-2 可观测性测试：价目表、节点级 Span、按节点成本归因、Trace/成本 API。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import graph
import tracing
import web
from state import new_state


class _FakeLLM:
    mode = "openai"
    model = "gpt-4o-mini"

    def __init__(self) -> None:
        self.usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        }

    def usage_snapshot(self) -> dict[str, int]:
        return dict(self.usage)

    def burn(self, prompt: int, completion: int) -> None:
        self.usage["prompt_tokens"] += prompt
        self.usage["completion_tokens"] += completion
        self.usage["total_tokens"] += prompt + completion
        self.usage["calls"] += 1


def test_pricing_longest_prefix_and_env_fallback(monkeypatch):
    pricing = tracing.load_pricing()
    assert "glm-4-flash" in pricing

    # 最长前缀优先：glm-4-flash-0914 应命中 glm-4-flash，而不是更短的 glm-4
    flash = tracing.estimate_cost("glm-4-flash-0914", 1_000_000, 0, pricing=pricing)
    assert flash["estimated"] is False
    assert flash["price_source"] == "glm-4-flash"
    assert flash["cost_cny"] == pytest.approx(0.0)

    plus = tracing.estimate_cost("glm-4-plus", 0, 1_000_000, pricing=pricing)
    assert plus["cost_cny"] == pytest.approx(5.0)

    monkeypatch.setenv("AGENT_PRICE_INPUT_PER_MTOK", "1.0")
    monkeypatch.setenv("AGENT_PRICE_OUTPUT_PER_MTOK", "4.0")
    unknown = tracing.estimate_cost("some-local-model", 1_000_000, 1_000_000, pricing=pricing)
    assert unknown["estimated"] is True
    assert unknown["price_source"] == "env-fallback"
    assert unknown["cost_cny"] == pytest.approx(5.0)


def test_instrument_records_span_and_node_usage(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("AGENT_TRACE_DIR", str(logs))
    monkeypatch.setenv("AGENT_LOG_DIR", str(logs))
    fake = _FakeLLM()
    monkeypatch.setattr(graph, "get_llm", lambda: fake)

    def demo_node(state):
        fake.burn(120, 30)
        return {"evidence": []}

    state = new_state("演示问题", trace_id="trace-1", log_dir=str(logs))
    out = graph._instrument("demo", demo_node)(state)

    assert out["node_usage"]["demo"]["prompt_tokens"] == 120
    assert out["node_usage"]["demo"]["completion_tokens"] == 30
    assert out["node_usage"]["demo"]["calls"] == 1

    data = tracing.read_trace("trace-1")
    assert data is not None
    assert data["span_count"] == 1
    span = data["spans"][0]
    assert span["node"] == "demo"
    assert span["status"] == "ok"
    assert span["total_tokens"] == 150
    assert span["cost_estimated"] is False
    assert span["cost_cny"] == pytest.approx((120 * 1.1 + 30 * 4.4) / 1_000_000)


def test_finalize_attributes_cost_by_node(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("AGENT_TRACE_DIR", str(logs))
    fake = _FakeLLM()
    fake.burn(1_000_000, 0)
    monkeypatch.setattr(graph, "get_llm", lambda: fake)

    state = new_state("归因问题", trace_id="trace-2", log_dir=str(logs))
    state["node_usage"] = {
        "plan_research": {"prompt_tokens": 600_000, "completion_tokens": 0, "total_tokens": 600_000, "calls": 1},
        "draft_report": {"prompt_tokens": 400_000, "completion_tokens": 0, "total_tokens": 400_000, "calls": 1},
    }
    final = graph.finalize(state)["final_output"]

    assert final["token_usage_by_node"]["plan_research"]["prompt_tokens"] == 600_000
    assert final["cost_by_node"]["plan_research"] == pytest.approx(0.66)
    assert final["cost_by_node"]["draft_report"] == pytest.approx(0.44)
    assert final["cost_cny"] == pytest.approx(1.1)
    assert sum(final["cost_by_node"].values()) == pytest.approx(final["cost_cny"], abs=1e-6)
    assert final["cost_estimated"] is False
    assert final["model"] == "gpt-4o-mini"

    summary = tracing.read_trace("trace-2")["summary"]
    assert summary["status"].startswith("completed")
    assert summary["total_tokens"] == 1_000_000
    assert summary["token_usage_by_node"]["plan_research"]["prompt_tokens"] == 600_000


def test_trace_and_cost_endpoints(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("AGENT_TRACE_DIR", str(logs))
    recorder = tracing.TraceRecorder("web-trace")
    recorder.add_span(
        node="plan_research",
        start=0.0,
        latency_ms=12,
        status="ok",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500, "calls": 1},
    )
    recorder.add_summary(
        {
            "status": "completed",
            "duration_ms": 42,
            "total_tokens": 1500,
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "llm_calls": 1,
            "cost_cny": 0.0011,
            "cost_estimated": False,
            "model": "gpt-4o-mini",
        }
    )

    client = TestClient(web.app)
    trace_resp = client.get("/api/trace/web-trace")
    assert trace_resp.status_code == 200
    body = trace_resp.json()
    assert body["span_count"] == 1
    assert body["spans"][0]["node"] == "plan_research"
    assert body["summary"]["status"] == "completed"

    cost = client.get("/api/cost/summary?days=7").json()
    assert cost["totals"]["runs"] >= 1
    assert any(row["model"] == "gpt-4o-mini" for row in cost["by_model"])


def test_missing_trace_returns_404(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TRACE_DIR", str(tmp_path / "empty"))
    client = TestClient(web.app)
    resp = client.get("/api/trace/not-exist")
    assert resp.status_code == 404


def test_trace_file_is_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TRACE_DIR", str(tmp_path))
    recorder = tracing.TraceRecorder("jsonl-1")
    recorder.add_span(node="x", start=0.0, latency_ms=1, status="ok", usage={})
    recorder.add_summary({"status": "completed"})
    path = tmp_path / "jsonl-1.trace.jsonl"
    assert path.exists()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {line["type"] for line in lines} == {"span", "summary"}
