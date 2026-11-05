"""LangGraph 状态定义。

整个研究流程只在这一个 TypedDict 里流转。所有节点返回的是“局部更新”，
LangGraph 负责合并进 checkpoint。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, TypedDict

from logging_utils import now_iso


class LogEntry(TypedDict):
    """结构化日志条目，字段与需求一一对应。"""

    step_id: str
    node_name: str
    tool_name: str
    input_summary: str
    output_summary: str
    status: str
    retry_count: int
    latency_ms: int
    error: str | None
    timestamp: str
    trace_id: str


class SearchResult(TypedDict, total=False):
    title: str
    url: str
    snippet: str
    score: float
    query: str


class FetchedPage(TypedDict, total=False):
    url: str
    title: str
    content: str
    chars: int
    encoding: str
    dropped_blocks: int
    error: str | None


class EvidenceItem(TypedDict, total=False):
    claim: str
    evidence_text: str
    source_url: str
    quote: str
    confidence: float
    relevance_reason: str
    source_snippet: str


class ActionResult(TypedDict, total=False):
    action: str
    ok: bool
    status: str
    mode: str
    detail: dict[str, Any]
    error: str | None
    attempts: int


class ResearchState(TypedDict, total=False):
    # ---- 需求要求的核心字段 ----
    research_question: str
    plan: dict[str, Any]
    search_results: list[SearchResult]
    fetched_pages: list[FetchedPage]
    evidence: list[EvidenceItem]
    draft_report: str
    human_feedback: dict[str, Any]
    approved_actions: list[str]
    email_payload: dict[str, Any] | None
    notion_payload: dict[str, Any] | None
    logs: list[LogEntry]
    errors: list[dict[str, Any]]
    retry_counts: dict[str, int]
    final_output: dict[str, Any]

    # ---- 运行期辅助字段 ----
    trace_id: str
    started_at: str
    llm_mode: str
    llm_usage_baseline: dict[str, int]
    revision_count: int
    step_counter: int
    run_log_path: str
    action_results: list[ActionResult]
    evidence_note: str


def new_state(
    research_question: str,
    *,
    trace_id: str | None = None,
    log_dir: str | os.PathLike[str] | None = None,
) -> ResearchState:
    """构造一次运行的初始状态。trace_id 用于把日志串成一条链路。"""
    trace_id = trace_id or uuid.uuid4().hex[:8]
    log_root = Path(log_dir or os.getenv("AGENT_LOG_DIR", "logs"))
    return ResearchState(
        research_question=research_question.strip(),
        plan={},
        search_results=[],
        fetched_pages=[],
        evidence=[],
        draft_report="",
        human_feedback={},
        approved_actions=[],
        email_payload=None,
        notion_payload=None,
        logs=[],
        errors=[],
        retry_counts={},
        final_output={},
        trace_id=trace_id,
        started_at=now_iso(),
        llm_mode="",
        revision_count=0,
        step_counter=0,
        run_log_path=str(log_root / f"research-{trace_id}.jsonl"),
        action_results=[],
        evidence_note="",
    )
