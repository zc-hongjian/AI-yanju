"""trace span 的 started_at 必须是「开始时间」，否则面板时间线会出现重叠/负间隔。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import tracing


def test_span_started_at_is_start_time(tmp_path):
    path = tmp_path / "t.trace.jsonl"
    recorder = tracing.TraceRecorder("t", path)
    before = datetime.now(timezone.utc)
    recorder.add_span(node="draft_report", start=0.0, latency_ms=2500, status="ok", model="glm-4-flash")
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    started = datetime.fromisoformat(line["started_at"])
    elapsed = (before - started).total_seconds()
    assert 1.5 <= elapsed <= 4.0, f"started_at 应比记录时刻早约 2.5s，实际早 {elapsed:.2f}s"
    assert started.tzinfo is not None


def test_span_started_at_never_in_future(tmp_path):
    path = tmp_path / "t2.trace.jsonl"
    recorder = tracing.TraceRecorder("t2", path)
    recorder.add_span(node="finalize", start=0.0, latency_ms=0, status="ok")
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    started = datetime.fromisoformat(line["started_at"])
    # 0ms 的 span 允许与记录时刻相差几毫秒（时钟精度），但不能明显超前
    assert started <= datetime.now(timezone.utc) + timedelta(milliseconds=50)
