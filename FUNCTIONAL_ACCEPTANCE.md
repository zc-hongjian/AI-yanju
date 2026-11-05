# 功能验收报告（必须闭环）

> 自动生成：`python evaluation/acceptance_check.py`。环境：离线 LLM + 确定性 mock 网页，不依赖外网，可重复执行；真实联网效果见 [evaluation/eval_report.md](evaluation/eval_report.md)。

## 结论：9/9 项通过

| # | 验收项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | ① 问题拆解 3~5 个子问题 | ✅ 通过 | 4 个：['LangGraph 的 interrupt 函数的核心结论与主要事实', 'LangGraph 的 interrupt 函数的关键数据与最新进展', 'LangGraph 的 interrupt 函数的主要挑战与原因', 'LangGraph 的 interrupt 函数有哪些权威来源与案例'] |
| 2 | ② 联网搜索拿到候选来源 | ✅ 通过 | search_web 成功 2 次，候选 3 条（去重后 URL 3 个） |
| 3 | ③ 网页抓取：正文 / 去广告 / 去重 | ✅ 通过 | 抓取成功 3 页；广告/导航/脚本已移除，重复段落已合并，正文 140 字符；来源 URL 唯一=True；搜索候选 URL 唯一=True |
| 4 | ④ 证据表（来源 / 摘要 / 可信度） | ✅ 通过 | 3 条证据，字段齐全；证据表已渲染（见 CLI 最终输出 / final_output.evidence） |
| 5 | ⑤ 结构化 Markdown 报告 | ✅ 通过 | 报告 931 字符，包含摘要/关键发现/局限与未证实 |
| 6 | ⑥ 引用校验：关键结论对应可点击来源 | ✅ 通过 | 关键发现 2 条，可追溯 2 条，可点击 2 条，链接均来自抓取来源 |
| 7 | ⑦ 人工确认：暂停 / 修改 / 继续 + 写操作门禁 | ✅ 通过 | 3 次暂停=['draft_review', 'email_confirmation', 'notion_confirmation']；确认前 outbox 为空=True；写操作 2 个且成功=True；修改分支重生成 1 次并继续完成=True；写操作产物 2 个 |
| 8 | ⑧ 失败处理：重试 + 降级 | ✅ 通过 | 可重试失败第 3 次成功（attempts=3）；404 不重试（attempts=1）；模型报错降级为模板（mode=offline_fallback） |
| 9 | ⑨ 可观测：trace / 日志 / token / 成本 / 耗时 | ✅ 通过 | trace_id=accept-approve-b5a886；日志 15 条且字段齐全=True；JSONL=True；token=0；成本=¥0.0；耗时=64ms；成本口径校验=True |

## 验收运行证据

- 研究问题：LangGraph 的 interrupt 函数有什么作用？
- trace_id：accept-approve-b5a886
- 日志条数：15；JSONL：`D:\Build\AI-hy\evaluation\results\acceptance\approve\logs\research-accept-approve-b5a886.jsonl`
- 子问题：4 个
- 证据：3 条；来源：3 个
- token：0；成本：¥0.0；耗时：64 ms
- 写操作：2 个成功（dry-run）

## 复现

```bash
python evaluation/acceptance_check.py
```
