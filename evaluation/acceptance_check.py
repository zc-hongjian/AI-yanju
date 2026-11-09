"""功能验收自动化：按「必须闭环」的 9 项要求逐项检查并生成 FUNCTIONAL_ACCEPTANCE.md。

用法：
    python evaluation/acceptance_check.py

检查范围：
1. 问题拆解 3~5 个子问题
2. 联网搜索（工具调用 + 候选来源）
3. 网页抓取（正文 / 去广告 / 去重）
4. 证据表（来源 / 摘要 / 可信度）
5. 结构化 Markdown 报告
6. 引用校验（关键结论对应可点击来源）
7. 人工确认（暂停 / 修改 / 继续 / 写操作门禁）
8. 失败处理（重试 / 确定性失败不重试 / 模型报错降级）
9. 可观测（trace / 工具日志 / token / 成本 / 耗时）

运行环境：离线 LLM + 确定性 mock 网页（不依赖外网，可重复）；真实联网效果见 evaluation/eval_report.md。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(EVAL_DIR))

# 本验收只看“联网检索 + 网页证据”链路：把知识库指向空目录，避免 KB 证据影响
# “可点击来源”的判定（知识库有独立验收：evaluation/kb_check.py）。
_EMPTY_KB = EVAL_DIR / "results" / "empty-kb"
_EMPTY_KB.mkdir(parents=True, exist_ok=True)
os.environ["AGENT_KB_DIR"] = str(_EMPTY_KB)
os.environ["AGENT_KB_INDEX"] = str(EVAL_DIR / "results" / "empty-kb-index.json")

from langgraph.types import Command  # noqa: E402

import graph as graph_module  # noqa: E402
import llm_client  # noqa: E402
import mock_web  # noqa: E402
import tools  # noqa: E402
from llm_client import LLMClient, LLMError  # noqa: E402
from state import new_state  # noqa: E402

ACCEPT_DIR = EVAL_DIR / "results" / "acceptance"
ACCEPT_DIR.mkdir(parents=True, exist_ok=True)

CHECKS: list[dict[str, Any]] = []


def check(item: str, ok: bool, evidence: str) -> None:
    CHECKS.append({"item": item, "ok": bool(ok), "evidence": evidence})
    print(f"{'PASS' if ok else 'FAIL'}  {item}  |  {evidence}")


def _load_entry(entry_id: str = "Q02") -> dict[str, Any]:
    for line in (EVAL_DIR / "dataset.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry["id"] == entry_id:
            return entry
    raise RuntimeError(f"dataset 中找不到 {entry_id}")


def _outbox_files(outbox: Path) -> list[Path]:
    return [path for path in outbox.rglob("*") if path.is_file()] if outbox.exists() else []


class _FakeResponse:
    status_code = 200

    def __init__(self, raw: bytes) -> None:
        self.content = raw
        self.headers = {"content-type": "text/html; charset=utf-8"}

    def iter_content(self, chunk_size: int):
        yield self.content

    def close(self) -> None:
        pass


def _run_flow(
    entry: dict[str, Any],
    *,
    tag: str,
    decide: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """跑一次完整图（mock 网页），返回 result / 中断记录等。"""
    run_dir = ACCEPT_DIR / tag
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    outbox = run_dir / "outbox"
    log_dir = run_dir / "logs"
    for path in (outbox, log_dir):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["AGENT_OUTBOX_DIR"] = str(outbox)
    os.environ["AGENT_LOG_DIR"] = str(log_dir)
    os.environ["DEFAULT_EMAIL_TO"] = "acceptance@example.com"
    os.environ["AGENT_QUIET_LOG"] = "1"
    os.environ["AGENT_LLM_MODE"] = "offline"
    os.environ.pop("OPENAI_API_KEY", None)

    fake_search, fake_fetch = mock_web.install(entry)
    saved = {"search_web": tools.TOOL_IMPL_REGISTRY.get("search_web"), "fetch_page": tools.TOOL_IMPL_REGISTRY.get("fetch_page")}
    tools.TOOL_IMPL_REGISTRY["search_web"] = fake_search
    tools.TOOL_IMPL_REGISTRY["fetch_page"] = fake_fetch
    try:
        tools.reset_provider_breakers()
        llm_client.reset_llm()
        trace_id = f"accept-{tag}-{uuid.uuid4().hex[:6]}"
        graph = graph_module.build_graph()
        config = {"configurable": {"thread_id": trace_id}}
        state = new_state(entry["question"], trace_id=trace_id, log_dir=log_dir)
        result = graph.invoke(state, config)
        interrupts: list[dict[str, Any]] = []
        ctx: dict[str, Any] = {"outbox": outbox}
        while isinstance(result, dict) and "__interrupt__" in result:
            payload = (result.get("__interrupt__") or [{}])[0].value or {}
            interrupts.append(
                {
                    "type": payload.get("type"),
                    "outbox_files": len(_outbox_files(outbox)),
                }
            )
            resume = decide(payload, ctx)
            result = graph.invoke(Command(resume=resume), config)
        return {
            "result": result,
            "interrupts": interrupts,
            "outbox": outbox,
            "log_dir": log_dir,
            "trace_id": trace_id,
        }
    finally:
        for key, value in saved.items():
            if value is None:
                tools.TOOL_IMPL_REGISTRY.pop(key, None)
            else:
                tools.TOOL_IMPL_REGISTRY[key] = value


def _approve_all(payload: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("type")
    if kind == "draft_review":
        return {"decision": "approve", "comment": ""}
    if kind == "email_confirmation":
        return {"send": True, "email_payload": payload.get("email")}
    if kind == "notion_confirmation":
        return {"save": True, "notion_payload": payload.get("notion")}
    raise RuntimeError(f"未知 interrupt：{kind}")


def _revise_once(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("type")
    if kind == "draft_review" and not ctx.get("revised"):
        ctx["revised"] = True
        return {"decision": "revise", "comment": "请补充局限与未证实部分"}
    if kind == "draft_review":
        return {"decision": "approve", "comment": ""}
    if kind == "email_confirmation":
        return {"send": False, "email_payload": None}
    if kind == "notion_confirmation":
        return {"save": False, "notion_payload": None}
    raise RuntimeError(f"未知 interrupt：{kind}")


def run_checks() -> None:
    entry = _load_entry("Q02")
    ctx: dict[str, Any] = {}
    flow = _run_flow(entry, tag="approve", decide=_approve_all)
    result = flow["result"]
    final = result.get("final_output") or {}
    logs = list(result.get("logs") or [])
    report = str(final.get("report") or "")
    evidence = list(final.get("evidence") or [])
    sources = list(final.get("sources") or [])

    # 1. 问题拆解
    sub_questions = list(final.get("sub_questions") or [])
    check(
        "① 问题拆解 3~5 个子问题",
        len(sub_questions) in {3, 4, 5},
        f"{len(sub_questions)} 个：{sub_questions}",
    )

    # 2. 联网搜索
    search_logs = [log for log in logs if log.get("tool_name") == "search_web" and log.get("status") == "ok"]
    search_results = list(result.get("search_results") or [])
    search_urls = [str(hit.get("url")) for hit in search_results]
    check(
        "② 联网搜索拿到候选来源",
        bool(search_logs) and bool(search_results),
        f"search_web 成功 {len(search_logs)} 次，候选 {len(search_results)} 条（去重后 URL {len(set(search_urls))} 个）",
    )

    # 3. 网页抓取：正文 / 去广告 / 去重
    fetch_logs = [log for log in logs if log.get("tool_name") == "fetch_page" and log.get("status") == "ok"]
    pages = list(result.get("fetched_pages") or [])
    de_ad_ok = False
    de_ad_detail = ""
    html = (
        "<html><head><title>抓取清洗测试</title></head><body>"
        "<nav>首页 产品 价格</nav>"
        "<div class='ad-banner'>限时大促，立即购买</div>"
        "<div id='cookie-notice'>本站使用 Cookie</div>"
        "<article>"
        "<p>LangGraph 通过 checkpointer 持久化每一步状态，从而支持人工中断。</p>"
        "<p>LangGraph 通过 checkpointer 持久化每一步状态，从而支持人工中断。</p>"
        "<p>调用方通过 Command 的 resume 参数恢复执行，节点会从 interrupt 处继续。</p>"
        "<p>官方文档建议为每次运行指定 thread_id，并在恢复时复用同一个 thread_id。</p>"
        "<script>track()</script>"
        "</article><footer>版权所有</footer></body></html>"
    )
    saved_assert = tools._assert_safe_url
    saved_fetch = tools._fetch_with_safe_redirects
    tools._assert_safe_url = lambda url: None
    tools._fetch_with_safe_redirects = lambda url, **kwargs: _FakeResponse(html.encode("utf-8"))
    try:
        cleaned_page = tools._fetch_page_impl("https://example.org/clean")
        content = str(cleaned_page["content"])
        de_ad_ok = (
            "大促" not in content
            and "Cookie" not in content
            and "首页 产品 价格" not in content
            and "track()" not in content
            and content.count("LangGraph 通过 checkpointer 持久化每一步状态，从而支持人工中断。") == 1
        )
        de_ad_detail = f"广告/导航/脚本已移除，重复段落已合并，正文 {cleaned_page['chars']} 字符"
    finally:
        tools._assert_safe_url = saved_assert
        tools._fetch_with_safe_redirects = saved_fetch
    source_unique = len({str(page.get("url")) for page in pages}) == len(pages)
    check(
        "③ 网页抓取：正文 / 去广告 / 去重",
        bool(fetch_logs) and bool(pages) and de_ad_ok and source_unique and len(set(search_urls)) == len(search_urls),
        f"抓取成功 {len(fetch_logs)} 页；{de_ad_detail}；来源 URL 唯一={source_unique}；搜索候选 URL 唯一={len(set(search_urls)) == len(search_urls)}",
    )

    # 4. 证据表
    evidence_fields_ok = all(
        str(item.get("claim") or item.get("evidence_text"))
        and str(item.get("source_url") or "").startswith("http")
        and str(item.get("source_snippet") or "")
        and 0.0 <= float(item.get("confidence") or 0) <= 1.0
        for item in evidence
    )
    evidence_table = "\n".join(graph_module.render_evidence_table(evidence))
    check(
        "④ 证据表（来源 / 摘要 / 可信度）",
        bool(evidence) and evidence_fields_ok and "| 编号 | 结论 / 摘要 | 来源 | 定位 | 可信度 |" in evidence_table,
        f"{len(evidence)} 条证据，字段齐全；证据表已渲染（见 CLI 最终输出 / final_output.evidence）",
    )

    # 5. 结构化报告
    sections = ["## 摘要", "## 关键发现", "## 局限与未证实"]
    check(
        "⑤ 结构化 Markdown 报告",
        all(section in report for section in sections) and len(report) >= 200,
        f"报告 {len(report)} 字符，包含摘要/关键发现/局限与未证实",
    )

    # 6. 引用校验
    findings = report.split("## 关键发现", 1)[1].split("## ", 1)[0] if "## 关键发现" in report else ""
    bullets = [line.strip()[2:].strip() for line in findings.splitlines() if line.strip().startswith(("- ", "* "))]
    cited = [b for b in bullets if any(1 <= int(n) <= len(evidence) for n in re.findall(r"\[S(\d+)\]", b))]
    clickable = [b for b in bullets if re.search(r"\[S\d+\]\(https?://", b)]
    source_urls = {str(source.get("url")) for source in sources}
    links_valid = all(
        any(token in source_urls for token in re.findall(r"\[S\d+\]\((https?://[^)]+)\)", bullet))
        for bullet in clickable
    )
    check(
        "⑥ 引用校验：关键结论对应可点击来源",
        bool(bullets) and len(cited) == len(bullets) and len(clickable) == len(bullets) and links_valid,
        f"关键发现 {len(bullets)} 条，可追溯 {len(cited)} 条，可点击 {len(clickable)} 条，链接均来自抓取来源",
    )

    # 7. 人工确认：暂停 / 修改 / 继续 / 写操作门禁
    interrupt_types = [entry["type"] for entry in flow["interrupts"]]
    gate_ok = all(entry["outbox_files"] == 0 for entry in flow["interrupts"])
    actions = list(final.get("actions") or [])
    write_ok = len(actions) == 2 and all(action.get("ok") for action in actions)
    files_after = _outbox_files(flow["outbox"])
    revise_flow = _run_flow(entry, tag="revise", decide=_revise_once)
    revise_final = revise_flow["result"].get("final_output") or {}
    revise_report = str(revise_final.get("report") or "")
    revise_ok = (
        [entry["type"] for entry in revise_flow["interrupts"]].count("draft_review") == 2
        and revise_flow["result"].get("revision_count") == 1
        and "修订说明" in revise_report
        and revise_final.get("status") == "completed"
    )
    check(
        "⑦ 人工确认：暂停 / 修改 / 继续 + 写操作门禁",
        interrupt_types == ["draft_review", "email_confirmation", "notion_confirmation"]
        and gate_ok
        and write_ok
        and bool(files_after)
        and revise_ok,
        f"3 次暂停={interrupt_types}；确认前 outbox 为空={gate_ok}；写操作 {len(actions)} 个且成功={write_ok}；"
        f"修改分支重生成 1 次并继续完成={revise_ok}；写操作产物 {len(files_after)} 个",
    )

    # 8. 失败处理：重试 / 确定性失败不重试 / 模型报错降级
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("模拟网络超时")
        return "ok"

    ok_value, retry_attempts = tools.retry_call(flaky, sleep=lambda _s: None)
    non_retry = {"n": 0}

    def fatal() -> str:
        non_retry["n"] += 1
        raise tools.NonRetryableToolError("HTTP 404（页面不可用）")

    try:
        tools.retry_call(fatal, sleep=lambda _s: None)
        fatal_raised = False
    except tools.NonRetryableToolError:
        fatal_raised = True
    llm = LLMClient(mode="openai", api_key="acceptance-key")
    llm._chat = lambda *args, **kwargs: (_ for _ in ()).throw(LLMError("模拟模型报错"))
    fallback_text, fallback_mode = llm.draft_report(
        "人工中断如何实现",
        [{"claim": "结论 A", "source_url": "https://example.org/a", "quote": "原文", "confidence": 0.8}],
        [{"url": "https://example.org/a", "title": "来源 A"}],
    )
    check(
        "⑧ 失败处理：重试 + 降级",
        ok_value == "ok"
        and retry_attempts == 3
        and fatal_raised
        and non_retry["n"] == 1
        and fallback_mode == "offline_fallback"
        and "## 摘要" in fallback_text,
        f"可重试失败第 3 次成功（attempts={retry_attempts}）；404 不重试（attempts={non_retry['n']}）；"
        f"模型报错降级为模板（mode={fallback_mode}）",
    )

    # 9. 可观测：trace / 日志 / token / 成本 / 耗时
    log_fields = {
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
    }
    logs_ok = bool(logs) and all(field in logs[0] for field in log_fields)
    log_path = Path(str(result.get("run_log_path") or ""))
    jsonl_ok = log_path.exists() and len(log_path.read_text(encoding="utf-8").splitlines()) == len(logs)

    # token / 成本核算（用带 usage 的桩 LLM 验证口径）
    class _UsageLLM:
        mode = "openai"

        def __init__(self) -> None:
            self.usage = {"prompt_tokens": 110, "completion_tokens": 55, "total_tokens": 165, "calls": 2}

        def usage_snapshot(self) -> dict[str, int]:
            return dict(self.usage)

    audit_state = new_state("可观测性核算", trace_id="accept-usage", log_dir=str(ACCEPT_DIR / "logs"))
    audit_state["llm_usage_baseline"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "calls": 1}
    saved_get_llm = graph_module.get_llm
    graph_module.get_llm = lambda: _UsageLLM()
    try:
        usage_final = graph_module.finalize(audit_state)["final_output"]
    finally:
        graph_module.get_llm = saved_get_llm
    token_ok = (
        usage_final["token_usage"]["prompt_tokens"] == 100
        and usage_final["token_usage"]["completion_tokens"] == 50
        and abs(usage_final["cost_cny"] - (100 / 1_000_000 + 50 * 4 / 1_000_000)) < 1e-9
    )
    observability_ok = (
        bool(final.get("trace_id"))
        and logs_ok
        and jsonl_ok
        and "token_usage" in final
        and "cost_cny" in final
        and int(final.get("duration_ms") or 0) >= 0
        and token_ok
    )
    check(
        "⑨ 可观测：trace / 日志 / token / 成本 / 耗时",
        observability_ok,
        f"trace_id={final.get('trace_id')}；日志 {len(logs)} 条且字段齐全={logs_ok}；JSONL={jsonl_ok}；"
        f"token={final.get('token_usage', {}).get('total_tokens', 0)}；成本=¥{final.get('cost_cny', 0)}；耗时={final.get('duration_ms', 0)}ms；"
        f"成本口径校验={token_ok}",
    )

    _write_report(final, result, logs)


def _write_report(final: dict[str, Any], result: dict[str, Any], logs: list[dict[str, Any]]) -> None:
    passed = sum(1 for check_item in CHECKS if check_item["ok"])
    lines = [
        "# 功能验收报告（必须闭环）",
        "",
        "> 自动生成：`python evaluation/acceptance_check.py`。环境：离线 LLM + 确定性 mock 网页，"
        "不依赖外网，可重复执行；真实联网效果见 [evaluation/eval_report.md](evaluation/eval_report.md)。",
        "",
        f"## 结论：{passed}/{len(CHECKS)} 项通过",
        "",
        "| # | 验收项 | 结果 | 证据 |",
        "| --- | --- | --- | --- |",
    ]
    for index, item in enumerate(CHECKS, start=1):
        lines.append(f"| {index} | {item['item']} | {'✅ 通过' if item['ok'] else '❌ 未通过'} | {item['evidence']} |")
    lines += [
        "",
        "## 验收运行证据",
        "",
        f"- 研究问题：{final.get('research_question', '')}",
        f"- trace_id：{final.get('trace_id', '')}",
        f"- 日志条数：{len(logs)}；JSONL：`{result.get('run_log_path', '')}`",
        f"- 子问题：{len(final.get('sub_questions') or [])} 个",
        f"- 证据：{len(final.get('evidence') or [])} 条；来源：{len(final.get('sources') or [])} 个",
        f"- token：{(final.get('token_usage') or {}).get('total_tokens', 0)}；成本：¥{final.get('cost_cny', 0)}；耗时：{final.get('duration_ms', 0)} ms",
        f"- 写操作：{len([action for action in final.get('actions') or [] if action.get('ok')])} 个成功（dry-run）",
        "",
        "## 复现",
        "",
        "```bash",
        "python evaluation/acceptance_check.py",
        "```",
        "",
    ]
    (ROOT / "FUNCTIONAL_ACCEPTANCE.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwritten {ROOT / 'FUNCTIONAL_ACCEPTANCE.md'} ({passed}/{len(CHECKS)} passed)")


def main() -> int:
    run_checks()
    return 0 if all(item["ok"] for item in CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
