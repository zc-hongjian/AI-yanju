"""LangGraph 图定义：12 个节点 + 三处人工中断 + 条件边 + checkpoint。

流程：
receive_question → plan_research → search_web → fetch_pages → retrieve_knowledge
→ summarize_sources → check_coverage ─┬─(覆盖不足)→ search_web（补检一轮）
                                      └─(覆盖达标/无补检预算)→ draft_report
→ draft_report → human_review_draft ─┬─(通过)→ human_confirm_actions → execute_actions → finalize
                                     ├─(修改)→ draft_report
                                     └─(终止)→ finalize

human_review_draft 与 human_confirm_actions 内部共调用三次 interrupt()：
草稿审核、发邮件前确认、保存 Notion 前确认。
编译时挂上 checkpointer，invoke 返回 "__interrupt__"，用 Command(resume=...) 恢复。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import prompts
import rag
import tools
import tracing
from llm_client import get_llm
from logging_utils import StepLog, log_stats, now_iso, summarize_text
from quality import compute_coverage, coverage_terms, gap_queries_for, verify_evidence
from state import ResearchState

MAX_PAGES = int(os.getenv("AGENT_MAX_PAGES", "3"))
MAX_QUERIES = int(os.getenv("AGENT_MAX_QUERIES", "3"))
MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "2"))

# ---- 研究质量闭环（P0）：覆盖 → 补检 → 校验 ----
# 最多检索几轮（含首轮）。默认 2：首轮之外最多补检 1 轮，避免网络成本失控。
MAX_RETRIEVAL_ROUNDS = max(1, int(os.getenv("AGENT_MAX_RETRIEVAL_ROUNDS", "2")))
# 子问题覆盖率低于该阈值时触发补检
COVERAGE_MIN_RATE = float(os.getenv("AGENT_COVERAGE_MIN", "0.75"))
# 单个子问题算“已覆盖”的分数阈值
COVERAGE_THRESHOLD = float(os.getenv("AGENT_COVERAGE_THRESHOLD", "0.6"))
# 一次补检最多生成几条 query
MAX_GAP_QUERIES = max(1, int(os.getenv("AGENT_MAX_GAP_QUERIES", "2")))
# 抓取页数硬上限（自适应目标不会超过它）
MAX_PAGES_HARD = max(MAX_PAGES, int(os.getenv("AGENT_MAX_PAGES_HARD", "6")))

COMPARISON_RE = re.compile(r"对比|比较|区别|差异|优劣|优缺点|哪个更|vs\b|VS\b", re.I)
MULTIHOP_RE = re.compile(r"为什么|如何|怎么|步骤|流程|机制|原理|分别|哪些因素|前置条件", re.I)
OFFICIAL_HOST_HINTS = (
    "langchain.com",
    "langchain.cn",
    "langgraph.com.cn",
    "github.com",
    "wikipedia.org",
    "openai.com",
    "docs.",
    ".gov",
    ".edu",
)

URL_RE = re.compile(r"(?i)(?:https?|ftp)://[^\s)\]\"'<>，。；]+")
MD_LINK_RE = re.compile(r"(?i)\[([^\]\n]+)\]\(((?:https?|ftp)://[^)\s]+)\)")
SOURCE_TITLES = {"来源清单", "参考来源", "来源", "references", "sources", "reference"}
_HEADING_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+(?P<h>.+?)|[ \t]*\*\*(?P<b>.+?)\*\*)[ \t]*:?[ \t]*$"
)


def _strip_source_sections(text: str) -> str:
    """删除模型自带的来源章节（兼容 h1~h6 / 加粗 / 冒号 / CRLF 等写法）。"""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            title = (match.group("h") or match.group("b") or "").strip().rstrip(":：").strip().lower()
            if title in SOURCE_TITLES:
                skipping = True
                continue
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def _elapsed_ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


def _usage_delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "calls")
    return {key: max(0, int(after.get(key, 0)) - int(before.get(key, 0))) for key in keys}


def _trace_recorder(state: ResearchState) -> tracing.TraceRecorder:
    """trace 与 JSONL 日志同目录：优先跟随本次运行的 run_log_path。"""
    trace_id = str(state.get("trace_id") or "unknown")
    run_log = str(state.get("run_log_path") or "")
    if run_log:
        target = Path(run_log).with_name(f"{trace_id}.trace.jsonl")
        return tracing.TraceRecorder(trace_id, target)
    return tracing.TraceRecorder(trace_id)


def _instrument(name: str, fn: Any) -> Any:
    """给每个节点套一层：记录耗时 / token / 成本 / 错误，写入 trace 文件。

    - 不改变节点语义：返回的 partial update 原样透传，只额外带 ``node_usage``；
    - 遇到 ``interrupt()`` 抛出的 GraphBubbleUp 直接放行（暂停中的节点不记 span，
      恢复时节点会重跑，避免同一次中断产生两条互相矛盾的记录）；
    - 节点抛普通异常时先记 error span 再抛出，保证 trace 里能看到失败位置。
    """

    def wrapped(state: ResearchState) -> dict[str, Any]:
        llm = get_llm()
        before = _llm_usage(llm)
        start = time.perf_counter()
        try:
            out = fn(state) or {}
        except GraphBubbleUp:
            raise
        except Exception as exc:  # noqa: BLE001 - 记录后原样抛出，由 LangGraph 处理
            _trace_recorder(state).add_span(
                node=name,
                start=start,
                latency_ms=_elapsed_ms(start),
                status="error",
                model=str(getattr(llm, "model", "") or ""),
                usage=_usage_delta(_llm_usage(llm), before),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        usage = _usage_delta(_llm_usage(llm), before)
        merged = dict(state.get("node_usage") or {})
        merged[name] = usage
        out = dict(out) if isinstance(out, dict) else {}
        out["node_usage"] = merged
        _trace_recorder(state).add_span(
            node=name,
            start=start,
            latency_ms=_elapsed_ms(start),
            status="ok",
            model=str(getattr(llm, "model", "") or ""),
            usage=usage,
        )
        return out

    return wrapped


def _llm_usage(llm: Any) -> dict[str, int]:
    """取 LLM token 用量快照；测试桩可能没有该接口，返回空字典。"""
    snapshot = getattr(llm, "usage_snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        return dict(snapshot())
    except Exception:  # noqa: BLE001 - 统计失败不能影响主流程
        return {}


def _sources_from_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for page in pages:
        url = str(page.get("url") or "")
        if not url:
            continue
        item: dict[str, Any] = {"url": url, "title": str(page.get("title") or "")}
        if page.get("is_kb"):
            item["is_kb"] = True
        sources.append(item)
    return sources


def _page_target(state: ResearchState) -> int:
    """按问题类型与检索轮次自适应抓取页数（仍受 MAX_PAGES_HARD 约束）。

    对比/多跳类问题天然需要更多来源，首轮 3 页经常只够覆盖一方；
    补检轮再 +1，但最多 MAX_PAGES_HARD 页，避免无限膨胀。
    """
    question = str(state.get("research_question") or "")
    sub_questions = " ".join(str(q) for q in (state.get("plan") or {}).get("sub_questions") or [])
    text = f"{question} {sub_questions}"
    target = MAX_PAGES
    if COMPARISON_RE.search(text):
        target += 1
    if MULTIHOP_RE.search(text):
        target += 1
    target += min(1, int(state.get("retrieval_round") or 0))
    return max(1, min(MAX_PAGES_HARD, target))


def _rank_bonus(hit: dict[str, Any], terms: list[str]) -> float:
    """检索结果重排：问题词命中 + 官方/文档站点先验，避免完全按搜索引擎顺序抓。"""
    title = str(hit.get("title") or "")
    snippet = str(hit.get("snippet") or "")
    url = str(hit.get("url") or "")
    text = f"{title} {snippet}".lower()
    bonus = 0.0
    if terms:
        matched = sum(1 for term in terms if term in text)
        bonus += min(0.3, 0.05 * matched)
    host = (urlparse(url).hostname or "").lower()
    if any(hint in host for hint in OFFICIAL_HOST_HINTS):
        bonus += 0.15
    if url.lower().endswith(".pdf"):
        bonus -= 0.1  # PDF 抓取失败率更高，同等条件下稍后尝试
    return bonus


def _content_fingerprint(content: str) -> str:
    """内容级去重指纹：归一化后取前 600 字符哈希，识别转载/镜像/重复正文。"""
    compact = re.sub(r"\s+", "", str(content or ""))[:600]
    if not compact:
        return ""
    return hashlib.md5(compact.encode("utf-8", errors="ignore")).hexdigest()


def _merge_hits(existing: list[dict[str, Any]], new_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 URL 合并两轮检索结果；保留首轮顺序与原始 score。"""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in [*existing, *new_hits]:
        url = str(hit.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(dict(hit))
    return merged


# ================================================================ 节点 1：接收问题
def receive_question(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    question = str(state.get("research_question") or "").strip()
    if not question:
        raise ValueError("research_question 不能为空")
    llm = get_llm()
    history = str(state.get("history_context") or "").strip()
    origin = "承接上一轮追问" if history else "首轮研究"
    sl.add(
        node_name="receive_question",
        input_summary=question,
        output_summary=(
            f"初始化完成（{origin}）；LLM 模式={llm.mode}；trace={state.get('trace_id')}"
        ),
        status="ok",
    )
    return sl.updates(
        research_question=question,
        llm_mode=llm.mode,
        llm_usage_baseline=_llm_usage(llm),
    )


# ================================================================ 节点 2：检索计划
def plan_research(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    question = str(state.get("research_question") or "")
    llm = get_llm()
    history = str(state.get("history_context") or "").strip()
    errors = list(state.get("errors") or [])
    search_schema = [s for s in tools.TOOL_SCHEMAS if s["function"]["name"] == "search_web"]
    start = time.perf_counter()
    status, error = "ok", None
    try:
        plan = llm.plan_research(question, tools=search_schema, context=history)
    except Exception as exc:  # noqa: BLE001 - 规划失败要降级而不是崩
        plan = {
            "queries": [{"query": question, "max_results": 5}],
            "focus": question,
            "rationale": "LLM 规划失败，降级为直接检索原问题",
            "provider": "fallback",
        }
        status, error = "degraded", f"检索规划失败：{exc}"
        errors.append({"node": "plan_research", "type": "llm_error", "error": str(exc)})
    latency = _elapsed_ms(start)
    queries = plan.get("queries") or []
    sub_questions = plan.get("sub_questions") or []
    sl.add(
        node_name="plan_research",
        input_summary=question,
        output_summary=(
            f"子问题 {len(sub_questions)} 个；{len(queries)} 条查询；"
            f"provider={plan.get('provider')}；{summarize_text(queries, 60)}"
        ),
        status=status,
        latency_ms=latency,
        error=error,
    )
    return sl.updates(plan=plan, errors=errors)


# ================================================================ 节点 3：搜索网页
def search_web_node(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    plan = state.get("plan") or {}
    existing_results = list(state.get("search_results") or [])
    gap_queries = list(state.get("gap_queries") or [])
    round_no = int(state.get("retrieval_round") or 0)
    if gap_queries:
        # 第二轮：只搜 check_coverage 指出的缺口，不再重复首轮 query
        queries = gap_queries[:MAX_GAP_QUERIES]
    else:
        queries = list(plan.get("queries") or [])[:MAX_QUERIES]
    if not queries:
        queries = [{"query": state.get("research_question", ""), "max_results": 5}]
    results: list[dict[str, Any]] = []
    errors = list(state.get("errors") or [])
    retry_counts = dict(state.get("retry_counts") or {})
    search_budget = float(os.getenv("AGENT_SEARCH_NODE_BUDGET", "40"))
    started = time.perf_counter()
    budget_hit = False
    for item in queries:
        if time.perf_counter() - started > search_budget:
            budget_hit = True
            break
        query = str((item or {}).get("query") or "").strip()
        if not query:
            continue
        try:
            max_results = int((item or {}).get("max_results") or 5)
        except (TypeError, ValueError):
            max_results = 5
        result = tools.call_tool("search_web", {"query": query, "max_results": max_results})
        retry_counts["search_web"] = retry_counts.get("search_web", 0) + max(0, result.attempts - 1)
        if result.ok:
            hits = list(result.data or [])
            results.extend(hits)
            sl.add(
                node_name="search_web",
                tool_name="search_web",
                input_summary=f"[第{round_no + 1}轮] {query}",
                output_summary=f"{len(hits)} 条结果",
                status="ok",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
            )
        else:
            errors.append(
                {
                    "node": "search_web",
                    "tool": "search_web",
                    "type": "tool_error",
                    "error": result.error or "未知错误",
                    "attempts": result.attempts,
                }
            )
            sl.add(
                node_name="search_web",
                tool_name="search_web",
                input_summary=f"[第{round_no + 1}轮] {query}",
                output_summary=result.summary(),
                status="error",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
                error=result.error,
            )
    if budget_hit:
        errors.append(
            {
                "node": "search_web",
                "type": "budget_exceeded",
                "error": f"检索总耗时超过 {search_budget:.0f}s，提前结束",
            }
        )
        sl.add(
            node_name="search_web",
            tool_name="search_web",
            input_summary=f"{len(queries)} 条查询",
            output_summary=f"检索总耗时超过 {search_budget:.0f}s，提前结束（已拿到 {len(results)} 条结果）",
            status="degraded",
            error=f"检索时间预算 {search_budget:.0f}s 耗尽",
        )
    deduped = _merge_hits(existing_results, results)
    if not deduped:
        sl.add(
            node_name="search_web",
            tool_name="search_web",
            input_summary=f"{len(queries)} 条查询",
            output_summary="没有任何可用结果，进入“无来源”降级路径",
            status="degraded",
            error="检索未返回任何结果",
        )
    return sl.updates(
        search_results=deduped,
        gap_queries=[],  # 本轮已消费；下一轮由 check_coverage 重新生成
        errors=errors,
        retry_counts=retry_counts,
    )


# ================================================================ 节点 4：抓正文
def fetch_pages_node(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    existing_pages = list(state.get("fetched_pages") or [])
    fetched_urls = {str(page.get("url") or "") for page in existing_pages}
    seen_fingerprints = {_content_fingerprint(str(page.get("content") or "")) for page in existing_pages}
    seen_fingerprints.discard("")

    plan = state.get("plan") or {}
    terms = coverage_terms(
        f"{state.get('research_question') or ''} " + " ".join(str(q) for q in plan.get("sub_questions") or [])
    )
    candidates = sorted(
        (
            hit
            for hit in state.get("search_results") or []
            if str(hit.get("url") or "").strip() not in fetched_urls
        ),
        key=lambda hit: float(hit.get("score") or 0) + _rank_bonus(hit, terms),
        reverse=True,
    )
    pages: list[dict[str, Any]] = list(existing_pages)
    page_target = _page_target(state)
    errors = list(state.get("errors") or [])
    retry_counts = dict(state.get("retry_counts") or {})
    fetch_budget = float(os.getenv("AGENT_FETCH_NODE_BUDGET", "45"))
    try:
        max_attempts = max(1, int(os.getenv("AGENT_FETCH_MAX_ATTEMPTS", "8")))
    except ValueError:
        max_attempts = 8
    started = time.perf_counter()
    attempts = 0
    stop_reason: str | None = None
    failed_hosts: set[str] = set()
    skipped_same_host = 0
    skipped_duplicate = 0
    for hit in candidates:
        if len(pages) >= page_target:
            break
        if attempts >= max_attempts:
            stop_reason = f"尝试 {attempts} 个 URL 后停止（上限 {max_attempts} 个）"
            break
        if time.perf_counter() - started > fetch_budget:
            stop_reason = f"抓取总耗时超过 {fetch_budget:.0f}s，提前结束"
            break
        url = str(hit.get("url") or "").strip()
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower()
        if host and host in failed_hosts:
            # 同一域名第一次已经失败（403 / SSL / JS 空页），后续同域候选直接跳过，
            # 把有限的抓取次数留给其他域名。
            skipped_same_host += 1
            continue
        attempts += 1
        result = tools.call_tool("fetch_page", {"url": url})
        retry_counts["fetch_page"] = retry_counts.get("fetch_page", 0) + max(0, result.attempts - 1)
        if result.ok:
            page = dict(result.data or {})
            page["search_title"] = str(hit.get("title") or "")
            fingerprint = _content_fingerprint(str(page.get("content") or ""))
            if fingerprint and fingerprint in seen_fingerprints:
                skipped_duplicate += 1
                sl.add(
                    node_name="fetch_pages",
                    tool_name="fetch_page",
                    input_summary=url,
                    output_summary="正文与已抓取页面重复（转载/镜像），跳过",
                    status="skipped",
                    retry_count=max(0, result.attempts - 1),
                    latency_ms=result.latency_ms,
                )
                continue
            if fingerprint:
                seen_fingerprints.add(fingerprint)
            pages.append(page)
            encoding = str(page.get("encoding") or "未知")
            dropped_blocks = int(page.get("dropped_blocks") or 0)
            detail = f"抓到正文 {page.get('chars', 0)} 字符（编码 {encoding}"
            detail += f"，丢弃乱码 {dropped_blocks} 段）" if dropped_blocks else "）"
            sl.add(
                node_name="fetch_pages",
                tool_name="fetch_page",
                input_summary=url,
                output_summary=detail,
                status="ok",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
            )
        else:
            if host:
                failed_hosts.add(host)
            errors.append(
                {
                    "node": "fetch_pages",
                    "tool": "fetch_page",
                    "type": "tool_error",
                    "error": result.error or "未知错误",
                    "url": url,
                    "attempts": result.attempts,
                }
            )
            sl.add(
                node_name="fetch_pages",
                tool_name="fetch_page",
                input_summary=url,
                output_summary=result.summary(),
                status="error",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
                error=result.error,
            )
    if stop_reason:
        errors.append({"node": "fetch_pages", "type": "budget_exceeded", "error": stop_reason})
        sl.add(
            node_name="fetch_pages",
            input_summary=f"已尝试 {attempts} 个 URL",
            output_summary=f"{stop_reason}；本轮页数 {len(pages) - len(existing_pages)}，累计 {len(pages)} 页",
            status="degraded",
            error=stop_reason,
        )
    if skipped_same_host:
        sl.add(
            node_name="fetch_pages",
            input_summary=f"跳过 {skipped_same_host} 个同域候选",
            output_summary="同一域名首次抓取失败后，后续同域候选不再重复尝试",
            status="skipped",
        )
    if skipped_duplicate:
        sl.add(
            node_name="fetch_pages",
            input_summary=f"跳过 {skipped_duplicate} 个重复正文",
            output_summary="按内容指纹去重：同一篇正文的转载/镜像不再进入证据",
            status="skipped",
        )
    if not candidates:
        sl.add(
            node_name="fetch_pages",
            input_summary=f"累计候选 {len(state.get('search_results') or [])} 个 URL",
            output_summary="没有新的候选 URL 可抓取（本轮全部是已抓取页面）",
            status="skipped",
        )
    if not pages:
        sl.add(
            node_name="fetch_pages",
            input_summary=f"{len(candidates)} 个候选 URL",
            output_summary="没有成功抓取任何页面，后续报告将标注“未找到可靠来源”",
            status="degraded",
            error="全部页面抓取失败",
        )
    return sl.updates(fetched_pages=pages, errors=errors, retry_counts=retry_counts)


# ================================================================ 节点 4.5：本地知识库检索（RAG）
def retrieve_knowledge(state: ResearchState) -> dict[str, Any]:
    """混合检索本地知识库，把命中片段作为 ``kb://`` 来源追加进 fetched_pages。

    这样下游的证据抽取、引用编号、URL 白名单、支持性校验全部复用，
    不需要为知识库单开一条证据链路。
    """
    sl = StepLog(state)
    question = str(state.get("research_question") or "")
    plan = state.get("plan") or {}
    queries = [str((item or {}).get("query") or "").strip() for item in plan.get("queries") or []]
    queries = [query for query in queries if query][:3] + [question]
    limit = max(1, int(os.getenv("AGENT_KB_TOP_K", "6")))
    pages = list(state.get("fetched_pages") or [])
    start = time.perf_counter()
    try:
        hits = rag.search_knowledge(queries, k=limit)
    except Exception as exc:  # noqa: BLE001 - 知识库故障不能拖垮研究主流程
        sl.add(
            node_name="retrieve_knowledge",
            input_summary=f"{len(queries)} 条查询",
            output_summary=f"知识库检索失败，跳过：{exc}",
            status="error",
            latency_ms=_elapsed_ms(start),
            error=str(exc),
        )
        return sl.updates(fetched_pages=pages, kb_hits=[])
    stats = rag.knowledge_stats()
    if not hits:
        sl.add(
            node_name="retrieve_knowledge",
            input_summary=f"{stats['documents']} 篇文档 / {stats['chunks']} 个片段",
            output_summary="知识库为空或未命中，跳过（不影响联网检索）",
            status="skipped",
            latency_ms=_elapsed_ms(start),
        )
        return sl.updates(fetched_pages=pages, kb_hits=[])
    existing = {str(page.get("url") or "") for page in pages}
    added: list[dict[str, Any]] = []
    for hit in hits:
        url = str(hit.get("url") or "")
        if not url or url in existing:
            continue
        existing.add(url)
        heading = str(hit.get("heading") or "").strip()
        title = str(hit.get("title") or hit.get("doc_id") or "")
        if heading:
            title += f" · {heading}"
        text = str(hit.get("text") or "")
        added.append(
            {
                "url": url,
                "title": title,
                "content": text,
                "chars": len(text),
                "encoding": "utf-8",
                "is_kb": True,
                "kb_doc_id": hit.get("doc_id"),
                "kb_chunk_id": hit.get("chunk_id"),
                "score": hit.get("score"),
                "error": None,
            }
        )
    sl.add(
        node_name="retrieve_knowledge",
        tool_name="knowledge_search",
        input_summary=f"{len(queries)} 条查询；{stats['documents']} 篇文档",
        output_summary=(
            f"命中 {len(hits)} 个片段（{hits[0].get('retriever')}），新增 {len(added)} 个知识库来源"
        ),
        status="ok" if added else "degraded",
        latency_ms=_elapsed_ms(start),
    )
    return sl.updates(fetched_pages=pages + added, kb_hits=hits)


# ================================================================ 节点 5：总结证据
def _kb_evidence_items(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """知识库片段直接作为“检索到的原文证据”。

    网页证据走 LLM/启发式抽取；知识库片段本身已经是按相关性召回的短文本，
    再抽句会因句子过短（表格行、列表项）而丢证据，所以整段引用、按检索分给置信度。
    """
    items: list[dict[str, Any]] = []
    for page in pages:
        text = re.sub(r"\s+", " ", str(page.get("content") or "")).strip()
        if not text:
            continue
        score = float(page.get("score") or 0.0)
        items.append(
            {
                "claim": text[:300],
                "evidence_text": text[:300],
                "source_url": str(page.get("url") or ""),
                "quote": text[:400],
                "source_snippet": str(page.get("title") or "知识库片段")[:120],
                "relevance_reason": f"知识库检索命中（score={score:.3f}）",
                "confidence": round(min(0.9, 0.5 + min(0.35, score)), 2),
            }
        )
    return items


def summarize_sources_node(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    question = str(state.get("research_question") or "")
    pages = state.get("fetched_pages") or []
    errors = list(state.get("errors") or [])
    retry_counts = dict(state.get("retry_counts") or {})
    if not pages:
        sl.add(
            node_name="summarize_sources",
            input_summary="0 个来源",
            output_summary="无来源可总结；证据为空",
            status="degraded",
            error="没有成功抓取的网页",
        )
        return sl.updates(
            evidence=[],
            evidence_note="未找到可靠来源（没有成功抓取的网页正文）",
            verification={},
            errors=errors,
            retry_counts=retry_counts,
        )
    web_pages = [page for page in pages if not page.get("is_kb")]
    kb_pages = [page for page in pages if page.get("is_kb")]
    sources = [
        {
            "url": str(page.get("url") or ""),
            "title": str(page.get("title") or ""),
            "content": str(page.get("content") or ""),
        }
        for page in web_pages
    ]
    raw_evidence: list[dict[str, Any]] = []
    notes: list[str] = []
    mode = "none"
    if sources:
        result = tools.call_tool(
            "summarize_sources", {"sources": sources, "research_question": question}
        )
        retry_counts["summarize_sources"] = (
            retry_counts.get("summarize_sources", 0) + max(0, result.attempts - 1)
        )
        if result.ok:
            data = result.data or {}
            mode = str(data.get("mode") or "")
            raw_evidence.extend(list(data.get("evidence") or []))
            note = str(data.get("note") or "")
            if note:
                notes.append(note)
            sl.add(
                node_name="summarize_sources",
                tool_name="summarize_sources",
                input_summary=f"{len(sources)} 个网页来源",
                output_summary=(
                    f"网页证据 {len(raw_evidence)} 条；mode={mode}；{summarize_text(note, 60)}"
                ),
                status="ok" if raw_evidence else "degraded",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
                error=None if raw_evidence else (note or "未提取到可验证证据"),
            )
        else:
            notes.append(f"网页证据总结失败：{result.error}")
            errors.append(
                {
                    "node": "summarize_sources",
                    "tool": "summarize_sources",
                    "type": "tool_error",
                    "error": result.error or "未知错误",
                    "attempts": result.attempts,
                }
            )
            sl.add(
                node_name="summarize_sources",
                tool_name="summarize_sources",
                input_summary=f"{len(sources)} 个网页来源",
                output_summary=result.summary(),
                status="error",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
                error=result.error,
            )
    kb_items = _kb_evidence_items(kb_pages)
    if kb_items:
        notes.append(f"知识库证据 {len(kb_items)} 条")
        raw_evidence.extend(kb_items)
    # P0：claim 级校验——只保留“结论能被引文支持（或部分支持）”的证据，
    # 明显外推/无关的条目从证据列表移除并记录，避免进入报告。
    evidence, verification = verify_evidence(raw_evidence)
    note = "；".join(item for item in notes if item)
    if verification.get("dropped"):
        note = (note + f"；已过滤 {verification['dropped']} 条未被原文支持的结论").lstrip("；")
    if not evidence:
        errors.append(
            {"node": "summarize_sources", "type": "no_evidence", "error": note or "未提取到可验证证据"}
        )
    if sources or kb_pages:
        sl.add(
            node_name="summarize_sources",
            input_summary=f"{len(sources)} 个网页 + {len(kb_pages)} 个知识库片段",
            output_summary=(
                f"{len(evidence)}/{len(raw_evidence)} 条可用证据；"
                f"支持率={verification.get('support_rate', 0):.0%}；"
                f"知识库证据 {len(kb_items)} 条"
            ),
            status="ok" if evidence else "degraded",
            error=None if evidence else (note or "未提取到可验证证据"),
        )
    return sl.updates(
        evidence=evidence,
        evidence_note=note or "未找到可靠来源（没有可用的网页正文或知识库片段）",
        verification=verification,
        errors=errors,
        retry_counts=retry_counts,
    )


# ================================================================ 节点 5.5：覆盖检查 / 缺口补检
def check_coverage(state: ResearchState) -> dict[str, Any]:
    """检查证据对子问题的覆盖度；不足时生成补检 query，最多补一轮。

    这是 P0 的关键节点：把“问题拆解”从展示字段变成真正的检索驱动信号。
    """
    sl = StepLog(state)
    plan = state.get("plan") or {}
    sub_questions = [str(item).strip() for item in plan.get("sub_questions") or [] if str(item).strip()]
    evidence = list(state.get("evidence") or [])
    round_no = int(state.get("retrieval_round") or 0)
    if not sub_questions:
        sl.add(
            node_name="check_coverage",
            input_summary="0 个子问题",
            output_summary="没有可检查的子问题，跳过覆盖度评估",
            status="skipped",
        )
        return sl.updates(
            coverage=[],
            coverage_summary={"total": 0, "covered": 0, "rate": 0.0, "uncovered": []},
            gap_queries=[],
        )

    coverage, summary = compute_coverage(sub_questions, evidence, threshold=COVERAGE_THRESHOLD)
    rate = float(summary.get("rate") or 0.0)
    existing_queries = [str((item or {}).get("query") or "") for item in plan.get("queries") or []]
    can_gap = round_no + 1 < MAX_RETRIEVAL_ROUNDS and bool(evidence or state.get("fetched_pages"))
    gaps = (
        gap_queries_for(
            coverage,
            question=str(state.get("research_question") or ""),
            existing_queries=existing_queries,
            limit=MAX_GAP_QUERIES,
            max_results=6,
        )
        if can_gap and rate < COVERAGE_MIN_RATE
        else []
    )
    detail = f"覆盖 {summary.get('covered', 0)}/{summary.get('total', 0)} 子问题（{rate:.0%}）"
    if gaps:
        sl.add(
            node_name="check_coverage",
            input_summary=f"{len(sub_questions)} 个子问题；第 {round_no + 1} 轮检索后",
            output_summary=(
                f"{detail}，低于阈值 {COVERAGE_MIN_RATE:.0%}；"
                f"发起第 {round_no + 2} 轮补检：{summarize_text([g['query'] for g in gaps], 90)}"
            ),
            status="degraded",
            error="子问题覆盖不足，已触发补检",
        )
        return sl.updates(
            coverage=coverage,
            coverage_summary=summary,
            gap_queries=gaps,
            retrieval_round=round_no + 1,
        )

    sl.add(
        node_name="check_coverage",
        input_summary=f"{len(sub_questions)} 个子问题；第 {round_no + 1} 轮检索后",
        output_summary=(
            f"{detail}，达到阈值 {COVERAGE_MIN_RATE:.0%}"
            if rate >= COVERAGE_MIN_RATE
            else f"{detail}，未达阈值但已无补检轮次/可用 query"
        ),
        status="ok" if rate >= COVERAGE_MIN_RATE else "degraded",
    )
    return sl.updates(coverage=coverage, coverage_summary=summary, gap_queries=[])


def route_after_coverage(state: ResearchState) -> Literal["search_web", "draft_report"]:
    return "search_web" if state.get("gap_queries") else "draft_report"


# ================================================================ 节点 6：草稿
def _sanitize_report(
    draft: str,
    sources: list[dict[str, str]],
    evidence: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """出报告前的最后一道闸：URL 白名单清洗 + 来源清单强制重建。

    - 模型自带的“来源清单 / References”整段删除，统一按真实抓取结果重建；
    - 指向未抓取 URL 的 Markdown 链接降级为纯文本，裸 URL 替换为提示；
    - 正文里的 [S1] 角标按证据映射成可点击链接 [S1](URL)（可用 AGENT_LINK_CITATIONS=0 关闭）；
    - 无证据时强制写入“未找到可靠来源”。
    返回 (清洗后的报告, 被移除的 URL 列表)。
    """
    text = (draft or "").strip()
    known = {str(source.get("url") or "").rstrip("/").lower() for source in sources}
    dropped: list[str] = []

    text = _strip_source_sections(text)

    def _link_repl(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if url.rstrip("/").lower() in known:
            return match.group(0)
        dropped.append(url)
        return f"{label}（未验证链接已移除）"

    text = MD_LINK_RE.sub(_link_repl, text)

    def _url_repl(match: re.Match[str]) -> str:
        url = match.group(0)
        core = url.rstrip(".,;")
        if core.rstrip("/").lower() in known:
            return url
        dropped.append(core)
        return "（未验证链接已移除）"

    text = URL_RE.sub(_url_repl, text)

    # 引用角标 → 可点击来源：每条关键结论的 [S1] 直接链到对应来源，验收口径可点击
    if os.getenv("AGENT_LINK_CITATIONS", "1").strip().lower() not in {"0", "false", "no", "off"}:

        def _link_citation(match: re.Match[str]) -> str:
            index = int(match.group(1))
            if not 1 <= index <= len(evidence):
                return match.group(0)
            url = str(evidence[index - 1].get("source_url") or "").strip()
            # 知识库来源是 kb:// 伪 URL，不做成可点击链接（点击没有意义）
            if url.startswith("kb://"):
                return match.group(0)
            return f"[S{index}]({url})" if url and url.rstrip("/").lower() in known else match.group(0)

        text = re.sub(r"\[S(\d+)\](?!\()", _link_citation, text)

    if not evidence and "未找到可靠来源" not in text:
        text = "## 未找到可靠来源\n\n本次流程没有获得可验证的证据，**未找到可靠来源**。\n\n" + text

    if sources or evidence:
        lines = ["", "## 来源清单", ""]
        lines.extend(prompts.source_list_lines(evidence, sources))
        text = text.rstrip() + "\n" + "\n".join(lines)

    unique: list[str] = []
    for url in dropped:
        if url not in unique:
            unique.append(url)
    return text, unique


def draft_report(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    question = str(state.get("research_question") or "")
    evidence = list(state.get("evidence") or [])
    pages = state.get("fetched_pages") or []
    sources = _sources_from_pages(pages)
    feedback = state.get("human_feedback") or {}
    feedback_text = str(feedback.get("comment") or "") if feedback.get("decision") == "revise" else ""
    errors = list(state.get("errors") or [])
    start = time.perf_counter()
    draft, mode = get_llm().draft_report(
        question, evidence, sources, feedback=feedback_text
    )
    draft, dropped_urls = _sanitize_report(draft, sources, evidence)
    if dropped_urls:
        errors.append(
            {
                "node": "draft_report",
                "type": "citation_mismatch",
                "error": "已从报告中移除未抓取 URL：" + ", ".join(dropped_urls[:5]),
            }
        )
    latency = _elapsed_ms(start)
    status = "ok" if evidence and mode == "openai" else "degraded"
    sl.add(
        node_name="draft_report",
        input_summary=f"{len(evidence)} 条证据/修改意见={summarize_text(feedback_text, 40) or '无'}",
        output_summary=(
            f"{len(draft)} 字符草稿；mode={mode}；移除未验证 URL {len(dropped_urls)} 个"
        ),
        status=status,
        latency_ms=latency,
        error=None if evidence else "无证据，草稿已标注未找到可靠来源",
    )
    return sl.updates(draft_report=draft, errors=errors)


# ================================================================ 节点 7：草稿人工审核
def human_review_draft(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    payload = {
        "type": "draft_review",
        "research_question": state.get("research_question", ""),
        "draft_report": state.get("draft_report", ""),
        "sources": _sources_from_pages(state.get("fetched_pages") or []),
        "evidence_count": len(state.get("evidence") or []),
        "revision_count": int(state.get("revision_count") or 0),
        # 质量元数据：让审核者知道“覆盖了多少子问题、证据支持率如何”
        "coverage_summary": state.get("coverage_summary") or {},
        "verification": state.get("verification") or {},
    }
    decision = interrupt(payload)  # ← 在这里暂停，等待 Command(resume=...)
    if not isinstance(decision, dict):
        decision = {"decision": "approve"}
    choice = str(decision.get("decision") or "approve").lower()
    if choice not in {"approve", "revise", "terminate"}:
        choice = "approve"
    comment = str(decision.get("comment") or "")
    edited = str(decision.get("edited_report") or "")
    revision_count = int(state.get("revision_count") or 0)
    errors = list(state.get("errors") or [])
    if choice == "revise":
        if revision_count >= MAX_REVISIONS:
            errors.append(
                {
                    "node": "human_review_draft",
                    "type": "revision_limit",
                    "error": f"已达到最大修改轮次 {MAX_REVISIONS}，按通过处理",
                }
            )
            choice = "approve"
        else:
            revision_count += 1
    feedback = {"decision": choice, "comment": comment}
    status_by_choice = {"approve": "approved", "revise": "revise", "terminate": "terminated"}
    sl.add(
        node_name="human_review_draft",
        tool_name="interrupt",
        input_summary="人工审核研究报告草稿",
        output_summary=f"决策={choice}；意见={summarize_text(comment, 60) or '无'}",
        status=status_by_choice[choice],
    )
    updates: dict[str, Any] = {
        "human_feedback": feedback,
        "revision_count": revision_count,
        "errors": errors,
    }
    if edited.strip():
        # 人工粘贴的稿件同样要过 URL 白名单清洗，不能成为绕过口。
        cleaned, dropped = _sanitize_report(
            edited,
            _sources_from_pages(state.get("fetched_pages") or []),
            list(state.get("evidence") or []),
        )
        if dropped:
            errors.append(
                {
                    "node": "human_review_draft",
                    "type": "citation_mismatch",
                    "error": "人工编辑稿中的未抓取 URL 已移除：" + ", ".join(dropped[:5]),
                }
            )
            updates["errors"] = errors
        updates["draft_report"] = cleaned
    return sl.updates(**updates)


def route_after_review(state: ResearchState) -> Literal["draft_report", "human_confirm_actions", "finalize"]:
    decision = str((state.get("human_feedback") or {}).get("decision") or "")
    if decision == "terminate":
        return "finalize"
    if decision == "revise":
        return "draft_report"
    return "human_confirm_actions"


# ================================================================ 节点 8：写操作确认
def human_confirm_actions(state: ResearchState) -> dict[str, Any]:
    """人工暂停 ②③：发送邮件前、保存 Notion 前各暂停一次（两个独立的 interrupt）。"""
    sl = StepLog(state)
    question = str(state.get("research_question") or "")
    draft = str(state.get("draft_report") or "")
    default_to = os.getenv("DEFAULT_EMAIL_TO", "").strip()
    email_default = {
        "to": default_to,
        "subject": f"研究报告：{question[:60]}",
        "body": draft,
    }
    notion_default = {
        "title": f"研究报告：{question[:80]}",
        "content": draft,
        "tags": ["research", "ai-report"],
    }
    errors = list(state.get("errors") or [])
    approved: list[str] = []
    email_payload: dict[str, Any] | None = None
    notion_payload: dict[str, Any] | None = None

    # ---- 人工暂停 ②：发送邮件前，展示收件人/主题/正文 ----
    email_decision = interrupt(
        {
            "type": "email_confirmation",
            "research_question": question,
            "email": email_default,
        }
    )
    if not isinstance(email_decision, dict):
        email_decision = {}
    if email_decision.get("send"):
        email_payload = dict(email_default)
        email_payload.update(email_decision.get("email_payload") or {})
        if not str(email_payload.get("to") or "").strip():
            errors.append(
                {
                    "node": "human_confirm_actions",
                    "type": "missing_email_to",
                    "error": "已同意发邮件，但收件人为空，已跳过该动作",
                }
            )
            email_payload = None
        else:
            approved.append("send_email")

    # ---- 人工暂停 ③：保存 Notion 前，展示标题/内容/标签 ----
    notion_decision = interrupt(
        {
            "type": "notion_confirmation",
            "research_question": question,
            "notion": notion_default,
        }
    )
    if not isinstance(notion_decision, dict):
        notion_decision = {}
    if notion_decision.get("save"):
        notion_payload = dict(notion_default)
        notion_payload.update(notion_decision.get("notion_payload") or {})
        if not str(notion_payload.get("title") or "").strip():
            errors.append(
                {
                    "node": "human_confirm_actions",
                    "type": "missing_notion_title",
                    "error": "已同意存 Notion，但标题为空，已跳过该动作",
                }
            )
            notion_payload = None
        else:
            approved.append("save_to_notion")

    email_status = "approved" if "send_email" in approved else "skipped"
    notion_status = "approved" if "save_to_notion" in approved else "skipped"
    sl.add(
        node_name="human_confirm_actions",
        tool_name="interrupt",
        input_summary="人工暂停 ②：发邮件前确认（已展示收件人/主题/正文）",
        output_summary=(
            f"send={'是' if email_status == 'approved' else '否'}；"
            f"收件人={(email_payload or {}).get('to', '-')}"
        ),
        status=email_status,
    )
    sl.add(
        node_name="human_confirm_actions",
        tool_name="interrupt",
        input_summary="人工暂停 ③：保存 Notion 前确认（已展示标题/内容/标签）",
        output_summary=(
            f"save={'是' if notion_status == 'approved' else '否'}；"
            f"标题={(notion_payload or {}).get('title', '-')}"
        ),
        status=notion_status,
    )
    return sl.updates(
        approved_actions=approved,
        email_payload=email_payload,
        notion_payload=notion_payload,
        errors=errors,
    )


# ================================================================ 节点 9：执行写操作
def execute_actions(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    results: list[dict[str, Any]] = []
    errors = list(state.get("errors") or [])
    retry_counts = dict(state.get("retry_counts") or {})
    for action in state.get("approved_actions") or []:
        payload: dict[str, Any] = {}
        if action == "send_email":
            raw = state.get("email_payload") or {}
            payload = {"to": raw.get("to"), "subject": raw.get("subject"), "body": raw.get("body")}
        elif action == "save_to_notion":
            raw = state.get("notion_payload") or {}
            payload = {
                "title": raw.get("title"),
                "content": raw.get("content"),
                "tags": raw.get("tags") or [],
            }
        else:
            continue
        # 关键：human_approved=True 只在这里出现，且仅对已人工确认的动作调用。
        result = tools.call_tool(action, payload, human_approved=True)
        retry_counts[action] = retry_counts.get(action, 0) + max(0, result.attempts - 1)
        data = result.data if isinstance(result.data, dict) else {}
        record = {
            "action": action,
            "ok": result.ok,
            "status": "ok" if result.ok else "failed",
            "mode": str(data.get("mode") or ""),
            "detail": data,
            "error": result.error,
            "attempts": result.attempts,
        }
        results.append(record)
        if result.ok:
            sl.add(
                node_name="execute_actions",
                tool_name=action,
                input_summary=f"执行人工确认的 {action}",
                output_summary=f"成功，mode={record['mode']}，{summarize_text(data, 90)}",
                status="ok",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
            )
        else:
            errors.append(
                {
                    "node": "execute_actions",
                    "tool": action,
                    "type": "write_failed",
                    "error": result.error or "未知错误",
                    "attempts": result.attempts,
                }
            )
            sl.add(
                node_name="execute_actions",
                tool_name=action,
                input_summary=f"执行人工确认的 {action}",
                output_summary=result.summary(),
                status="error",
                retry_count=max(0, result.attempts - 1),
                latency_ms=result.latency_ms,
                error=result.error,
            )
    if not results:
        sl.add(
            node_name="execute_actions",
            input_summary="approved_actions=[]",
            output_summary="没有已确认的写操作，跳过",
            status="skipped",
        )
    return sl.updates(action_results=results, errors=errors, retry_counts=retry_counts)


# ================================================================ 节点 10：收尾
def finalize(state: ResearchState) -> dict[str, Any]:
    sl = StepLog(state)
    errors = list(state.get("errors") or [])
    actions = list(state.get("action_results") or [])
    evidence = list(state.get("evidence") or [])
    feedback = state.get("human_feedback") or {}
    if feedback.get("decision") == "terminate":
        status = "terminated_by_user"
    elif any(not action.get("ok") for action in actions):
        status = "completed_with_action_failure"
    elif not evidence:
        status = "completed_no_sources"
    else:
        status = "completed"
    stats = log_stats(state.get("logs") or [])
    current_usage = _llm_usage(get_llm())
    baseline = dict(state.get("llm_usage_baseline") or {})
    token_usage = {
        key: max(0, int(current_usage.get(key, 0)) - int(baseline.get(key, 0)))
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "calls")
    }
    try:
        model = str(getattr(get_llm(), "model", "") or "")
    except Exception:  # noqa: BLE001
        model = ""
    cost_info = tracing.estimate_cost(
        model,
        token_usage["prompt_tokens"],
        token_usage["completion_tokens"],
    )
    cost_cny = round(cost_info["cost_cny"], 6)
    node_usage = dict(state.get("node_usage") or {})
    token_usage_by_node: dict[str, dict[str, int]] = {}
    cost_by_node: dict[str, float] = {}
    for node_name, usage in node_usage.items():
        usage = dict(usage or {})
        token_usage_by_node[node_name] = {
            key: int(usage.get(key, 0) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "calls")
        }
        node_cost = tracing.estimate_cost(
            model,
            token_usage_by_node[node_name]["prompt_tokens"],
            token_usage_by_node[node_name]["completion_tokens"],
        )
        cost_by_node[node_name] = round(node_cost["cost_cny"], 8)
    duration_ms = 0
    try:
        duration_ms = int(
            (datetime.fromisoformat(now_iso()) - datetime.fromisoformat(str(state.get("started_at") or ""))).total_seconds()
            * 1000
        )
    except ValueError:
        duration_ms = 0
    plan = state.get("plan") or {}
    coverage_summary = state.get("coverage_summary") or {}
    verification = state.get("verification") or {}
    sl.add(
        node_name="finalize",
        input_summary=f"证据={len(evidence)}；动作={len(actions)}；错误={len(errors)}",
        output_summary=(
            f"status={status}；日志步骤={stats['total'] + 1}；重试={stats['retries']}；"
            f"覆盖={coverage_summary.get('covered', 0)}/{coverage_summary.get('total', 0)}；"
            f"支持率={verification.get('support_rate', 0):.0%}；"
            f"耗时={duration_ms}ms；token={token_usage['total_tokens']}；成本=¥{cost_cny}"
        ),
        status="ok" if status.startswith("completed") else "degraded",
    )
    final_output = {
        "research_question": state.get("research_question", ""),
        "status": status,
        "report": state.get("draft_report", ""),
        "sources": _sources_from_pages(state.get("fetched_pages") or []),
        "evidence": evidence,
        "evidence_count": len(evidence),
        "evidence_note": state.get("evidence_note", ""),
        "sub_questions": list(plan.get("sub_questions") or []),
        "queries": list(plan.get("queries") or []),
        # 研究质量闭环：覆盖度、断言校验、实际检索轮次
        "coverage": state.get("coverage") or [],
        "coverage_summary": coverage_summary,
        "verification": verification,
        "retrieval_rounds": int(state.get("retrieval_round") or 0) + 1,
        "actions": actions,
        "approved_actions": state.get("approved_actions") or [],
        "errors": errors,
        "llm_mode": state.get("llm_mode", ""),
        "model": model,
        "token_usage": token_usage,
        "token_usage_by_node": token_usage_by_node,
        "cost_cny": cost_cny,
        "cost_by_node": cost_by_node,
        "cost_estimated": bool(cost_info["estimated"]),
        "cost_price_source": cost_info["price_source"],
        "duration_ms": duration_ms,
        "trace_id": state.get("trace_id", ""),
        "started_at": state.get("started_at", ""),
        "finished_at": now_iso(),
        # 这里用追加自身日志后的 sl.logs 统计，保证 total 与实际条数一致。
        "log_summary": log_stats(sl.logs),
    }
    _trace_recorder(state).add_summary(
        {
            "status": status,
            "duration_ms": duration_ms,
            "model": model,
            "prompt_tokens": token_usage["prompt_tokens"],
            "completion_tokens": token_usage["completion_tokens"],
            "total_tokens": token_usage["total_tokens"],
            "llm_calls": token_usage["calls"],
            "cost_cny": cost_cny,
            "cost_estimated": bool(cost_info["estimated"]),
            "price_source": cost_info["price_source"],
            "node_count": len(token_usage_by_node),
            "token_usage_by_node": token_usage_by_node,
            "cost_by_node": cost_by_node,
        }
    )
    return sl.updates(final_output=final_output)


# ================================================================ 图构建
def _node_functions() -> dict[str, Any]:
    return {
        "receive_question": receive_question,
        "plan_research": plan_research,
        "search_web": search_web_node,
        "fetch_pages": fetch_pages_node,
        "retrieve_knowledge": retrieve_knowledge,
        "summarize_sources": summarize_sources_node,
        "check_coverage": check_coverage,
        "draft_report": draft_report,
        "human_review_draft": human_review_draft,
        "human_confirm_actions": human_confirm_actions,
        "execute_actions": execute_actions,
        "finalize": finalize,
    }


def build_graph(checkpointer: Any | None = None) -> Any:
    builder = StateGraph(ResearchState)
    for name, fn in _node_functions().items():
        builder.add_node(name, _instrument(name, fn))

    builder.add_edge(START, "receive_question")
    builder.add_edge("receive_question", "plan_research")
    builder.add_edge("plan_research", "search_web")
    builder.add_edge("search_web", "fetch_pages")
    builder.add_edge("fetch_pages", "retrieve_knowledge")
    builder.add_edge("retrieve_knowledge", "summarize_sources")
    builder.add_edge("summarize_sources", "check_coverage")
    builder.add_conditional_edges(
        "check_coverage",
        route_after_coverage,
        {
            "search_web": "search_web",  # 覆盖不足 → 补检一轮，再走抓取/总结
            "draft_report": "draft_report",
        },
    )
    builder.add_edge("draft_report", "human_review_draft")
    builder.add_conditional_edges(
        "human_review_draft",
        route_after_review,
        {
            "draft_report": "draft_report",
            "human_confirm_actions": "human_confirm_actions",
            "finalize": "finalize",
        },
    )
    builder.add_edge("human_confirm_actions", "execute_actions")
    builder.add_edge("execute_actions", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


def render_evidence_table(evidence: list[dict[str, Any]]) -> list[str]:
    """把证据表渲染成 Markdown（编号 / 摘要 / 来源 / 定位 / 可信度）。"""
    if not evidence:
        return ["（无可用证据）"]
    lines = [
        "| 编号 | 结论 / 摘要 | 来源 | 定位 | 可信度 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, item in enumerate(evidence, start=1):
        claim = str(item.get("claim") or item.get("evidence_text") or "").replace("|", "\\|")[:90]
        url = str(item.get("source_url") or "")
        snippet = str(item.get("source_snippet") or "").replace("|", "\\|")[:50]
        lines.append(f"| S{idx} | {claim} | {url} | {snippet} | {item.get('confidence', 0)} |")
    return lines


def render_final_output(final: dict[str, Any]) -> str:
    """把 final_output 渲染成终端可读的最终结果。"""
    lines = [
        "# 最终结果",
        "",
        f"- 研究问题：{final.get('research_question', '')}",
        f"- 状态：{final.get('status', '')}",
        f"- 来源数：{len(final.get('sources') or [])}；证据数：{final.get('evidence_count', 0)}",
        f"- 子问题：{len(final.get('sub_questions') or [])} 个；耗时：{final.get('duration_ms', 0)}ms",
        (
            f"- 证据覆盖：{(final.get('coverage_summary') or {}).get('covered', 0)}/"
            f"{(final.get('coverage_summary') or {}).get('total', 0)} 子问题"
            f"（{(final.get('coverage_summary') or {}).get('rate', 0):.0%}）；"
            f"检索轮次：{final.get('retrieval_rounds', 1)}"
        ),
        (
            f"- 断言校验：支持 {(final.get('verification') or {}).get('supported', 0)} / "
            f"部分 {(final.get('verification') or {}).get('partial', 0)} / "
            f"丢弃 {(final.get('verification') or {}).get('dropped', 0)}"
            f"（支持率 {(final.get('verification') or {}).get('support_rate', 0):.0%}）"
        ),
        f"- token：{(final.get('token_usage') or {}).get('total_tokens', 0)}；成本：¥{final.get('cost_cny', 0)}；LLM 模式：{final.get('llm_mode', '')}",
        f"- trace_id：{final.get('trace_id', '')}",
        "",
    ]
    actions = final.get("actions") or []
    lines.append("## 写操作执行结果")
    lines.append("")
    if not actions:
        lines.append("- 本次没有执行任何写操作。")
    else:
        lines.append("| 动作 | 结果 | 模式 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for action in actions:
            detail = action.get("detail") or {}
            note = detail.get("path") or detail.get("url") or detail.get("message_id") or action.get("error") or ""
            lines.append(
                f"| {action.get('action')} | {'成功' if action.get('ok') else '失败'} | "
                f"{action.get('mode') or '-'} | {note} |"
            )
    lines.append("")
    if final.get("errors"):
        lines.append("## 错误与降级记录")
        lines.append("")
        for err in final["errors"]:
            lines.append(
                f"- [{err.get('node', '?')}] {err.get('type', 'error')}: {err.get('error', '')}"
            )
        lines.append("")
    if final.get("evidence_note"):
        lines.append(f"> 证据说明：{final['evidence_note']}")
        lines.append("")
    lines.append("## 证据表")
    lines.append("")
    lines.extend(render_evidence_table(list(final.get("evidence") or [])))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(final.get("report") or "（没有生成报告）")
    return "\n".join(lines)
