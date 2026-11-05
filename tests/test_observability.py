"""可观测性验收：token 用量/成本、耗时、证据表、日志链路。"""

from __future__ import annotations

import json

import pytest

import graph
from state import new_state


class _UsageLLM:
    mode = "openai"

    def __init__(self, usage: dict[str, int]) -> None:
        self.usage = dict(usage)

    def usage_snapshot(self) -> dict[str, int]:
        return dict(self.usage)


def test_finalize_computes_token_delta_cost_and_duration(tmp_path, monkeypatch):
    state = new_state("测试问题", trace_id="obs-1", log_dir=str(tmp_path / "logs"))
    state["llm_usage_baseline"] = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "calls": 1,
    }
    llm = _UsageLLM({"prompt_tokens": 110, "completion_tokens": 55, "total_tokens": 165, "calls": 2})
    monkeypatch.setattr(graph, "get_llm", lambda: llm)
    monkeypatch.setenv("AGENT_PRICE_INPUT_PER_MTOK", "1.0")
    monkeypatch.setenv("AGENT_PRICE_OUTPUT_PER_MTOK", "4.0")

    final = graph.finalize(state)["final_output"]

    assert final["token_usage"]["prompt_tokens"] == 100
    assert final["token_usage"]["completion_tokens"] == 50
    assert final["token_usage"]["calls"] == 1
    assert final["cost_cny"] == pytest.approx(100 / 1_000_000 + 50 * 4 / 1_000_000, abs=1e-9)
    assert final["duration_ms"] >= 0
    assert final["trace_id"] == "obs-1"


def test_finalize_writes_jsonl_log_and_summary(tmp_path):
    state = new_state("日志问题", trace_id="obs-2", log_dir=str(tmp_path / "logs"))
    final = graph.finalize(state)["final_output"]
    assert final["log_summary"]["total"] == 1
    log_path = tmp_path / "logs" / "research-obs-2.jsonl"
    assert log_path.exists(), "每次任务必须有可回放的 JSONL 日志"
    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    for field in (
        "step_id",
        "node_name",
        "tool_name",
        "input_summary",
        "output_summary",
        "status",
        "retry_count",
        "latency_ms",
        "error",
        "timestamp",
        "trace_id",
    ):
        assert field in entry


def test_render_evidence_table_has_source_summary_confidence():
    evidence = [
        {
            "claim": "结论 A",
            "source_url": "https://real.example/a",
            "source_snippet": "小节一",
            "confidence": 0.8,
        }
    ]
    text = "\n".join(graph.render_evidence_table(evidence))
    assert "| 编号 | 结论 / 摘要 | 来源 | 定位 | 可信度 |" in text
    assert "S1" in text and "https://real.example/a" in text and "0.8" in text
    assert "（无可用证据）" in "\n".join(graph.render_evidence_table([]))


def test_render_final_output_includes_evidence_table():
    final = {
        "research_question": "q",
        "status": "completed",
        "report": "## 摘要\n\n内容",
        "sources": [],
        "evidence": [
            {
                "claim": "结论 A",
                "source_url": "https://real.example/a",
                "source_snippet": "小节一",
                "confidence": 0.8,
            }
        ],
        "evidence_count": 1,
    }
    text = graph.render_final_output(final)
    assert "## 证据表" in text
    assert "https://real.example/a" in text
