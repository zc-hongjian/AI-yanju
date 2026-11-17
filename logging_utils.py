"""结构化日志。

每条日志固定包含以下字段：
step_id, node_name, tool_name, input_summary, output_summary,
status, retry_count, latency_ms, error, timestamp（+ trace_id 用于串联链路）。

日志同时：
1) 追加进当前 state["logs"]（供 finalize 做摘要）；
2) 追加写入 logs/research-<trace_id>.jsonl（可离线按 step_id 排序回放）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

LOG_FIELDS = (
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
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def ensure_utf8_stdout() -> bool:
    """把 stdout 切到 UTF-8 编码。

    背景：Windows 默认 GBK 控制台打印 ``¥`` 或 emoji 会抛 ``UnicodeEncodeError``，
    而 ``finalize`` 正要打印含 ``¥`` 的报告摘要——一次正常的研究会因此被标成 failed。
    这里只做尽力而为的切换：已是 UTF-8 / 不支持 reconfigure / 平台拒绝都静默返回。
    """
    stream = sys.stdout
    encoding = (getattr(stream, "encoding", "") or "").lower()
    if encoding and "utf" in encoding:
        return True
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return False
    try:
        reconfigure(encoding="utf-8", errors="replace")
        return True
    except (AttributeError, OSError, ValueError):
        return False


# --------------------------------------------------------------- 实时日志订阅
# Web 版用它在一次 HTTP 请求还没结束时就把日志推给前端（NDJSON 流式响应）。
_LOG_SINKS: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
_LOG_SINKS_LOCK = threading.Lock()


def register_log_sink(trace_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
    with _LOG_SINKS_LOCK:
        _LOG_SINKS.setdefault(trace_id, []).append(callback)


def unregister_log_sink(trace_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
    with _LOG_SINKS_LOCK:
        sinks = _LOG_SINKS.get(trace_id)
        if not sinks:
            return
        if callback in sinks:
            sinks.remove(callback)
        if not sinks:
            _LOG_SINKS.pop(trace_id, None)


def emit_log_entry(entry: dict[str, Any]) -> None:
    trace_id = str(entry.get("trace_id") or "")
    if not trace_id:
        return
    with _LOG_SINKS_LOCK:
        sinks = list(_LOG_SINKS.get(trace_id, []))
    for sink in sinks:
        try:
            sink(entry)
        except Exception:  # noqa: BLE001 - 订阅者异常不能影响主流程
            continue


def summarize_text(value: Any, limit: int = 160) -> str:
    """把任意值压成一行短文本，避免把整页正文或密钥写进日志。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)] + "...(truncated)"


class StepLog:
    """在单个节点内收集日志，最后把 logs/step_counter 一起交回 state。"""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.counter = int(state.get("step_counter") or 0)
        self.logs: list[dict[str, Any]] = list(state.get("logs") or [])
        self.trace_id = str(state.get("trace_id") or "")
        self.path = state.get("run_log_path")

    def add(
        self,
        *,
        node_name: str,
        tool_name: str = "",
        input_summary: Any = "",
        output_summary: Any = "",
        status: str = "ok",
        retry_count: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
    ) -> dict[str, Any]:
        self.counter += 1
        entry = {
            "step_id": f"S{self.counter:03d}",
            "node_name": node_name,
            "tool_name": tool_name or "",
            "input_summary": summarize_text(input_summary),
            "output_summary": summarize_text(output_summary),
            "status": status,
            "retry_count": int(retry_count),
            "latency_ms": int(latency_ms),
            "error": summarize_text(error, 300) if error else None,
            "timestamp": now_iso(),
            "trace_id": self.trace_id,
        }
        self.logs.append(entry)
        _print_entry(entry)
        self._persist(entry)
        emit_log_entry(entry)
        return entry

    def updates(self, **extra: Any) -> dict[str, Any]:
        """把节点业务字段和日志字段合并成一次 state 更新。"""
        return {**extra, "logs": self.logs, "step_counter": self.counter}

    def _persist(self, entry: dict[str, Any]) -> None:
        if not self.path:
            return
        try:
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            # 日志落盘失败不应该让研究流程崩掉，但必须可见（否则 JSONL 会静默缺行）。
            print(f"[WARN] 结构化日志落盘失败（{self.path}）：{exc}", file=sys.stderr)


def _print_entry(entry: dict[str, Any]) -> None:
    if os.getenv("AGENT_QUIET_LOG") == "1":
        return
    tool = entry["tool_name"] or "-"
    line = (
        f"[{entry['step_id']}] {entry['node_name']:<22} {tool:<14} "
        f"{entry['status']:<10} {entry['latency_ms']:>5}ms  "
        f"{entry['input_summary']} -> {entry['output_summary']}"
    )
    if entry["error"]:
        line += f"  ERROR={entry['error']}"
    print(line, flush=True)


def log_stats(logs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    logs = list(logs)
    return {
        "total": len(logs),
        "errors": sum(1 for x in logs if x.get("status") in {"error", "failed"}),
        "degraded": sum(1 for x in logs if x.get("status") == "degraded"),
        "rejected": sum(1 for x in logs if x.get("status") == "rejected"),
        "retries": sum(int(x.get("retry_count") or 0) for x in logs),
        "latency_ms": sum(int(x.get("latency_ms") or 0) for x in logs),
    }


def render_log_summary(logs: Iterable[dict[str, Any]]) -> str:
    """把整条执行链路按 step_id 排成 markdown 表，便于验收第 5 条。"""
    logs = sorted(logs, key=lambda x: str(x.get("step_id", "")))
    lines = [
        "| step_id | node_name | tool_name | status | retry | latency_ms | error |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in logs:
        error = entry.get("error") or ""
        if len(error) > 80:
            error = error[:77] + "..."
        lines.append(
            "| {step_id} | {node_name} | {tool_name} | {status} | {retry_count} | "
            "{latency_ms} | {error} |".format(
                step_id=entry.get("step_id", ""),
                node_name=entry.get("node_name", ""),
                tool_name=entry.get("tool_name") or "-",
                status=entry.get("status", ""),
                retry_count=entry.get("retry_count", 0),
                latency_ms=entry.get("latency_ms", 0),
                error=error.replace("|", "\\|"),
            )
        )
    return "\n".join(lines)
