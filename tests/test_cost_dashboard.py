"""成本看板：/api/cost/summary 的形状、边界与前端入口。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import web

INDEX = Path(__file__).resolve().parent.parent / "index.html"


def test_cost_summary_shape_and_clamp():
    client = TestClient(web.app)
    # aggregate_costs 里 `days or 7`：0 视为“没传”，走默认 7 天
    assert client.get("/api/cost/summary", params={"days": 0}).json()["days"] == 7
    assert client.get("/api/cost/summary", params={"days": -5}).json()["days"] == 1, "负数下限是 1"

    data = client.get("/api/cost/summary").json()
    assert set(data["totals"]) >= {
        "runs", "calls", "prompt_tokens", "completion_tokens", "total_tokens", "cost_cny", "estimated_runs",
    }
    assert isinstance(data["by_model"], list)
    assert isinstance(data["by_day"], list)

    assert client.get("/api/cost/summary", params={"days": 999}).json()["days"] == 90, "days 上限 90"


def test_cost_summary_aggregates_trace_files(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TRACE_DIR", str(tmp_path))
    trace = tmp_path / "unit-test.trace.jsonl"
    lines = [
        {
            "type": "summary", "llm_calls": 2, "prompt_tokens": 10,
            "completion_tokens": 5, "total_tokens": 15, "cost_cny": 0.01, "cost_estimated": True,
        },
        {
            "type": "span", "node": "draft_report", "model": "glm-4-flash",
            "calls": 1, "prompt_tokens": 8, "completion_tokens": 4,
            "total_tokens": 12, "cost_cny": 0.0055,
        },
    ]
    trace.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    client = TestClient(web.app)
    data = client.get("/api/cost/summary", params={"days": 7}).json()
    assert data["totals"]["runs"] >= 1
    assert data["totals"]["calls"] >= 2
    assert data["totals"]["total_tokens"] >= 15
    assert data["totals"]["estimated_runs"] >= 1
    models = {row["model"] for row in data["by_model"]}
    assert "glm-4-flash" in models
    assert all("date" in row for row in data["by_day"])


def test_frontend_has_cost_dashboard():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="cost-btn"' in html
    assert 'id="cost-modal"' in html
    assert 'id="cost-totals"' in html
    assert 'id="cost-days"' in html
    assert "/api/cost/summary" in html
