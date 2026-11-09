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
    # 多轮追问：上一轮结论摘要（由 web 层从父会话读取后注入，长度受控）
    history_context: str
    parent_thread_id: str
    llm_mode: str
    llm_usage_baseline: dict[str, int]
    revision_count: int
    step_counter: int
    run_log_path: str
    action_results: list[ActionResult]
    evidence_note: str

    # ---- 研究质量闭环（P0） ----
    # 子问题覆盖度：见 quality.compute_coverage()
    coverage: list[dict[str, Any]]
    coverage_summary: dict[str, Any]
    # 证据断言校验：见 quality.verify_evidence()
    verification: dict[str, Any]
    # 检索轮次：0=首轮；1=已做一次补检。补检由 check_coverage 节点决定。
    retrieval_round: int
    gap_queries: list[dict[str, Any]]
    # 本地知识库召回片段（RAG）：见 rag.search_knowledge()
    kb_hits: list[dict[str, Any]]

    # ---- 报告质量闭环：大纲 → 分节写作 → 引用校验（见 reporting.py） ----
    # 报告大纲：sections（标题/目标/证据编号）+ uncovered（无证据子问题）
    outline: dict[str, Any]
    # 未清洗的原始草稿：保留 [S#] 角标，供 draft_review 校验与重写
    draft_raw: str
    # 草稿校验结果：issues / passed / rewrites / fixes
    draft_review: dict[str, Any]

    # ---- 可观测性：节点级 token 用量（由 graph._instrument 写入） ----
    node_usage: dict[str, dict[str, int]]


def new_state(
    research_question: str,
    *,
    trace_id: str | None = None,
    log_dir: str | os.PathLike[str] | None = None,
    history_context: str = "",
    parent_thread_id: str = "",
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
        history_context=(history_context or "").strip()[:1200],
        parent_thread_id=(parent_thread_id or "").strip(),
        llm_mode="",
        revision_count=0,
        step_counter=0,
        run_log_path=str(log_root / f"research-{trace_id}.jsonl"),
        action_results=[],
        evidence_note="",
        coverage=[],
        coverage_summary={},
        verification={},
        retrieval_round=0,
        gap_queries=[],
        kb_hits=[],
        outline={},
        draft_raw="",
        draft_review={},
        node_usage={},
    )
