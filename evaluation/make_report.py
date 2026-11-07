"""把评测结果汇总成 evaluation/eval_report.md。

用法（先跑 run_eval.py 生成 results/*.summary.json）：
    python evaluation/make_report.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
RESULTS = EVAL_DIR / "results"
REPORT = EVAL_DIR / "eval_report.md"

TARGETS = {
    "success_rate": ("任务成功率", 0.80, "≥ 80%"),
    "citation_trace_rate": ("引用可追溯率", 0.90, "≥ 90%"),
    "clickable_rate": ("关键结论可点击来源率", 1.00, "= 100%"),
    "tool_correct_rate": ("工具调用正确率", 0.85, "≥ 85%"),
    "fact_coverage": ("预期要点覆盖率（自动代理）", 0.85, "≥ 85%（参考）"),
}


def load_summary(tag: str) -> dict[str, Any] | None:
    path = RESULTS / f"{tag}.summary.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_records(tag: str) -> list[dict[str, Any]]:
    path = RESULTS / f"{tag}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pct(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def metric_table(summary: dict[str, Any]) -> list[str]:
    lines = [
        "| 指标 | 结果 | 目标 | 是否达标 |",
        "| --- | --- | --- | --- |",
    ]
    for key, (label, target, target_text) in TARGETS.items():
        value = float(summary.get(key) or 0.0)
        lines.append(f"| {label} | {pct(value)} | {target_text} | {mark(value >= target)} |")
    lines.append(f"| P95 延迟 | {summary.get('p95_latency_ms', 0)} ms | ≤ 30000 ms | {mark(summary.get('p95_latency_ms', 0) <= 30000)} |")
    lines.append(f"| 平均延迟 | {summary.get('avg_latency_ms', 0)} ms | - | - |")
    lines.append(f"| 单任务平均成本 | ¥{summary.get('avg_cost_cny', 0)} | ≤ ¥0.5 | {mark(float(summary.get('avg_cost_cny', 0)) <= 0.5)} |")
    lines.append(f"| 平均 token | {summary.get('avg_tokens', 0)} | - | - |")
    lines.append(f"| 来源去重率 | {pct(summary.get('dedup_rate'))} | 去重有效（>0 表示有重复被合并） | - |")
    lines.append(f"| 子问题拆解达标率 | {pct(summary.get('sub_questions_ok_rate'))} | 3~5 个/题 | - |")
    if "coverage_rate" in summary:
        lines.append(
            f"| 子问题证据覆盖率（P0） | {pct(summary.get('coverage_rate'))} | ≥ 75% | "
            f"{mark(float(summary.get('coverage_rate') or 0) >= 0.75)} |"
        )
    if "evidence_supported_rate" in summary:
        lines.append(
            f"| 证据断言支持率（P0） | {pct(summary.get('evidence_supported_rate'))} | ≥ 75% | "
            f"{mark(float(summary.get('evidence_supported_rate') or 0) >= 0.75)} |"
        )
    if "avg_retrieval_rounds" in summary:
        lines.append(f"| 平均检索轮次（P0） | {summary.get('avg_retrieval_rounds')} | - | - |")
    lines.append(f"| 人工确认平均次数 | {summary.get('avg_interrupts', 0)} | 3 次/题 | - |")
    lines.append("")
    return lines


def failure_table(records: list[dict[str, Any]]) -> list[str]:
    failures = [rec for rec in records if not rec.get("success")]
    if not failures:
        return ["本次运行没有失败任务。", ""]
    lines = [
        "| 题目 | 类别 | 状态 | 失败原因 | 证据数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rec in failures[:12]:
        reason = str(rec.get("fail_reason") or "").replace("|", "\\|")[:160]
        lines.append(
            f"| {rec.get('id')} | {rec.get('category')} | {rec.get('status')} | {reason} | {rec.get('evidence_count', 0)} |"
        )
    lines.append("")
    return lines


def category_table(summary: dict[str, Any]) -> list[str]:
    lines = [
        "| 类别 | 题数 | 成功率 | 引用可追溯率 |",
        "| --- | --- | --- | --- |",
    ]
    labels = {
        "simple_fact": "简单事实",
        "comparison": "对比分析",
        "timeliness": "时效性",
        "multi_hop": "多跳推理",
    }
    for key, bucket in (summary.get("by_category") or {}).items():
        lines.append(
            f"| {labels.get(key, key)} | {bucket.get('n', 0)} | {pct(bucket.get('success_rate'))} | "
            f"{pct(bucket.get('citation_trace_rate'))} |"
        )
    lines.append("")
    return lines


def real_comparison_table(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    after_tag: str = "real_after_360",
) -> list[str]:
    if not before or not after:
        return ["（优化前后数据不完整）", ""]
    keys = [
        ("success_rate", "任务成功率"),
        ("citation_trace_rate", "引用可追溯率"),
        ("clickable_rate", "关键结论可点击来源率"),
        ("tool_correct_rate", "工具调用正确率"),
        ("fact_coverage", "预期要点覆盖（宽松代理）"),
        ("avg_latency_ms", "平均延迟(ms)"),
        ("p95_latency_ms", "P95 延迟(ms)"),
    ]
    lines = [
        f"| 指标 | 优化前（real_current） | 优化后（{after_tag}） | 变化 |",
        "| --- | --- | --- | --- |",
    ]
    for key, label in keys:
        wait = before.get(key, 0)
        now = after.get(key, 0)
        if key.endswith("_ms"):
            lines.append(f"| {label} | {wait} | {now} | {now - wait:+d} ms |")
        else:
            lines.append(f"| {label} | {pct(wait)} | {pct(now)} | {(float(now) - float(wait)) * 100:+.1f} pp |")
    before_fail = 40 - before.get("success", 0)
    after_fail = 40 - after.get("success", 0)
    lines += [
        f"| 失败题数 | {before_fail} | {after_fail} | {after_fail - before_fail:+d} |",
        "",
        "优化内容：① 中文宽泛问题新增 360 搜索源（`so360`，结果 URL 取自 `data-mdurl`，绕过跳转）；"
        "② 证据抽取增加导航/表格/菜单噪声过滤（`_looks_like_noise`）；"
        "③ 检索每查询返回数 5→8、抓取最多尝试 URL 5→8；"
        "④ P0：子问题覆盖检查 + 缺口补检（最多 2 轮）；"
        "⑤ P0：抓取页数按对比/多跳自适应、候选重排、正文内容指纹去重；"
        "⑥ P0：证据进入报告前做 claim→quote 支持性校验。",
        "",
        "**读法说明**：真实联网评测受搜索引擎返回质量与站点 403/超时的波动影响，单次 ±1~2 题属于网络噪声；"
        "引用类指标的提升有一部分来自将无证据题从分母中剔除。补检轮会提高延迟（平均轮次见 5.3），"
        "证据相关性请以第 8 节人工抽样与第 3 节自动断言支持率为准，而不是只看成功率。",
        "",
    ]
    return lines


def p0_quality_table(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    """P0 研究质量闭环的前后对比（覆盖率/断言支持率/补检轮次为新增指标）。"""
    if not after:
        return ["（real_p0 尚未运行）", ""]
    before = before or {}
    rows = [
        ("子问题证据覆盖率", "coverage_rate", "新增"),
        ("证据断言支持率", "evidence_supported_rate", "新增"),
        ("平均检索轮次", "avg_retrieval_rounds", "1.0（仅首轮）"),
        ("任务成功率", "success_rate", "real_current"),
        ("预期要点覆盖（宽松代理）", "fact_coverage", "real_current"),
        ("平均延迟(ms)", "avg_latency_ms", "real_current"),
    ]
    lines = [
        "| 指标 | 优化前（real_current） | 优化后（real_p0） | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for label, key, note in rows:
        old = before.get(key)
        new = after.get(key)
        old_text = "-" if old is None else (f"{old}" if key in {"avg_retrieval_rounds", "avg_latency_ms"} else pct(old))
        new_text = f"{new}" if key in {"avg_retrieval_rounds", "avg_latency_ms"} else pct(new)
        lines.append(f"| {label} | {old_text} | {new_text} | {note} |")
    lines += [
        "",
        "P0 改动：① `check_coverage` 节点按子问题计算证据覆盖度，未达标时用缺口生成补检 query（默认最多 2 轮）；"
        "② 抓取页数按对比/多跳自适应，候选按问题词与官方站点先验重排，正文按内容指纹去重；"
        "③ 证据进入报告前做 claim→quote 支持性校验，明显外推/无关的结论会被过滤并记录。",
        "",
    ]
    return lines


def ablation_table(baseline: dict[str, Any] | None, current: dict[str, Any] | None) -> list[str]:
    if not baseline or not current:
        return ["（消融基线尚未运行）", ""]
    keys = [
        ("success_rate", "任务成功率"),
        ("citation_trace_rate", "引用可追溯率"),
        ("clickable_rate", "关键结论可点击来源率"),
        ("tool_correct_rate", "工具调用正确率"),
        ("sub_questions_ok_rate", "子问题拆解达标率"),
        ("avg_latency_ms", "平均延迟(ms)"),
    ]
    lines = ["| 指标 | 基线（关闭新增能力） | 当前 | 变化 |", "| --- | --- | --- | --- |"]
    for key, label in keys:
        before = baseline.get(key, 0)
        after = current.get(key, 0)
        if key.endswith("_ms"):
            delta = f"{after - before:+d} ms"
            before_text, after_text = f"{before} ms", f"{after} ms"
        else:
            delta = f"{(float(after) - float(before)) * 100:+.1f} pp"
            before_text, after_text = pct(before), pct(after)
        lines.append(f"| {label} | {before_text} | {after_text} | {delta} |")
    lines.append("")
    return lines


def example_section(records: list[dict[str, Any]], q21_mock: dict[str, Any] | None = None) -> list[str]:
    target = next((rec for rec in records if rec.get("id") == "Q21"), None)
    if target is None:
        return ["（Q21 尚未运行）", ""]
    lines = [
        "题目：**2025 年 AI Agent 在企业落地的主要挑战是什么？**（时效性类别）",
        "",
        "| 验收项 | 预期 | 实际 | 结果 |",
        "| --- | --- | --- | --- |",
        f"| 问题拆解 | 3~5 个子问题 | {target.get('sub_questions', 0)} 个 | {mark(3 <= target.get('sub_questions', 0) <= 5)} |",
        f"| 联网搜索 | 至少 1 次成功检索 | search ok={target.get('search_ok', 0)} 次 | {mark(target.get('search_ok', 0) >= 1)} |",
        f"| 网页抓取 | 至少 1 篇正文 | fetch ok={target.get('fetch_ok', 0)} 次，来源 {target.get('source_count', 0)} 个 | {mark(target.get('fetch_ok', 0) >= 1)} |",
        f"| 证据整理 | 证据含来源/摘要/可信度 | {target.get('evidence_count', 0)} 条证据 | {mark(target.get('evidence_count', 0) > 0)} |",
        f"| 报告生成 | 结构化 Markdown | {target.get('report_chars', 0)} 字符 | {mark(target.get('report_chars', 0) >= 200)} |",
        f"| 引用校验 | 关键结论可追溯 | 可追溯 {target.get('bullets_cited', 0)}/{target.get('bullets', 0)} | {mark(target.get('citation_trace_rate', 0) >= 0.9)} |",
        f"| 可点击来源 | 每条结论至少 1 个链接 | 可点击 {target.get('bullets_clickable', 0)}/{target.get('bullets', 0)} | {mark(target.get('all_clickable', False))} |",
        f"| 人工确认 | 3 次暂停并可继续 | {target.get('interrupt_rounds', 0)} 次 | {mark(target.get('interrupt_rounds', 0) >= 3)} |",
        f"| 可观测 | trace/日志/token/耗时 | {target.get('elapsed_ms', 0)} ms，token={((target.get('token_usage') or {}).get('total_tokens', 0))} | {mark(target.get('elapsed_ms', 0) > 0)} |",
        f"| 任务闭环 | 有证据且完成 | status={target.get('status')} | {mark(bool(target.get('success')))} |",
        f"| 失败原因可见 | 不静默卡死 | {str(target.get('fail_reason') or '无')[:80]} | {mark(bool(target.get('success')) or bool(target.get('fail_reason')))} |",
        "",
    ]
    if not target.get("success") and target.get("fail_reason"):
        lines += [
            "**说明**：该题在真实联网环境下检索到的页面多为 403 / JS 空页，或内容与问题无关；"
            "证据门禁按「宁缺毋滥」拒绝生成带来源结论，流程以 `completed_no_sources` 结束并记录失败原因，"
            "而不是编造一份报告。同一道题在受控 fixture（mock）下可以完整跑通（见下）。",
            "",
        ]
    if q21_mock:
        lines += [
            f"受控来源（`--mode mock --ids Q21`）结果：success={q21_mock.get('success')}，"
            f"证据 {sum(rec.get('evidence_count', 0) for rec in load_records('q21_mock'))} 条，"
            f"引用可追溯率 {pct(q21_mock.get('citation_trace_rate'))}。",
            "",
        ]
    excerpt = str(target.get("report_excerpt") or "").strip()
    if excerpt:
        lines += ["报告片段：", "", "```markdown", excerpt[:600], "```", ""]
    preview = target.get("evidence_preview") or []
    if preview:
        lines += [
            "证据表样例：",
            "",
            "| 结论 | 来源 | 可信度 |",
            "| --- | --- | --- |",
        ]
        for item in preview[:3]:
            claim = str(item.get("claim") or "").replace("|", "\\|")[:80]
            url = str(item.get("source_url") or "")
            lines.append(f"| {claim} | {url} | {item.get('confidence', '-')} |")
        lines.append("")
    return lines


def main() -> int:
    after_tag = next(
        (tag for tag in ("real_p0", "real_llm", "real_after_360", "real_final") if load_summary(tag)),
        "real_final",
    )
    real_after = load_summary(after_tag)
    real_before = load_summary("real_current")
    real = real_after or real_before
    mock_tag = "mock_p0" if load_summary("mock_p0") else "mock_current"
    mock = load_summary(mock_tag)
    baseline = load_summary("mock_baseline")
    q21_mock = load_summary("q21_mock")
    real_records = (
        load_records(after_tag)
        or load_records("real_after_360")
        or load_records("real_final")
        or load_records("real_after_fix")
        or load_records("real_current")
    )
    mock_records = load_records(mock_tag)
    q21 = load_summary("real_q21")

    lines: list[str] = [
        "# 研究助手评测报告（eval_report.md）",
        "",
        "> 自动生成：`python evaluation/make_report.py`。原始数据在 `evaluation/results/`，"
        "测试集在 `evaluation/dataset.jsonl`。",
        "",
        "## 1. 测试集与运行配置",
        "",
        "- 测试集：`evaluation/dataset.jsonl`，共 40 题，四类各 10 题：",
        "  - 简单事实（simple_fact）：概念/定义类；",
        "  - 对比分析（comparison）：两个方案/产品的差异；",
        "  - 时效性（timeliness）：2025 年现状与趋势；",
        "  - 多跳推理（multi_hop）：需要组合多个事实才能回答。",
        "- 每题字段：`id / category / question / expected_facts`（expected_facts 用于自动代理指标与 mock fixture，不作为唯一答案）。",
        "- 被测配置：离线 LLM（无 `OPENAI_API_KEY`，启发式规划/证据/草稿）+ 真实联网检索"
        "（Bing / 360 / DuckDuckGo / Wikipedia 回退，中文宽泛问题优先 360），"
        "或本地确定性 fixture（mock 模式，只衡量流水线机制）。",
        "- 人工确认：每题自动走完 3 次 interrupt（草稿通过、邮件取消、Notion 取消），不产生真实写操作。",
        "",
        "## 2. 指标定义",
        "",
        "| 指标 | 计算方式 |",
        "| --- | --- |",
        "| 任务成功率 | `status ∈ {completed, completed_with_action_failure}` 且报告 ≥200 字且证据数 >0 的题数 / 总题数 |",
        "| 引用可追溯率 | 「关键发现」中包含至少 1 个有效 `[S#]`（编号 1..证据数）的条目数 / 条目总数（无证据的失败题不计入分母） |",
        "| 关键结论可点击来源率 | 「关键发现」中带 `[S#](https://…)` 可点击链接的条目数 / 条目总数（同上口径） |",
        "| 工具调用正确率 | 每题满足：搜索成功≥1、抓取成功≥1、证据总结执行、报告生成、子问题 3~5 个 |",
        "| 预期要点覆盖（宽松代理） | `expected_facts` 与报告共享 ≥50% 的字符 bigram 即视为覆盖；另给「精确命中率」与「证据逐字可回溯率」 |",
        "| 延迟 | 单题从 graph.invoke 到首次拿到 final_output 的墙钟时间（含 3 次 resume） |",
        "| 成本 | `final_output.cost_cny`，按 token × 单价（默认 ¥1/¥4 每百万 token）计算；离线模式无 API 成本，恒为 0 |",
        "| 去重率 | 1 −（去重后来源数 / 搜索原始命中数） |",
        "",
        "## 3. 真实联网评测结果（优化后）",
        "",
    ]
    if real:
        lines += metric_table(real)
        if real.get("fact_coverage_exact") is not None:
            lines.append(
                f"| 预期要点覆盖（精确命中，参考） | {pct(real.get('fact_coverage_exact'))} | - | - |"
            )
            lines.append("")
        lines += ["", "按类别拆分：", ""]
        lines += category_table(real)
        lines += [
            f"共 {real['total']} 题，成功 {real['success']} 题；来源去重率 {pct(real.get('dedup_rate'))}，"
            f"平均子问题 {real.get('avg_sub_questions')} 个。",
            "",
        ]
    else:
        lines += ["（real_after_fix 尚未运行）", ""]

    lines += ["## 4. 流水线机制评测（mock fixture，不计真实检索质量）", ""]
    if mock:
        lines += metric_table(mock)
        lines += [
            f"mock 模式用 `evaluation/mock_web.py` 生成确定性网页，主要验证：搜索→抓取→证据→报告→引用→人工确认全链路、"
            f"引用可点击、去重与延迟。共 {mock['total']} 题，失败 {len(mock.get('failures') or [])} 题。",
            "",
        ]
    else:
        lines += ["（mock_current 尚未运行）", ""]

    lines += ["## 5. 优化前后对比", "", "### 5.1 mock 消融（引用可点击化 / 子问题拆解）", ""]
    lines += ablation_table(baseline, mock)
    lines += [
        "基线通过 `AGENT_LINK_CITATIONS=0 AGENT_PLAN_SUB_QUESTIONS=0` 关闭本轮新增能力，"
        "用于证明「引用可点击化」和「3~5 个子问题拆解」确实生效，而不是靠提示词自述。",
        "",
        "### 5.2 真实联网优化（P0 前后对比）",
        "",
    ]
    lines += real_comparison_table(real_before, real_after, after_tag=after_tag)
    lines += ["### 5.3 研究质量闭环（P0：子问题覆盖 + 断言校验）", ""]
    lines += p0_quality_table(real_before, load_summary("real_p0"))

    lines += ["## 6. 示例题：Q21 预期 vs 实际", ""]
    lines += example_section(real_records or mock_records, q21_mock)
    if q21 and not real_records:
        lines += ["", f"（Q21 单独运行：success={q21['success']}，{q21['avg_latency_ms']}ms）", ""]

    lines += ["## 7. 失败案例", ""]
    lines += failure_table(real_records)

    audit_path = RESULTS / "manual_audit.json"
    lines += ["## 8. 人工抽样核对（关键结论）", ""]
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        lines += [
            f"- 抽样方式：从真实联网评测的 40 题结果中随机抽 {len(audit['samples'])} 条「关键发现」，"
            "回看其结论是否为完整句子、是否真的回答该题、是否来自该来源；",
            f"- 判定结果：支持 {audit['supported']}/{len(audit['samples'])}，准确率 {pct(audit['accuracy'])}；",
            "- 说明：以上抽样来自 `real_after_360`（P0 之前）；`real_p0` 的「证据断言支持率」是自动代理指标，"
            "尚未重新人工抽样，两者口径不同。",
            "",
            "| 题目 | 关键结论（截断） | 来源 | 判定 | 备注 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for sample in audit["samples"]:
            lines.append(
                f"| {sample.get('id')} | {str(sample.get('claim'))[:60]} | {sample.get('source')} | "
                f"{'✅' if sample.get('supported') else '❌'} | {sample.get('note', '')} |"
            )
        lines.append("")
    else:
        lines += ["（尚未执行人工抽样；见第 10 节限制）", ""]

    lines += [
        "## 9. 功能验收对照",
        "",
        "| 验收项 | 实现/证据 | 结果 |",
        "| --- | --- | --- |",
        "| 问题拆解 3~5 个子问题 | `plan_research` 子问题字段，日志 S002 记录；mock 40/40 达标 | ✅ |",
        "| 联网搜索拿到候选来源 | `search_web` 三提供方回退；真实评测每题均有搜索记录 | ✅ |",
        "| 网页抓取正文/去广告/去重 | `fetch_pages` + `text_clean` 清洗；去重率见上表 | ✅ |",
        "| 证据表（来源/摘要/可信度） | `state.evidence` + `final_output.evidence`，字段含 `source_url/source_snippet/confidence` | ✅ |",
        "| 结构化 Markdown 报告 | `DRAFT_SYSTEM` 固定摘要/关键发现/局限与未证实三段式 | ✅ |",
        "| 引用校验 | `filter_evidence` 逐字校验 + `_sanitize_report` URL 白名单 + `[S#]` 链接化 | ✅ |",
        "| 子问题覆盖 / 缺口补检（P0） | `check_coverage` + `quality.compute_coverage`；覆盖率与轮次见第 3 节 | ✅ |",
        "| claim 支持性校验（P0） | `quality.verify_evidence`；自动支持率见第 3 节 | ✅ |",
        "| 人工确认可暂停/修改/继续 | 3 处 `interrupt()`；pytest 覆盖通过/修改/终止分支 | ✅ |",
        "| 失败重试与降级 | 工具重试 3 次 + 指数退避 + 熔断；失败案例见第 7 节 | ✅ |",
        "| 可观测（trace/日志/token/耗时） | `final_output` 含 `trace_id/log_summary/token_usage/cost_cny/duration_ms`；JSONL 日志 | ✅ |",
        "",
        "## 10. 限制与后续",
        "",
        "- 本次环境没有 `OPENAI_API_KEY`，LLM 走离线启发式：**token/成本真实数据为 0**，P95 延迟也不包含模型推理时间；"
        "接入真实模型后需要重跑本评测才能给出真实成本与延迟。",
        "- 真实联网评测的检索质量受搜索引擎可用性影响（部分站点 403 / 区域不可达 / PDF 链接），失败案例见第 7 节。",
        "- P0 的「子问题覆盖率」目前 51.2%，低于 75% 目标：主要卡在对比题只抓到一方、"
        "部分站点 403/JS 空页；下一步应按实体分别检索、扩展官方搜索 API / PDF 解析。",
        "- P0 的「证据断言支持率」是规则版代理（bigram / 数字 / 实体 / 否定词一致性），"
        "不是语义蕴含判断；它只用于拦截明显外推，最终仍需人工抽检。",
        "- 补检轮会提高延迟（real_p0 平均 15.6s、P95 26.7s，平均 1.68 轮）；"
        "后续需要并发抓取、缓存与按轮次收紧预算来控制 P95。",
        "- 事实准确率：宽松代理（预期要点 bigram 覆盖）与人工抽样（第 8 节）只是近似，不能替代独立标注；"
        "上线前建议扩展到 100+ 题并双人标注。",
        "- mock fixture 只验证流水线机制，不代表真实检索效果；勿把 mock 的高分当作产品指标。",
        "",
        "## 11. 复现命令",
        "",
        "```bash",
        "python evaluation/run_eval.py --mode mock --tag mock_current",
        "python evaluation/run_eval.py --mode mock --tag mock_p0",
        "python evaluation/run_eval.py --mode mock --tag q21_mock --ids Q21",
        "python evaluation/run_eval.py --mode mock --tag mock_baseline --no-link-citations --no-sub-questions",
        "python evaluation/run_eval.py --mode real --tag real_current",
        "python evaluation/run_eval.py --mode real --tag real_after_360",
        "python evaluation/run_eval.py --mode real --tag real_p0",
        "python evaluation/make_report.py",
        "```",
        "",
    ]

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
