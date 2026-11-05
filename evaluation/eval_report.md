# 研究助手评测报告（eval_report.md）

> 自动生成：`python evaluation/make_report.py`。原始数据在 `evaluation/results/`，测试集在 `evaluation/dataset.jsonl`。

## 1. 测试集与运行配置

- 测试集：`evaluation/dataset.jsonl`，共 40 题，四类各 10 题：
  - 简单事实（simple_fact）：概念/定义类；
  - 对比分析（comparison）：两个方案/产品的差异；
  - 时效性（timeliness）：2025 年现状与趋势；
  - 多跳推理（multi_hop）：需要组合多个事实才能回答。
- 每题字段：`id / category / question / expected_facts`（expected_facts 用于自动代理指标与 mock fixture，不作为唯一答案）。
- 被测配置：离线 LLM（无 `OPENAI_API_KEY`，启发式规划/证据/草稿）+ 真实联网检索（Bing / 360 / DuckDuckGo / Wikipedia 回退，中文宽泛问题优先 360），或本地确定性 fixture（mock 模式，只衡量流水线机制）。
- 人工确认：每题自动走完 3 次 interrupt（草稿通过、邮件取消、Notion 取消），不产生真实写操作。

## 2. 指标定义

| 指标 | 计算方式 |
| --- | --- |
| 任务成功率 | `status ∈ {completed, completed_with_action_failure}` 且报告 ≥200 字且证据数 >0 的题数 / 总题数 |
| 引用可追溯率 | 「关键发现」中包含至少 1 个有效 `[S#]`（编号 1..证据数）的条目数 / 条目总数（无证据的失败题不计入分母） |
| 关键结论可点击来源率 | 「关键发现」中带 `[S#](https://…)` 可点击链接的条目数 / 条目总数（同上口径） |
| 工具调用正确率 | 每题满足：搜索成功≥1、抓取成功≥1、证据总结执行、报告生成、子问题 3~5 个 |
| 预期要点覆盖（宽松代理） | `expected_facts` 与报告共享 ≥50% 的字符 bigram 即视为覆盖；另给「精确命中率」与「证据逐字可回溯率」 |
| 延迟 | 单题从 graph.invoke 到首次拿到 final_output 的墙钟时间（含 3 次 resume） |
| 成本 | `final_output.cost_cny`，按 token × 单价（默认 ¥1/¥4 每百万 token）计算；离线模式无 API 成本，恒为 0 |
| 去重率 | 1 −（去重后来源数 / 搜索原始命中数） |

## 3. 真实联网评测结果（优化后）

| 指标 | 结果 | 目标 | 是否达标 |
| --- | --- | --- | --- |
| 任务成功率 | 82.5% | ≥ 80% | ✅ |
| 引用可追溯率 | 100.0% | ≥ 90% | ✅ |
| 关键结论可点击来源率 | 100.0% | = 100% | ✅ |
| 工具调用正确率 | 87.5% | ≥ 85% | ✅ |
| 预期要点覆盖率（自动代理） | 21.2% | ≥ 85%（参考） | ❌ |
| P95 延迟 | 19802 ms | ≤ 30000 ms | ✅ |
| 平均延迟 | 9821 ms | - | - |
| 单任务平均成本 | ¥0.0 | ≤ ¥0.5 | ✅ |
| 平均 token | 0.0 | - | - |
| 来源去重率 | 18.0% | 去重有效（>0 表示有重复被合并） | - |
| 子问题拆解达标率 | 100.0% | 3~5 个/题 | - |
| 人工确认平均次数 | 3.0 | 3 次/题 | - |

| 预期要点覆盖（精确命中，参考） | 0.0% | - | - |


按类别拆分：

| 类别 | 题数 | 成功率 | 引用可追溯率 |
| --- | --- | --- | --- |
| 简单事实 | 10 | 100.0% | 100.0% |
| 对比分析 | 10 | 70.0% | 100.0% |
| 时效性 | 10 | 100.0% | 100.0% |
| 多跳推理 | 10 | 60.0% | 100.0% |

共 40 题，成功 33 题；来源去重率 18.0%，平均子问题 4.0 个。

## 4. 流水线机制评测（mock fixture，不计真实检索质量）

| 指标 | 结果 | 目标 | 是否达标 |
| --- | --- | --- | --- |
| 任务成功率 | 100.0% | ≥ 80% | ✅ |
| 引用可追溯率 | 100.0% | ≥ 90% | ✅ |
| 关键结论可点击来源率 | 100.0% | = 100% | ✅ |
| 工具调用正确率 | 100.0% | ≥ 85% | ✅ |
| 预期要点覆盖率（自动代理） | 100.0% | ≥ 85%（参考） | ✅ |
| P95 延迟 | 22 ms | ≤ 30000 ms | ✅ |
| 平均延迟 | 20 ms | - | - |
| 单任务平均成本 | ¥0.0 | ≤ ¥0.5 | ✅ |
| 平均 token | 0.0 | - | - |
| 来源去重率 | 50.4% | 去重有效（>0 表示有重复被合并） | - |
| 子问题拆解达标率 | 100.0% | 3~5 个/题 | - |
| 人工确认平均次数 | 3.0 | 3 次/题 | - |

mock 模式用 `evaluation/mock_web.py` 生成确定性网页，主要验证：搜索→抓取→证据→报告→引用→人工确认全链路、引用可点击、去重与延迟。共 40 题，失败 0 题。

## 5. 优化前后对比

### 5.1 mock 消融（引用可点击化 / 子问题拆解）

| 指标 | 基线（关闭新增能力） | 当前 | 变化 |
| --- | --- | --- | --- |
| 任务成功率 | 100.0% | 100.0% | +0.0 pp |
| 引用可追溯率 | 100.0% | 100.0% | +0.0 pp |
| 关键结论可点击来源率 | 0.0% | 100.0% | +100.0 pp |
| 工具调用正确率 | 0.0% | 100.0% | +100.0 pp |
| 子问题拆解达标率 | 0.0% | 100.0% | +100.0 pp |
| 平均延迟(ms) | 20 ms | 20 ms | +0 ms |

基线通过 `AGENT_LINK_CITATIONS=0 AGENT_PLAN_SUB_QUESTIONS=0` 关闭本轮新增能力，用于证明「引用可点击化」和「3~5 个子问题拆解」确实生效，而不是靠提示词自述。

### 5.2 真实联网优化（证据噪声过滤 / 抓取候选扩容）

| 指标 | 优化前（real_current） | 优化后（real_after_360） | 变化 |
| --- | --- | --- | --- |
| 任务成功率 | 77.5% | 82.5% | +5.0 pp |
| 引用可追溯率 | 93.0% | 100.0% | +7.0 pp |
| 关键结论可点击来源率 | 93.0% | 100.0% | +7.0 pp |
| 工具调用正确率 | 92.5% | 87.5% | -5.0 pp |
| 预期要点覆盖（宽松代理） | 0.0% | 21.2% | +21.2 pp |
| 平均延迟(ms) | 9289 | 9821 | +532 ms |
| P95 延迟(ms) | 29602 | 19802 | -9800 ms |
| 失败题数 | 9 | 7 | -2 |

优化内容：① 中文宽泛问题新增 360 搜索源（`so360`，结果 URL 取自 `data-mdurl`，绕过跳转）；② 证据抽取增加导航/表格/菜单噪声过滤（`_looks_like_noise`）；③ 检索每查询返回数 5→8、抓取最多尝试 URL 5→8，提高绕过 403/JS 空页面的概率。

**读法说明**：真实联网评测受搜索引擎返回质量与站点 403/超时的波动影响，单次 ±1~2 题属于网络噪声；引用类指标的提升有一部分来自将无证据题从分母中剔除。证据相关性请以第 8 节人工抽样为准，而不是只看成功率。

## 6. 示例题：Q21 预期 vs 实际

题目：**2025 年 AI Agent 在企业落地的主要挑战是什么？**（时效性类别）

| 验收项 | 预期 | 实际 | 结果 |
| --- | --- | --- | --- |
| 问题拆解 | 3~5 个子问题 | 4 个 | ✅ |
| 联网搜索 | 至少 1 次成功检索 | search ok=2 次 | ✅ |
| 网页抓取 | 至少 1 篇正文 | fetch ok=1 次，来源 1 个 | ✅ |
| 证据整理 | 证据含来源/摘要/可信度 | 2 条证据 | ✅ |
| 报告生成 | 结构化 Markdown | 1243 字符 | ✅ |
| 引用校验 | 关键结论可追溯 | 可追溯 2/2 | ✅ |
| 可点击来源 | 每条结论至少 1 个链接 | 可点击 2/2 | ✅ |
| 人工确认 | 3 次暂停并可继续 | 3 次 | ✅ |
| 可观测 | trace/日志/token/耗时 | 5423 ms，token=0 | ✅ |
| 任务闭环 | 有证据且完成 | status=completed | ✅ |
| 失败原因可见 | 不静默卡死 | 无 | ✅ |

受控来源（`--mode mock --ids Q21`）结果：success=1，证据 3 条，引用可追溯率 100.0%。

报告片段：

```markdown
# 研究报告：2025 年 AI Agent 在企业落地的主要挑战是什么？

## 摘要

对此，风投 公司Dragonfly合伙人Haseeb Qureshi近期就评论表示，当前席卷业界的「AI Agents」热潮或许能延续到2025年，但最终将难以为继，真正有影响力的将是AI在软件工程与安全领域的应用：AI Agents最终会消亡，这些并不是真正的agent，只是附有memecoin的聊天机器人，除了在Twitter上发文外，它们几乎没有任何代理性[S1](https://www.jb51.net/blockchain/968771.html)。对于AI Agent，《经济学人》引述AI数据公司Scale的Alex Wang表示：从聊天行动阶段突破，可能成为2025年科技界最为重要的成就之一，这种预期已经推高了像Salesforce这类巨头的股价上涨，该公司表示，其工作场景AI Agent自推出以来，一周内就与超过200位客户达成协议，连微软这一强大竞争对手也相继发布了众多AI Agent[S2](https://www.jb51.net/blockchain/968771.html)。

## 关键发现

- 对此，风投 公司Dragonfly合伙人Haseeb Qureshi近期就评论表示，当前席卷业界的「AI Agents」热潮或许能延续到2025年，但最终将难以为继，真
```

证据表样例：

| 结论 | 来源 | 可信度 |
| --- | --- | --- |
| 对此，风投 公司Dragonfly合伙人Haseeb Qureshi近期就评论表示，当前席卷业界的「AI Agents」热潮或许能延续到2025年，但最终将难以 | https://www.jb51.net/blockchain/968771.html | 0.51 |
| 对于AI Agent，《经济学人》引述AI数据公司Scale的Alex Wang表示：从聊天行动阶段突破，可能成为2025年科技界最为重要的成就之一，这种预期已 | https://www.jb51.net/blockchain/968771.html | 0.51 |

## 7. 失败案例

| 题目 | 类别 | 状态 | 失败原因 | 证据数 |
| --- | --- | --- | --- | --- |
| Q12 | comparison | completed_no_sources | 无可用证据（检索/抓取/证据提取失败）；[fetch_pages] fetch_page 重试 3 次后仍失败：HTTP 521；[fetch_pages] fetch_page 重试 3 次后仍失败：HTTP 521 | 0 |
| Q13 | comparison | completed_no_sources | 无可用证据（检索/抓取/证据提取失败）；[fetch_pages] 不可重试失败：HTTP 403（页面不可用）；[fetch_pages] 不可重试失败：正文提取失败（内容过短，可能是 JS 渲染页面或编码无法识别） | 0 |
| Q20 | comparison | completed_no_sources | 无可用证据（检索/抓取/证据提取失败）；[fetch_pages] 不可重试失败：SSL 证书校验失败：HTTPSConnectionPool(host='blog.csdn.net', port=443): Max retries exceeded with url: /mobangde/article/detail | 0 |
| Q33 | multi_hop | completed_no_sources | 无可用证据（检索/抓取/证据提取失败）；[fetch_pages] 不可重试失败：SSL 证书校验失败：HTTPSConnectionPool(host='blog.csdn.net', port=443): Max retries exceeded with url: /2501_91474102/article/d | 0 |
| Q34 | multi_hop | completed_no_sources | 无可用证据（检索/抓取/证据提取失败）；[fetch_pages] 不可重试失败：SSL 证书校验失败：HTTPSConnectionPool(host='blog.csdn.net', port=443): Max retries exceeded with url: /gitblog_00330/article/d | 0 |
| Q35 | multi_hop | completed_no_sources | 无可用证据（检索/抓取/证据提取失败）；[fetch_pages] 不可重试失败：SSL 证书校验失败：HTTPSConnectionPool(host='blog.csdn.net', port=443): Max retries exceeded with url: /weixin_32830601/article | 0 |
| Q37 | multi_hop | completed_no_sources | 无可用证据（检索/抓取/证据提取失败）；[fetch_pages] 不可重试失败：SSL 证书校验失败：HTTPSConnectionPool(host='blog.csdn.net', port=443): Max retries exceeded with url: /weixin_42526087/article | 0 |

## 8. 人工抽样核对（关键结论）

- 抽样方式：从真实联网评测的 40 题结果中随机抽 12 条「关键发现」，回看其结论是否为完整句子、是否真的回答该题、是否来自该来源；
- 判定结果：支持 5/12，准确率 41.7%；

| 题目 | 关键结论（截断） | 来源 | 判定 | 备注 |
| --- | --- | --- | --- | --- |
| Q01 | LangGraph 是一个低级别的编排框架，用于构建、管理和部署长时间运行、有状态的智能体。 | https://langgraph.com.cn/index.html | ✅ | 官方站定位描述，完整回答“LangGraph 是什么” |
| Q02 | LangGraph 是一个低级别的编排框架，用于构建、管理和部署长时间运行、有状态的智能体…… | https://langgraph.com.cn/index.html | ❌ | 未涉及 interrupt 函数，答非所问 |
| Q03 | RAG（Retrieval Augmented Generation）即检索增强生成技术，是一种将信息检索与生成式 AI | https://developer.baidu.com/article/detail.html?id=8123272 | ✅ | 给出 RAG 定义，符合原理问题 |
| Q11 | LangGraph 是一个低级别的编排框架，用于构建、管理和部署长时间运行、有状态的智能体…… | https://langgraph.com.cn/index.html | ❌ | 只讲 LangGraph，未与 CrewAI 对比 |
| Q14 | LangChain：使用任意模型提供商快速构建智能体。 | https://langchain-doc.cn/ | ❌ | 只讲 LangChain，缺少 LlamaIndex 一侧，无法构成对比 |
| Q15 | 代码示意（伪代码）：# 单智能体决策逻辑 def single_agent_action ( env_data ): … | https://developer.baidu.com/article/detail.html?id=7330994 | ❌ | 代码片段不构成“实现复杂度差异”的结论 |
| Q21 | 对此，风投公司 Dragonfly 合伙人 Haseeb Qureshi 近期就评论表示，当前席卷业界的「AI Agen | https://www.jb51.net/blockchain/968771.html | ❌ | 讨论 AI Agents 热潮与质疑，未直接回答“企业落地挑战” |
| Q22 | 在越来越强大的小型模型的推动下，执行 GPT-3.5 水平任务的系统推理成本在 2022 年 11 月至 2024 年  | https://finance.sina.com.cn/roll/2025-04-08/doc-inesnnhq4714712.shtml | ✅ | 给出推理成本下降的直接原因与数据 |
| Q23 | 2025 中国 AI Agent 商业应用场景洞察研究 | https://www.doc88.com/p-91968460569774.html | ❌ | 只是文档标题，没有列出具体场景 |
| Q31 | LangGraph 是一个低级别的编排框架，用于构建、管理和部署长时间运行、有状态的智能体…… | https://langgraph.com.cn/index.html | ❌ | 只讲框架定位，未讲 checkpointer 与 interrupt 的配合 |
| Q32 | 超越 Prompt 与 RAG：为什么企业级 AI 客服必须走微调之路？ | http://developer.aliyun.com/article/1709498 | ✅ | 标题直接给出“仍需微调”的论点方向 |
| Q36 | Agent 的容错与重试机制：当工具调用失败时，Agent 如何自己想办法 | https://juejin.cn/post/7644870301448405027 | ✅ | 标题即回答重试/降级方向，与问题一致 |

## 9. 功能验收对照

| 验收项 | 实现/证据 | 结果 |
| --- | --- | --- |
| 问题拆解 3~5 个子问题 | `plan_research` 子问题字段，日志 S002 记录；mock 40/40 达标 | ✅ |
| 联网搜索拿到候选来源 | `search_web` 三提供方回退；真实评测每题均有搜索记录 | ✅ |
| 网页抓取正文/去广告/去重 | `fetch_pages` + `text_clean` 清洗；去重率见上表 | ✅ |
| 证据表（来源/摘要/可信度） | `state.evidence` + `final_output.evidence`，字段含 `source_url/source_snippet/confidence` | ✅ |
| 结构化 Markdown 报告 | `DRAFT_SYSTEM` 固定摘要/关键发现/局限与未证实三段式 | ✅ |
| 引用校验 | `filter_evidence` 逐字校验 + `_sanitize_report` URL 白名单 + `[S#]` 链接化 | ✅ |
| 人工确认可暂停/修改/继续 | 3 处 `interrupt()`；pytest 覆盖通过/修改/终止分支 | ✅ |
| 失败重试与降级 | 工具重试 3 次 + 指数退避 + 熔断；失败案例见第 7 节 | ✅ |
| 可观测（trace/日志/token/耗时） | `final_output` 含 `trace_id/log_summary/token_usage/cost_cny/duration_ms`；JSONL 日志 | ✅ |

## 10. 限制与后续

- 本次环境没有 `OPENAI_API_KEY`，LLM 走离线启发式：**token/成本真实数据为 0**，P95 延迟也不包含模型推理时间；接入真实模型后需要重跑本评测才能给出真实成本与延迟。
- 真实联网评测的检索质量受搜索引擎可用性影响（部分站点 403 / 区域不可达 / PDF 链接），失败案例见第 7 节。
- 事实准确率：宽松代理（预期要点 bigram 覆盖）与人工抽样（第 8 节）只是近似，不能替代独立标注；上线前建议扩展到 100+ 题并双人标注。
- mock fixture 只验证流水线机制，不代表真实检索效果；勿把 mock 的高分当作产品指标。

## 11. 复现命令

```bash
python evaluation/run_eval.py --mode mock --tag mock_current
python evaluation/run_eval.py --mode mock --tag q21_mock --ids Q21
python evaluation/run_eval.py --mode mock --tag mock_baseline --no-link-citations --no-sub-questions
python evaluation/run_eval.py --mode real --tag real_current
python evaluation/run_eval.py --mode real --tag real_after_360
python evaluation/make_report.py
```
