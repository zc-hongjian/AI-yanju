"""可观测性：节点级 Span 记录 + 模型价目表成本归因。

两种数据分开存：

- ``logs/research-<trace_id>.jsonl``：原有的结构化日志（每步一条，面向排障）；
- ``logs/<trace_id>.trace.jsonl``：本模块新增的 Span 流（node 级耗时 / token / 成本），
  文件里第一类是 ``span``，最后一类是 ``summary``，供 ``GET /api/trace/{trace_id}`` 与
  ``GET /api/cost/summary`` 读取。

成本口径：

- 优先读 ``config/pricing.json``（按模型最长前缀匹配），单位：元 / 百万 token；
- 未命中的模型回退 ``AGENT_PRICE_INPUT_PER_MTOK`` / ``AGENT_PRICE_OUTPUT_PER_MTOK``，
  并在结果里标记 ``estimated=True``——简历/报表上必须能区分“真实价目”与“估算”。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PRICING = BASE_DIR / "config" / "pricing.json"
_CURRENCY = "CNY"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def trace_dir() -> Path:
    """Span 落盘目录：AGENT_TRACE_DIR → AGENT_LOG_DIR → logs/。"""
    raw = (os.getenv("AGENT_TRACE_DIR") or os.getenv("AGENT_LOG_DIR") or "").strip()
    return Path(raw) if raw else BASE_DIR / "logs"


def trace_path(trace_id: str) -> Path:
    safe = "".join(ch for ch in str(trace_id or "") if ch.isalnum() or ch in "-_") or "unknown"
    return trace_dir() / f"{safe}.trace.jsonl"


# ------------------------------------------------------------------ 价目表
def _fallback_prices() -> tuple[float, float]:
    try:
        price_in = float(os.getenv("AGENT_PRICE_INPUT_PER_MTOK", "1.0"))
        price_out = float(os.getenv("AGENT_PRICE_OUTPUT_PER_MTOK", "4.0"))
    except (TypeError, ValueError):
        price_in, price_out = 1.0, 4.0
    return price_in, price_out


def load_pricing(path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, float]]:
    """读价目表；文件缺失/损坏时返回空表（调用方自动回退 env 单价）。"""
    target = Path(path) if path else DEFAULT_PRICING
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    models = raw.get("models") if isinstance(raw, dict) else None
    table: dict[str, dict[str, float]] = {}
    for name, price in (models or {}).items():
        if not isinstance(price, dict):
            continue
        try:
            table[str(name).strip().lower()] = {
                "in": float(price.get("in", 0.0)),
                "out": float(price.get("out", 0.0)),
            }
        except (TypeError, ValueError):
            continue
    return table


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    pricing: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """按模型最长前缀匹配计价；未命中回退 env 单价并标记 estimated。"""
    table = load_pricing() if pricing is None else pricing
    name = str(model or "").strip().lower()
    matched = max((key for key in table if key and name.startswith(key)), key=len, default=None)
    if matched:
        price_in, price_out = table[matched]["in"], table[matched]["out"]
        source, estimated = matched, False
    else:
        price_in, price_out = _fallback_prices()
        source, estimated = "env-fallback", True
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cost = prompt_tokens / 1_000_000 * price_in + completion_tokens / 1_000_000 * price_out
    return {
        "cost_cny": round(cost, 8),
        "estimated": estimated,
        "currency": _CURRENCY,
        "price_source": source,
        "price_in_per_mtok": price_in,
        "price_out_per_mtok": price_out,
    }


# ------------------------------------------------------------------ Span 记录
class TraceRecorder:
    """单次运行的 Span 写入器；线程内顺序 append，文件级加锁。"""

    def __init__(self, trace_id: str, trace_path_override: str | os.PathLike[str] | None = None) -> None:
        self.trace_id = str(trace_id or "unknown")
        self.path = Path(trace_path_override) if trace_path_override else trace_path(self.trace_id)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def add_span(
        self,
        *,
        node: str,
        start: float,
        latency_ms: int,
        status: str,
        model: str = "",
        usage: dict[str, int] | None = None,
        error: str | None = None,
        attempt: int = 1,
    ) -> dict[str, Any]:
        usage = usage or {}
        cost = estimate_cost(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        span = {
            "type": "span",
            "trace_id": self.trace_id,
            "span_id": f"{node}-{time.time_ns()}",
            "node": node,
            "started_at": started_at,
            "latency_ms": int(latency_ms),
            "status": status,
            "attempt": int(attempt),
            "model": model,
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "calls": int(usage.get("calls", 0) or 0),
            "cost_cny": cost["cost_cny"],
            "cost_estimated": cost["estimated"],
            "error": error,
        }
        self._append(span)
        return span

    def add_summary(self, summary: dict[str, Any]) -> None:
        payload = {"type": "summary", "trace_id": self.trace_id, "finished_at": _now_iso()}
        payload.update(summary)
        self._append(payload)


# ------------------------------------------------------------------ 读取/聚合
def read_trace(trace_id: str) -> dict[str, Any] | None:
    """读回一次运行的 span 列表与 summary；文件不存在返回 None。"""
    path = trace_path(trace_id)
    if not path.exists():
        return None
    spans: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") == "span":
                spans.append(item)
            elif item.get("type") == "summary":
                summary = item
    except OSError:
        return None
    spans.sort(key=lambda span: (span.get("started_at") or "", span.get("span_id") or ""))
    return {"trace_id": str(trace_id), "spans": spans, "summary": summary, "span_count": len(spans)}


def aggregate_costs(days: int = 7) -> dict[str, Any]:
    """扫 trace 目录里的 summary/span，聚合调用次数、token、成本（按模型/按天）。"""
    days = max(1, min(int(days or 7), 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    directory = trace_dir()
    by_model: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    totals = {"runs": 0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "total_tokens": 0, "cost_cny": 0.0, "estimated_runs": 0}
    if not directory.exists():
        return {"days": days, "totals": totals, "by_model": [], "by_day": []}
    for path in sorted(directory.glob("*.trace.jsonl")):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        summary: dict[str, Any] = {}
        spans: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") == "summary":
                    summary = item
                elif item.get("type") == "span":
                    spans.append(item)
        except OSError:
            continue
        if not spans and not summary:
            continue
        totals["runs"] += 1
        totals["calls"] += int(summary.get("llm_calls") or sum(s.get("calls", 0) for s in spans))
        totals["prompt_tokens"] += int(summary.get("prompt_tokens") or sum(s.get("prompt_tokens", 0) for s in spans))
        totals["completion_tokens"] += int(
            summary.get("completion_tokens") or sum(s.get("completion_tokens", 0) for s in spans)
        )
        totals["total_tokens"] += int(summary.get("total_tokens") or sum(s.get("total_tokens", 0) for s in spans))
        totals["cost_cny"] += float(summary.get("cost_cny") or sum(s.get("cost_cny", 0) for s in spans))
        if summary.get("cost_estimated"):
            totals["estimated_runs"] += 1
        day = mtime.astimezone().date().isoformat()
        day_row = by_day.setdefault(day, {"date": day, "runs": 0, "total_tokens": 0, "cost_cny": 0.0})
        day_row["runs"] += 1
        day_row["total_tokens"] += int(summary.get("total_tokens") or 0)
        day_row["cost_cny"] += float(summary.get("cost_cny") or 0)
        for span in spans:
            model = str(span.get("model") or "offline/unknown")
            row = by_model.setdefault(
                model, {"model": model, "calls": 0, "total_tokens": 0, "cost_cny": 0.0, "spans": 0}
            )
            row["calls"] += int(span.get("calls", 0))
            row["total_tokens"] += int(span.get("total_tokens", 0))
            row["cost_cny"] += float(span.get("cost_cny", 0))
            row["spans"] += 1
    totals["cost_cny"] = round(totals["cost_cny"], 8)
    for row in by_model.values():
        row["cost_cny"] = round(row["cost_cny"], 8)
    for row in by_day.values():
        row["cost_cny"] = round(row["cost_cny"], 8)
    return {
        "days": days,
        "totals": totals,
        "by_model": sorted(by_model.values(), key=lambda row: row["cost_cny"], reverse=True),
        "by_day": sorted(by_day.values(), key=lambda row: row["date"]),
    }
