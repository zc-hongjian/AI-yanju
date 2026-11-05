"""评测入口：按 evaluation/dataset.jsonl 逐题跑完整图，统计验收指标。

用法：
    # 真实联网（默认），40 题
    python evaluation/run_eval.py --mode real --tag real_current

    # 本地确定性 fixture，用于流水线/引用/延迟指标
    python evaluation/run_eval.py --mode mock --tag mock_current

    # 消融基线：关闭本轮新增的引用可点击化与问题拆解
    python evaluation/run_eval.py --mode mock --tag mock_baseline \
        --no-link-citations --no-sub-questions

    # 快速验证
    python evaluation/run_eval.py --mode mock --tag smoke --limit 5

输出：
    evaluation/results/<tag>.jsonl    # 每题明细
    evaluation/results/<tag>.summary.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(EVAL_DIR))

from langgraph.types import Command  # noqa: E402

import graph as graph_module  # noqa: E402
import llm_client  # noqa: E402
import mock_web  # noqa: E402
import tools  # noqa: E402
from state import new_state  # noqa: E402

DATASET = EVAL_DIR / "dataset.jsonl"
RESULTS_DIR = EVAL_DIR / "results"
LOG_ROOT = EVAL_DIR / "logs"
CITE_RE = re.compile(r"\[S(\d+)\]")
LINK_CITE_RE = re.compile(r"\[S(\d+)\]\(https?://")
HIT_COUNT_RE = re.compile(r"(\d+)\s*条结果")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="研究助手评测")
    parser.add_argument("--mode", choices=["mock", "real"], default="real")
    parser.add_argument("--tag", required=True, help="结果文件名前缀，如 real_current")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", default="", help="只跑指定题号，逗号分隔，如 Q21,Q31")
    parser.add_argument(
        "--llm",
        choices=["offline", "openai"],
        default="offline",
        help="offline=启发式；openai=读取 .env 的真实模型（需 OPENAI_API_KEY）",
    )
    parser.add_argument("--no-link-citations", action="store_true", help="消融：关闭引用可点击化")
    parser.add_argument("--no-sub-questions", action="store_true", help="消融：关闭问题拆解")
    return parser.parse_args()


def load_dataset(limit: int | None = None) -> list[dict[str, Any]]:
    entries = [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return entries[:limit] if limit else entries


def resume_value(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str((payload or {}).get("type") or "")
    if kind == "draft_review":
        return {"decision": "approve", "comment": ""}
    if kind == "email_confirmation":
        return {"send": False, "email_payload": None}
    if kind == "notion_confirmation":
        return {"save": False, "notion_payload": None}
    raise RuntimeError(f"未知 interrupt 类型：{kind!r}")


def run_graph(question: str, *, trace_id: str) -> tuple[dict[str, Any], int]:
    graph = graph_module.build_graph()
    config = {"configurable": {"thread_id": f"eval-{trace_id}"}}
    state = new_state(question, trace_id=trace_id, log_dir=LOG_ROOT / trace_id)
    started = time.perf_counter()
    result = graph.invoke(state, config)
    interrupt_rounds = 0
    while isinstance(result, dict) and "__interrupt__" in result:
        interrupt_rounds += 1
        if interrupt_rounds > 10:
            raise RuntimeError("interrupt 次数异常（>10）")
        payload = (result.get("__interrupt__") or [{}])[0].value or {}
        result = graph.invoke(Command(resume=resume_value(payload)), config)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result, elapsed_ms


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def _section(report: str, name: str) -> str:
    marker = f"## {name}"
    if marker not in report:
        return ""
    return report.split(marker, 1)[1].split("## ", 1)[0]


def analyze(
    entry: dict[str, Any],
    result: dict[str, Any],
    elapsed_ms: int,
) -> dict[str, Any]:
    final = result.get("final_output") or {}
    report = str(final.get("report") or "")
    evidence = list(final.get("evidence") or result.get("evidence") or [])
    sources = list(final.get("sources") or [])
    logs = list(result.get("logs") or [])
    errors = list(final.get("errors") or [])

    findings = _section(report, "关键发现")
    bullets = [
        line.strip()[2:].strip()
        for line in findings.splitlines()
        if line.strip().startswith(("- ", "* "))
    ]
    if not evidence:
        # 无证据的失败题目不计入引用类指标（避免把「未找到可靠来源」当作未引用结论）
        bullets = []
    cited = [bullet for bullet in bullets if any(1 <= int(n) <= len(evidence) for n in CITE_RE.findall(bullet))]
    clickable = [bullet for bullet in bullets if LINK_CITE_RE.search(bullet)]
    trace_rate = len(cited) / len(bullets) if bullets else 0.0
    clickable_rate = len(clickable) / len(bullets) if bullets else 0.0
    all_clickable = bool(bullets) and len(clickable) == len(bullets)

    search_ok = sum(
        1 for log in logs if log.get("tool_name") == "search_web" and log.get("status") == "ok"
    )
    fetch_ok = sum(
        1 for log in logs if log.get("tool_name") == "fetch_page" and log.get("status") == "ok"
    )
    summarize_ok = any(
        log.get("tool_name") == "summarize_sources"
        and log.get("status") in {"ok", "degraded"}
        for log in logs
    )
    draft_logged = any(log.get("node_name") == "draft_report" for log in logs)
    sub_questions = list(final.get("sub_questions") or [])
    tool_ok = (
        search_ok >= 1
        and fetch_ok >= 1
        and summarize_ok
        and draft_logged
        and 3 <= len(sub_questions) <= 5
    )

    status = str(final.get("status") or "")
    success = (
        status in {"completed", "completed_with_action_failure"}
        and len(report) >= 200
        and len(evidence) > 0
    )
    fail_reason = ""
    if not success:
        if not evidence:
            fail_reason = "无可用证据（检索/抓取/证据提取失败）"
        elif len(report) < 200:
            fail_reason = "报告过短"
        else:
            fail_reason = f"final status={status}"

    normalized_report = _norm(report)
    expected = [str(fact).strip() for fact in entry.get("expected_facts") or [] if str(fact).strip()]
    covered = sum(1 for fact in expected if _norm(fact) and _norm(fact) in normalized_report)
    fact_coverage = covered / len(expected) if expected else 0.0

    # 宽松代理：预期要点与报告共享足够多的字符 bigram，即视为覆盖（容忍改写/换述）
    report_compact = re.sub(r"\s+", "", report)
    report_bigrams = {report_compact[i : i + 2] for i in range(len(report_compact) - 1)}
    loose_covered = 0
    for fact in expected:
        fact_compact = re.sub(r"\s+", "", fact)
        grams = {fact_compact[i : i + 2] for i in range(len(fact_compact) - 1)}
        if grams and sum(1 for gram in grams if gram in report_bigrams) / len(grams) >= 0.5:
            loose_covered += 1
    fact_coverage_loose = loose_covered / len(expected) if expected else 0.0

    pages_by_url = {
        str(page.get("url") or ""): _norm(page.get("content"))
        for page in result.get("fetched_pages") or []
    }
    verbatim = sum(
        1
        for item in evidence
        if _norm(item.get("quote")) and _norm(item.get("quote")) in pages_by_url.get(str(item.get("source_url") or ""), "")
    )
    verbatim_rate = verbatim / len(evidence) if evidence else 0.0

    raw_hits = 0
    for log in logs:
        if log.get("node_name") == "search_web":
            match = HIT_COUNT_RE.search(str(log.get("output_summary") or ""))
            if match:
                raw_hits += int(match.group(1))
    unique_hits = len(result.get("search_results") or [])
    dedup_rate = 1 - unique_hits / raw_hits if raw_hits else 1.0

    final_errors = [f"[{e.get('node')}] {e.get('error')}" for e in errors][:5]
    if fail_reason and final_errors:
        fail_reason += "；" + "；".join(final_errors[:2])

    return {
        "id": entry.get("id"),
        "category": entry.get("category"),
        "question": entry.get("question"),
        "mode": final.get("llm_mode", ""),
        "status": status,
        "success": bool(success),
        "fail_reason": fail_reason,
        "report_chars": len(report),
        "report_excerpt": report[:800],
        "report": report,
        "evidence_preview": [
            {
                "claim": str(item.get("claim") or "")[:160],
                "quote": str(item.get("quote") or "")[:160],
                "source_url": item.get("source_url"),
                "confidence": item.get("confidence"),
            }
            for item in evidence[:4]
        ],
        "evidence_count": len(evidence),
        "source_count": len(sources),
        "sub_questions": len(sub_questions),
        "search_ok": search_ok,
        "fetch_ok": fetch_ok,
        "summarize_ok": bool(summarize_ok),
        "tool_ok": bool(tool_ok),
        "bullets": len(bullets),
        "bullets_cited": len(cited),
        "bullets_clickable": len(clickable),
        "citation_trace_rate": round(trace_rate, 4),
        "clickable_rate": round(clickable_rate, 4),
        "all_clickable": bool(all_clickable),
        "fact_coverage": round(fact_coverage, 4),
        "fact_coverage_loose": round(fact_coverage_loose, 4),
        "expected_facts": expected,
        "covered_facts": covered,
        "loose_covered_facts": loose_covered,
        "evidence_verbatim_rate": round(verbatim_rate, 4),
        "dedup_rate": round(dedup_rate, 4),
        "raw_hits": raw_hits,
        "unique_hits": unique_hits,
        "interrupt_rounds": 0,
        "elapsed_ms": elapsed_ms,
        "token_usage": final.get("token_usage") or {},
        "cost_cny": float(final.get("cost_cny") or 0.0),
        "errors": final_errors,
    }


def _ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    return round(numerator / denominator, 4) if denominator else default


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def build_summary(tag: str, mode: str, records: list[dict[str, Any]], llm: str = "offline") -> dict[str, Any]:
    total = len(records)
    bullets = sum(rec["bullets"] for rec in records)
    bullets_cited = sum(rec["bullets_cited"] for rec in records)
    bullets_clickable = sum(rec["bullets_clickable"] for rec in records)
    evidence = sum(rec["evidence_count"] for rec in records)
    latencies = [int(rec["elapsed_ms"]) for rec in records]
    by_category: dict[str, dict[str, Any]] = {}
    for rec in records:
        bucket = by_category.setdefault(
            rec["category"], {"n": 0, "success": 0, "evidence": 0, "bullets": 0, "cited": 0}
        )
        bucket["n"] += 1
        bucket["success"] += int(rec["success"])
        bucket["evidence"] += rec["evidence_count"]
        bucket["bullets"] += rec["bullets"]
        bucket["cited"] += rec["bullets_cited"]
    for bucket in by_category.values():
        bucket["success_rate"] = _ratio(bucket["success"], bucket["n"])
        bucket["citation_trace_rate"] = _ratio(bucket["cited"], bucket["bullets"])

    failures = [
        {
            "id": rec["id"],
            "question": rec["question"],
            "status": rec["status"],
            "reason": rec["fail_reason"],
            "evidence_count": rec["evidence_count"],
        }
        for rec in records
        if not rec["success"]
    ]

    return {
        "tag": tag,
        "mode": mode,
        "llm": llm,
        "total": total,
        "success": sum(1 for rec in records if rec["success"]),
        "success_rate": _ratio(sum(1 for rec in records if rec["success"]), total),
        "citation_trace_rate": _ratio(bullets_cited, bullets),
        "clickable_rate": _ratio(bullets_clickable, bullets),
        "all_clickable_rate": _ratio(sum(1 for rec in records if rec["all_clickable"]), total),
        "tool_correct_rate": _ratio(sum(1 for rec in records if rec["tool_ok"]), total),
        "fact_coverage": _ratio(
            sum(rec.get("loose_covered_facts", 0) for rec in records),
            sum(len(rec["expected_facts"]) for rec in records),
        ),
        "fact_coverage_exact": _ratio(
            sum(rec["covered_facts"] for rec in records),
            sum(len(rec["expected_facts"]) for rec in records),
        ),
        "evidence_verbatim_rate": _ratio(
            sum(round(rec["evidence_verbatim_rate"] * rec["evidence_count"]) for rec in records), evidence
        ),
        "sub_questions_ok_rate": _ratio(
            sum(1 for rec in records if 3 <= rec["sub_questions"] <= 5), total
        ),
        "avg_sub_questions": round(sum(rec["sub_questions"] for rec in records) / total, 2) if total else 0,
        "avg_interrupts": round(sum(rec["interrupt_rounds"] for rec in records) / total, 2) if total else 0,
        "avg_latency_ms": int(sum(latencies) / total) if total else 0,
        "p95_latency_ms": _p95(latencies),
        "avg_cost_cny": round(sum(rec["cost_cny"] for rec in records) / total, 6) if total else 0.0,
        "total_cost_cny": round(sum(rec["cost_cny"] for rec in records), 6),
        "avg_tokens": round(
            sum(int((rec["token_usage"] or {}).get("total_tokens", 0)) for rec in records) / total, 1
        )
        if total
        else 0,
        "dedup_rate": round(
            sum(rec["dedup_rate"] for rec in records) / total, 4
        )
        if total
        else 0.0,
        "by_category": by_category,
        "failures": failures,
    }


def setup_env(mode: str, llm: str = "offline") -> None:
    if llm == "openai":
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        except Exception:  # noqa: BLE001 - 没有 .env 时用系统环境变量
            pass
        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise SystemExit(
                "[错误] --llm openai 需要 OPENAI_API_KEY：请在项目根目录 .env 中配置，"
                "然后先运行 `python check_llm.py` 自检。"
            )
        os.environ["AGENT_LLM_MODE"] = "openai"
        print(f"[评测] LLM=openai model={os.getenv('OPENAI_MODEL', '') or 'gpt-4o-mini'}", flush=True)
    else:
        os.environ["AGENT_LLM_MODE"] = "offline"
        os.environ.pop("OPENAI_API_KEY", None)
        print("[评测] LLM=offline（启发式）", flush=True)
    os.environ["AGENT_QUIET_LOG"] = "1"
    os.environ["AGENT_AUTO_APPROVE"] = "1"
    os.environ.setdefault("AGENT_LOG_DIR", str(LOG_ROOT))
    if mode == "mock":
        os.environ["AGENT_SEARCH_TIMEOUT"] = "5"


def main() -> int:
    args = parse_args()
    setup_env(args.mode, args.llm)
    if args.no_link_citations:
        os.environ["AGENT_LINK_CITATIONS"] = "0"
    if args.no_sub_questions:
        os.environ["AGENT_PLAN_SUB_QUESTIONS"] = "0"

    dataset = load_dataset(args.limit)
    if args.ids.strip():
        wanted = {item.strip().upper() for item in args.ids.split(",") if item.strip()}
        dataset = [entry for entry in dataset if str(entry.get("id", "")).upper() in wanted]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    for index, entry in enumerate(dataset, start=1):
        trace_id = f"{args.tag}-{entry['id']}-{uuid.uuid4().hex[:4]}"
        saved: dict[str, Any] = {}
        if args.mode == "mock":
            fake_search, fake_fetch = mock_web.install(entry)
            saved["search_web"] = tools.TOOL_IMPL_REGISTRY.get("search_web")
            saved["fetch_page"] = tools.TOOL_IMPL_REGISTRY.get("fetch_page")
            tools.TOOL_IMPL_REGISTRY["search_web"] = fake_search
            tools.TOOL_IMPL_REGISTRY["fetch_page"] = fake_fetch
        try:
            tools.reset_provider_breakers()
            llm_client.reset_llm()
            result, elapsed_ms = run_graph(str(entry["question"]), trace_id=trace_id)
            record = analyze(entry, result, elapsed_ms)
            record["interrupt_rounds"] = int(
                sum(1 for log in (result.get("logs") or []) if log.get("tool_name") == "interrupt")
            )
        except Exception as exc:  # noqa: BLE001 - 单题失败不能中断整批评测
            record = {
                "id": entry.get("id"),
                "category": entry.get("category"),
                "question": entry.get("question"),
                "status": "exception",
                "success": False,
                "fail_reason": f"{type(exc).__name__}: {exc}",
                "report_chars": 0,
                "evidence_count": 0,
                "source_count": 0,
                "sub_questions": 0,
                "search_ok": 0,
                "fetch_ok": 0,
                "summarize_ok": False,
                "tool_ok": False,
                "bullets": 0,
                "bullets_cited": 0,
                "bullets_clickable": 0,
                "citation_trace_rate": 0.0,
                "clickable_rate": 0.0,
                "all_clickable": False,
                "fact_coverage": 0.0,
                "expected_facts": [],
                "covered_facts": 0,
                "evidence_verbatim_rate": 0.0,
                "dedup_rate": 0.0,
                "raw_hits": 0,
                "unique_hits": 0,
                "interrupt_rounds": 0,
                "elapsed_ms": 0,
                "token_usage": {},
                "cost_cny": 0.0,
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
        finally:
            if args.mode == "mock":
                for key, value in saved.items():
                    if value is None:
                        tools.TOOL_IMPL_REGISTRY.pop(key, None)
                    else:
                        tools.TOOL_IMPL_REGISTRY[key] = value
        records.append(record)
        print(
            f"[{index:02d}/{len(dataset)}] {record['id']} {record['category']:<11} "
            f"success={record['success']} evidence={record['evidence_count']} "
            f"{record['elapsed_ms']}ms {record.get('fail_reason') or ''}",
            flush=True,
        )

    (RESULTS_DIR / f"{args.tag}.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    summary = build_summary(args.tag, args.mode, records, args.llm)
    (RESULTS_DIR / f"{args.tag}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
