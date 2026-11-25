# 研究助手优化路线图（OPTIMIZATION.md）

> 基于当前仓库代码与最近一次评测（`eval_report.md`、`evaluation/results/`）的差距分析。
> 目标不是“再加功能”，而是把「有来源」升级为「答得对、答得全、可复核」。

## 0. 一句话结论

当前流水线已经能稳定跑通「规划 → 搜索 → 抓取 → 证据 → 报告 → 三处人工确认 → 写操作」，
**工程闭环是完整的，但研究质量还没有闭环**：

- 引用可追溯 100%，只证明“角标能点回某个网页”，**不证明该网页支持这句话**；人工抽样支持率只有 **41.7%**（5/12）。
- 对比类成功率 **70%**、多跳类 **60%**，明显低于简单事实/时效类的 100%。
- 「预期要点覆盖率」自动代理只有 **21.2%**。
- 40 题评测仍是**离线启发式 LLM**（成本/token = 0）；真实 LLM 只跑了 1 题 smoke（`llm_smoke`）。

根因不在提示词，而在结构：**单轮检索、最多 3 个页面、子问题不参与检索与验收、没有重排、没有 claim 级校验**。
所以优化的第一优先级是「检索—证据—断言」这条质量链，而不是 UI 或更多工具。

## 1. 现状诊断（带证据）

| 维度 | 现状 | 位置 / 数据 | 后果 |
| --- | --- | --- | --- |
| 检索轮次 | 只跑一轮，按规划好的 query 各搜一次 | `graph.py:142-232` | 发现“答非所问”后不会补搜 |
| 抓取规模 | `MAX_PAGES=3`、`MAX_QUERIES=3` | `graph.py:33-34` | 对比题常只抓到一方；多跳题证据不足 |
| 子问题 | 只解析并展示，**不驱动检索/验收** | `llm_client.py:342-344` | “拆解”是装饰性的，没有覆盖度闭环 |
| 证据抽取 | 每页最多 2 句、总共最多 8 条 | `llm_client.py:636-680` | 长文关键段落容易漏；证据面窄 |
| 上下文 | 每来源只喂前 1800 字符 | `prompts.py:66-74` | 正文后半段的关键结论永远看不到 |
| 证据校验 | 只校验 URL 在抓取集合、quote 逐字命中 | `llm_client.py:552-600` | 防编造，但**不校验 claim 是否被 quote 支持** |
| 报告合成 | 单次 LLM 草稿，无来源交叉验证 | `graph.py:492-526` | 结论可能由一条弱证据外推；无冲突标注 |
| 去重 | 只按 URL 去重 | `graph.py:215-222` | 同文转载、同义段落重复进入证据 |
| 时效 | 没有发布日期/时间过滤 | 全链路 | 时效题可能引用多年前内容 |
| 人工审核 | 通过 / 改意见 / 终止；后端支持 `edited_report`，前端没入口 | `index.html:209-215`、`graph.py:576` | 用户不能直接改稿 |
| 会话 | `InMemorySaver` + 进程内 `_SESSIONS` | `web.py:78,88` | 服务重启/刷新后中断会话全丢 |
| 交互 | NDJSON 步骤日志；无 token 流、无取消、无历史 | `web.py:122-162` | 等待期不可中断，体验像“卡住” |
| 真实模型评测 | 仅 1 题 smoke | `results/llm_smoke.summary.json` | 真实模型下的质量/成本/延迟没有数据 |
| 依赖/CI | `requirements.txt` 全是 `>=`；无 CI | `requirements.txt` | 环境漂移，回归靠手动 |

## 2. 能力缺口矩阵（“不完善”具体指什么）

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 单问题研究 + 引用 | ✅ 闭环 | 当前主路径 |
| 对比/多跳/时效的**深度** | ⚠️ 半成品 | 能跑完，但覆盖与正确性不足 |
| 真实 LLM 全量评测 | ❌ 缺失 | 只有 1 题 |
| 断点恢复/会话历史 | ❌ 缺失 | 重启即丢 |
| 流式输出/取消 | ❌ 缺失 | 只有步骤日志 |
| 报告编辑/导出 | ⚠️ 半成品 | 只能下载 Markdown；不能页面内改稿 |
| 真实邮件/Notion | ⚠️ 可选 | 默认 dry-run；无幂等/审计 |
| 缓存/并发 | ❌ 缺失 | 串行抓取，重复请求不缓存 |
| 多用户/权限/部署 | ❌ 缺失 | 单机单用户 |
| 可观测性 | ⚠️ 基础 | 本地 JSONL，无 trace 平台/指标面板 |

## 3. P0：研究质量闭环（最优先，约 1–2 周）

### 3.1 子问题驱动的两轮检索 + 覆盖矩阵

**改法**

1. `plan_research` 输出 `sub_questions`（已有），为每个子问题生成 1 条 query，而不是只生成 2–3 条泛化 query。
2. 新增 `gap_check` 节点（或 `retrieve_round_2`）：
   - 对每个子问题，用证据的 `quote/claim` 做关键词/向量覆盖率判断；
   - 未覆盖的子问题生成补搜 query（“实体 + 维度 + 年份/官方文档”），最多再搜 2–3 条、再抓 2–3 页；
   - 总轮次硬上限 2，预算仍用 `AGENT_SEARCH_NODE_BUDGET` / `AGENT_FETCH_NODE_BUDGET` 控制。
3. `state` 增加 `coverage: [{sub_question, evidence_ids, covered: bool, gap_query}]`，`final_output` 输出覆盖率。

**预期**：多跳 60% → ≥85%，对比 70% → ≥90%，证据覆盖 21% → ≥60%。

### 3.2 来源重排与选段（不是“多抓”，是“抓对”）

- **重排**：抓取前按「域名权威度 + query 词命中 + 标题/摘要相关度 + 是否为官方文档/GitHub/百科」打分；不用搜索原始顺序。
- **选段**：长文不要只截前 1800 字符；按子问题把正文切段，用 BM25/embedding 选出 top-k 段喂给证据抽取（每来源 2–3 段）。
- **内容去重**：URL 去重之外，加 SimHash/MinHash 近似去重，识别转载与同义段落。
- **时效**：抓取时解析 `datePublished`/正文日期；时效题优先近 12–24 个月来源，报告标注发布日期。
- **来源质量**：对 CSDN/博客园等 UGC 与官方文档给不同先验；低质来源不直接丢弃，但降低排序并在报告标注来源类型。

### 3.3 断言级溯源校验（把“可追溯”升级为“被支持”）

当前只校验 quote 逐字存在。加一道 **claim verifier**：

- 输入：`claim` + `quote` + 子问题；
- 输出：`supported / partial / unsupported` + 理由；
- `unsupported` 的 claim：从「关键发现」移除，或降级到「局限与未证实」并标注“证据不足”；
- 同一结论被多个来源支持时合并引用；来源互相矛盾时新增「来源分歧」小节，而不是二选一。

成本可控：只校验进入报告的 8–20 条 claim，每条约 200–400 token；也可用一次批量 JSON 调用完成。
离线模式保留规则版（关键词/句子相似度）作为降级。

### 3.4 结构化合成，而不是“一段摘要 + 列表”

- 报告从「摘要/关键发现/局限」升级为：
  - 摘要（直接回答 + 一句置信度说明）
  - 按子问题分节（每节：结论 + 证据 + 覆盖状态）
  - 对比题自动生成对比表（维度 × A/B），多维题生成要点表
  - 来源分歧 / 局限与未证实
  - 来源清单（保留现有程序化重建）
- 草稿只允许引用 `supported` 证据；`_sanitize_report` 增加“无证据句”检查（可选：给无引用句打标）。

### 3.5 评测升级（否则无法证明优化有效）

- 40 题 → **100 题**（每类 25），增加“对比双方都覆盖”“多跳链路”“时效年份”标注。
- 跑 **真实 LLM 全量 100 题**，同时保留离线基线。
- 增加 LLM-as-judge 三维：`answer_relevance`、`faithfulness`（claim 是否被引用支持）、`coverage`（子问题覆盖）；人工抽检 30 条校准 judge。
- CI/脚本设阈值：成功率、faithfulness、对比/多跳分类成功率、P95、单题成本；低于阈值直接失败。
- 评测结果区分「检索能力」与「模型能力」：固定检索集重放，避免网络波动掩盖改进。

**P0 验收目标（建议）**

| 指标 | 当前 | 目标 |
| --- | --- | --- |
| 人工/ judge 支持率 | 41.7% | ≥ 75% |
| 对比类成功率 | 70% | ≥ 90% |
| 多跳类成功率 | 60% | ≥ 85% |
| 子问题覆盖率 | 未统计 | ≥ 80% |
| 预期要点覆盖（judge） | 21.2%（代理） | ≥ 70% |
| 单题成本（真实 LLM） | 数据缺失 | ≤ ¥0.15 |
| P95 延迟 | 19.8s（离线） | ≤ 45s（真实 LLM） |

## 4. P1：可靠性与体验（约 1 周）

1. **持久化会话**：`InMemorySaver` → `SqliteSaver`；`thread_id` 落库，服务重启可恢复；新增 `GET /api/sessions`、会话列表/继续按钮。
2. **可中断**：前端加「取消」；后端用任务取消/超时（每节点预算已有，补总预算）；取消后落 `cancelled` 状态与日志。
3. **流式体验**：把节点级 NDJSON 升级为「节点日志 + LLM token 流」；显示当前子问题/已抓页数/预计剩余。
4. **抓取加固**：
   - PDF 正文解析（`pypdf`）；
   - JS 空页可选 Playwright（按需启用，不进默认依赖）；
   - 403/521 时尝试 `http://`、移动 UA、去掉 referer 的直连；
   - 可选接入官方搜索 API（Tavily/Bocha/Serper）作为第一提供方，HTML 抓取作 fallback；
   - 搜索/抓取结果本地缓存（SQLite，TTL 1–7 天），同一问题重跑不重复打网络。
5. **并发**：`search_web` 多 query、`fetch_page` 多 URL 用线程池/异步并发（当前串行），P95 可显著下降。
6. **成本与失败预算**：`AGENT_MAX_COST_CNY`、`AGENT_MAX_TOKENS`；超预算降级（减少证据/改用离线草稿）而不是无限重试。

## 5. P2：产品化（按需，约 1–2 周）

- **报告资产**：页面内编辑（后端 `edited_report` 已支持，补前端）、导出 Word/PDF、分享链接、历史报告库。
- **真实写操作**：SMTP/Notion 真正可用；加幂等键（同 thread 同动作只执行一次）、审批人/时间审计、失败重试与回执。
- **多用户**：登录、按用户隔离 thread/outbox、配额；部署用 Docker（固定依赖 lockfile）。
- **可观测**：OpenTelemetry / LangSmith trace、每节点 token/成本面板、错误分类看板；日志脱敏（URL query、邮箱、Key）。
- **检索质量运营**：域名黑白名单、站点级抓取策略、失败域名自动降级；把人工审核的“修改意见”回流成评测样本。

## 6. 不建议现在做（避免伪优化）

- **多智能体堆叠**：当前瓶颈是证据质量，不是 agent 数量；多一层 planner/critic 只会放大延迟与成本。
- **更多工具**：在 41.7% 支持率下加 arXiv/新闻 API，只会让报告“更长”，不会“更对”。
- **微调模型**：没有高质量标注集之前，prompt + 校验 + 重排的收益远大于微调。
- **花哨 UI**：先把「取消/恢复/流式/编辑」这四个基础交互补齐，再谈视觉。

## 7. 建议落地顺序（两周 sprint）

| 天 | 工作 | 产出 |
| --- | --- | --- |
| D1–D2 | 子问题→query 映射、coverage 状态、gap_check 节点 | 两轮检索可跑，覆盖率入 final_output |
| D3–D4 | 重排 + 选段 + 内容去重 + 日期解析 | 证据相关性提升，报告带日期 |
| D5–D6 | claim verifier + 冲突标注 + 报告结构升级 | faithfulness 指标可测 |
| D7 | 评测集扩到 100 题、接 judge | 新基线 |
| D8–D10 | 真实 LLM 全量跑 + 调阈值 + 修失败案例 | `eval_report.md` 更新 |
| D11–D12 | SqliteSaver、取消、流式 token、编辑入口 | 体验闭环 |
| D13–D14 | 缓存/并发/PDF/官方搜索 API + CI | 稳定性与成本达标 |

## 8. 怎么证明“优化有效”

每次改动必须同时给出：

1. 固定评测集上的前后对比（成功率、分类成功率、faithfulness、覆盖率、P95、成本）；
2. 至少 10 条人工抽检（支持/不支持/部分支持），并说明抽样方式；
3. 失败案例归因（检索没找到 / 抓取失败 / 证据不相关 / 模型外推 / 引用错配），不是只报总分；
4. 关键路径不回归（99 个现有测试 + 新增 gap/verifier 测试全绿）。

---

**建议先做 P0 的 3.1 + 3.3**：它们分别解决“证据不够”和“证据不被支持”这两个最大扣分项，
而且都能在现有图结构上增量实现，不需要重写架构。

## 9. 实施记录（P0 第一批，2026-11-07）

已落地（现有 10 节点图 → 11 节点，全部增量改造）：

| 能力 | 实现 | 状态 |
| --- | --- | --- |
| 子问题覆盖度 | `quality.compute_coverage()`；英文实体 + 中文语义块，按子问题计算 score/covered/证据编号 | ✅ |
| 缺口补检 | 新增 `check_coverage` 节点；覆盖率 < 75% 时生成 gap query，最多补 1 轮（默认总轮次 2） | ✅ |
| 抓取自适应 | 对比/多跳题自动扩页（基础 3 → 最多 6）；补检轮 +1；候选按问题词 + 官方站点先验重排 | ✅ |
| 内容去重 | 正文指纹（前 600 字符哈希），拦截转载/镜像；URL 去重保留 | ✅ |
| claim 支持校验 | `quality.verify_evidence()`：逐字/包含 → 1.0；bigram 重叠 + 数字/英文实体/否定一致性 → supported/partial/unsupported；只丢弃明显无关项 | ✅（规则版） |
| 可观测 | `final_output` 新增 `coverage / coverage_summary / verification / retrieval_rounds`；日志新增 `check_coverage`；报告与 Web 草案页展示覆盖率/支持率 | ✅ |
| 评测指标 | `run_eval` 新增 `coverage_rate / evidence_supported_rate / avg_retrieval_rounds`；报告新增 5.3 节 | ✅ |
| 测试 | 新增 `tests/test_research_quality.py`（7 个用例）；全量 **106 passed** | ✅ |

### 9.1 真实联网前后对比（离线 LLM + 真实检索）

| 指标 | P0 前（real_after_360） | P0 后（real_p0） | 变化 |
| --- | --- | --- | --- |
| 任务成功率 | 82.5% | **85.0%** | +2.5 pp |
| 多跳类成功率 | 60.0% | **70.0%** | +10.0 pp |
| 对比类成功率 | 70.0% | 70.0% | 0 |
| 事实覆盖（宽松代理） | 21.2% | **26.3%** | +5.0 pp |
| 引用可追溯 / 可点击 | 100% / 100% | 100% / 100% | 持平 |
| 子问题证据覆盖率 | 未统计 | **51.2%** | 新增（未达标） |
| 证据断言支持率 | 未统计 | **99.4%** | 新增（规则代理） |
| 平均检索轮次 | 1.0 | **1.68** | +0.68 |
| 平均延迟 / P95 | 9.8s / 19.8s | 15.6s / 26.7s | +5.8s / +6.9s |
| 测试数 | 99 | **106** | +7 |

结论：**成功率与多跳提升成立，但子问题覆盖率 51.2% 未达 75% 目标**，说明“补检”只能缓解、不能解决
“对比题只抓到一方 / 站点 403/JS 空页”这两类根因。断言支持率 99.4% 是规则代理，不是语义蕴含，
不能直接当作人工审计结果。

### 9.2 P0 下一批（按收益排序）

1. **对比题按实体拆分检索**：识别 A/B 两个实体，分别检索并各自要求覆盖，避免“只抓到 A 就补搜泛化 query”。
2. **选段而不是截断**：按子问题对长文切段打分，喂 top-k 段给证据抽取（现在只喂每来源 1800 字符）。
3. **LLM verifier（可选开关）**：对进入报告的 8~20 条 claim 做一次批量语义支持校验；规则版保留为降级。
4. **结构化报告**：按子问题分节 + 对比表；`局限与未证实` 自动列出未覆盖子问题。
5. **评测扩到 100 题 + LLM-as-judge**（answer relevance / faithfulness / coverage），并重新人工抽检 30 条。

### 9.3 已知风险

- 补检轮让平均延迟从 9.8s 升到 15.6s、P95 从 19.8s 升到 26.7s；接近 30s 目标线，需要并发/缓存/轮次预算控制。
- 规则 verifier 会误杀高度抽象的合法改写（score < 0.25 才丢弃，风险已压低），但不能替代语义判断。
- `real_p0` 仍是离线 LLM；接入真实模型后 token/成本/延迟要重跑，support rate 也可能变化。

## 10. 实施记录（P0-1：会话持久化与多轮，2026-11-09）

目标：解决「服务重启后审批中断全丢、没有历史会话、无法追问」三个产品化缺口，
并在不改图结构的前提下完成（`build_graph(checkpointer=...)` 接口早已预留）。

| 能力 | 实现 | 状态 |
| --- | --- | --- |
| 断点恢复 | `persistence.build_checkpointer()` → `langgraph-checkpoint-sqlite` 的 `SqliteSaver`，`data/checkpoints.sqlite`（WAL + `check_same_thread=False`） | ✅ |
| 会话索引 | `SessionStore`（`data/sessions.sqlite`）：thread_id / trace_id / question / status / parent_thread_id / 时间 | ✅ |
| 历史 API | `GET /api/sessions?limit=20`、`GET /api/sessions/{thread_id}`（返回与 `/api/research` 同构结构，含中断载荷） | ✅ |
| 重启后流式恢复 | `_trace_for()` 优先内存缓存、缺失时从索引回源；`/api/resume/stream` 不再因重启 400 | ✅ |
| 多轮追问 | `POST /api/research {"parent_thread_id": ...}`：父会话结论摘要（问题/子问题/报告 ≤1200 字符）→ `history_context` → 规划提示词 | ✅ |
| 前端 | 右上角「历史会话」抽屉（状态徽标 + 一键继续审批）；`?history=1`、`?session=<thread_id>` 深链 | ✅ |
| 测试 | 新增 `tests/test_persistence.py`（5 例）；全量 **118 passed** | ✅ |

### 10.1 跨进程验收（实际执行）

1. 独立脚本用假网页跑出 `draft_review` 中断，写入 SQLite（模拟"另一台进程/上一次运行"）；
2. 启动全新的 `web.py`（内存里没有任何会话缓存）；
3. `GET /api/sessions` 列出该会话、`GET /api/sessions/{id}` 能还原 15 条日志与草稿；
4. `POST /api/resume` 直接返回 `email_confirmation` —— **证明重启后可继续审批**。

截图：`../原型图/web-ui-history.png`（历史抽屉）、`../原型图/web-ui-recover.png`（恢复后的草稿审核页）。

### 10.2 设计取舍

- **不依赖 checkpointer 的 `list()` 列举会话**：不同版本 API 形态不稳定，且要从 checkpoint BLOB 里反解问题/状态；独立索引表更稳、更快。
- **索引写失败不阻断研究**：`_session_*` 全部 try/except，索引挂了只影响"历史列表"。
- **多轮只注入摘要，不做长期记忆**：1200 字符上限，避免 token 膨胀与"把上一轮错误带进下一轮"。
- **保持同步调用链**：没有顺手 async 化，避免与现有 11 节点、工具重试逻辑连锁改动；并发/异步属于 P2。
- **SQLite 而非 Postgres**：本机单进程演示够用，`build_checkpointer()` 已是单点替换位。

### 10.3 已知风险

- SQLite 单写者：不要起多个 uvicorn worker；多实例部署要换 `PostgresSaver`。
- 索引表与 checkpoint 可能短暂不一致（例如索引写失败）：表现为历史列表缺一条，不影响恢复。
- 仍无鉴权/限流，`thread_id` 即能力凭证，不要暴露公网。

## 11. 实施记录（P0-2：调用链 Trace + 成本归因，2026-11-09）

| 能力 | 实现 | 状态 |
| --- | --- | --- |
| 节点 Span | `graph._instrument` 统一包住 12 个节点，追加 `logs/<trace_id>.trace.jsonl`（`span`/`summary` 两类记录） | ✅ |
| 节点级 token | 包装器在节点前后取 `usage_snapshot()` 差值，写入 state `node_usage` | ✅ |
| 价目表 | `config/pricing.json` + `tracing.estimate_cost`：模型最长前缀匹配；未命中回退 env 并标记 `estimated` | ✅ |
| 成本归因 | `finalize` 输出 `token_usage_by_node / cost_by_node / cost_estimated / cost_price_source / model` | ✅ |
| 查询 API | `GET /api/trace/{trace_id}`、`GET /api/cost/summary?days=7`（按模型/按天聚合） | ✅ |
| 前端 | 结果页「调用链 Trace」卡片：耗时条、每节点 token/成本、估算标记 | ✅ |
| 测试 | 新增 `tests/test_tracing.py`（6 例）；全量 **124 passed** | ✅ |

验收：用真实 LLM 跑一次完整流程（glm-4-flash）→ 12 个 span、token 6041、`retrieve_knowledge` 节点
单独 1442 token；`token_usage_by_node` 之和等于运行总量。截图 `../原型图/web-ui-trace.png`。

设计取舍：

- **自研 trace 而不是接 Langfuse**：数据就在本机 JSONL，`/api/trace` 已够用；接 SaaS 是可选项，不是依赖。
- **未知模型必须标记估算**：价目表不可能覆盖所有网关模型，宁可显式 `estimated=true` 也不假装精确。
- **interrupt 节点不记 span**：`GraphBubbleUp` 直接放行；恢复时节点会重跑，避免两条互相矛盾的记录。

## 12. 实施记录（P0-3：本地知识库混合检索，2026-11-09）

| 能力 | 实现 | 状态 |
| --- | --- | --- |
| 分块 | 按 Markdown 标题切段 → 800 字滑窗 / 120 重叠，保留 heading 路径与 `kb://doc#chunk` URL | ✅ |
| BM25 | 中文字符 bigram + 英文词，k1=1.5 / b=0.75，不引入分词依赖 | ✅ |
| 向量 | OpenAI 兼容 `/embeddings`（可配 `AGENT_EMBEDDING_MODEL`），纯 Python 余弦 | ✅ |
| 融合 | RRF（Σ 1/(60+rank)），BM25 与向量排名合并 | ✅ |
| 降级 | 无 Key / 调用失败 → 纯 BM25；知识库为空 → 节点 `skipped` | ✅ |
| 图接入 | 新节点 `retrieve_knowledge`（fetch_pages → retrieve → summarize），命中片段转 `kb://` 来源 | ✅ |
| 证据链路 | KB 片段整段作为证据（避免短句抽取丢证据），与网页证据一起走 verify / 引用 / URL 白名单 | ✅ |
| 引用展示 | 来源清单显示 `📚 知识库 · 文档 · 小节`，`kb://` 不做成可点击链接 | ✅ |
| 前端 | 证据表知识库来源显示 `📚 知识库`；结果页 trace 新增 `retrieve_knowledge` 节点 | ✅ |
| 验收 | `evaluation/kb_check.py`：只给 `knowledge/`、禁止联网，6 题全部命中并引用 | ✅ **6/6** |
| 测试 | 新增 `tests/test_rag.py`（7 例）；全量 **131 passed** | ✅ |

验收数据：`knowledge/` 3 篇文档 / 14 个 chunk；Zhipu `embedding-3` 真实向量（2048 维）索引成功，
无 embedding 时自动降级 BM25。真实 LLM + 真实联网一次运行：来源 8（网页 2 + 知识库 6）、
证据 10、token 6041。截图 `../原型图/web-ui-rag.png`。

设计取舍：

- **统一 `[S#]` 编号，而不是双通道 [S#]/[K#]**：复用验证过的证据抽取与引用校验链路，改动面小；
  来源清单用 📚 标注类型，信息不丢。双编号会与现有 URL 白名单/编号重建冲突，收益不抵风险。
- **KB 证据整段引用**：知识库片段常见表格行/列表项，句长 < 25 会被网页抽取器丢弃；
  检索已按相关性排序，整段引用 + 检索分给置信度更合理。
- **不引入 Chroma/FAISS**：14 个 chunk 的规模用纯 Python 余弦足够，少一个重依赖；
  超过 1 万 chunk 再换。
- **索引缓存**：`data/kb_index.json` 记录 mtime/size 与模型名，文档变更自动重建。

## 13. 实施记录（报告质量闭环：证据卡片 → 大纲 → 分节写作 → 引用校验，2026-11-09）

针对实测的四个问题：**摘要太水 / 逻辑割裂 / 引用堆叠错误 / 缺乏推演**，把报告生成从“一次调用”
拆成四层（新增 `reporting.py`，图从 12 节点变 14 节点）。

| 能力 | 实现 | 对应的病 |
| --- | --- | --- |
| 证据卡片化 | `summarize_sources` → `reporting.enrich_evidence`：`sub_question` / `kind`（定义/机制/数据/案例/观点）/ `promo`（营销标记）/ 同义合并（`merged_count`、`merged_urls`） | 引用堆叠：同页同义证据不再反复出现 |
| 大纲规划 | 新增 `plan_outline`：LLM 输出 `{sections:[{title,goal,evidence_ids}]}`；失败/离线回落规则大纲；`uncovered` 只保留真没证据的子问题 | 逻辑割裂：一节只写一个子问题 |
| 分节写作 | `draft_report` 按节调用（每节只喂本节证据，编号仍全局 `[S#]`）；摘要独立提示词「结论→依据→边界」；「关键发现」程序生成；「分析与推演」每条【推断】 | 摘要太水 / 缺乏推演 |
| 引用硬校验 | 新增 `draft_review`：编号越界、单句 >2 角标、引用与句子无词面关联、关键发现无引用、摘要空判断、小节缺失；不达标让模型最小改动重写一次（**问题更多就丢弃**），再程序剪枝/补节 | 引用堆叠错误 + 防止改坏 |

结果写入 `final_output.outline` 与 `final_output.draft_review`（`status / issues / rewrites /
rejected_rewrites / checks.remaining_issues`）；消融开关 `AGENT_EVIDENCE_CARDS`、`AGENT_DRAFT_REVIEW`。

**实施过程中被真实运行打出来的 5 个坑（都已修 + 有测试）**：

1. 首版让模型“修引用”，它直接把角标删光、把小节重排 → 校验增加 `no_citations` / `findings_not_bulleted`，
   并且**重写后问题数不下降就丢弃**，保留原稿走程序修复；
2. 程序修复按“行”重排角标，把一句话的引用挪到行尾 → 改成**逐句**修复，角标放回句末标点之前；
3. 规则大纲只有“摘要/内容节/推演/局限”，没有「关键发现」→ `find_bullets()` 按证据程序生成，
   条条带引用、不会被模型写成大段散文；
4. 知识库把本项目自己的 RAG 设计文档塞进“LangGraph vs AutoGen”报告 → `retrieve_knowledge`
   增加问题词面相关性门槛，日志记录过滤条数；
5. 覆盖率是词面代理，会出现“写了整节 yet 局限里说未覆盖” → `uncovered` 先按证据归属与大纲小节过滤；
6. 大纲 JSON 偶发非法 → `chat_json` 用 `response_format=json_object`（网关不支持自动重试），
   仅对 JSON 解析错误重试，超时不重试。

**实测（glm-4-flash + 真实联网，对比题）**：大纲 6 节（LLM）、草稿 4.6k 字符、`draft_review`
剩余问题 0（状态 passed）；摘要从“存在差异”式空判断变成“LangGraph 用 interrupt 在节点内部暂停，
AutoGen 靠检查点回放状态”；「分析与推演」每条带【推断】与前提/推翻条件；局限不再与正文自相矛盾。
代价：单题耗时从 ~140s 升到 ~260s（分节写作 5~6 次调用 + 一次重写预算）。
后续已通过“分节并发 + 长超时分档”把写作阶段压回来，见 §14。

**遗留**：证据仍是摘录式时，关键发现会出现英文原句（摘要/推演已是中文）；引用校验只能查格式与词面
相关，查不出“结论对但因果写错”；重写失败时保留已知问题（写入 `draft_review`，不阻塞流程）。

## 14. 实施记录（运行时性能修复：超时放大 / 串行写作 / 耗时归因，2026-11-23）

一次真实运行（`trace_id=7914d6b6`，glm-4-flash，2 轮检索）总耗时 **419.3s**。拆账后：

| 节点/阶段 | 耗时 | 占比 | 是否有效产出 |
| --- | --- | --- | --- |
| `summarize_sources` 第 1 轮 | 121.3s | 29% | 否：`Request timed out` → `offline_fallback` |
| `summarize_sources` 第 2 轮 | 121.6s | 29% | 否：同上 |
| `draft_report`（6 次串行） | 80.3s | 19% | 是 |
| `plan_outline` | 22.1s | 5% | 是 |
| 搜索/抓取/规划等 | ~23s | 5% | 是 |
| 人工确认等待（两次 interrupt） | 50.5s | 12% | 是（用户操作） |

### 根因

1. **一次 30s 超时被放大成 121s**：`_sdk` 用 `max_retries=1`（×2），`_chat` 又在任意异常时
   “去掉 `response_format` 再试一次”（×2），合计 4×30s≈120s，实测 121.3s；
2. **30s 对长输出本来就不够**：用同一 Key 实测 `summarize_evidence`，输出 750 token 需 **27~33s**
   （glm-4-flash 约 23 token/s），正好压在超时线上，必然间歇失败；
3. **工具预算形同虚设**：`call_tool` 的 deadline 只在两次重试之间检查，单次阻塞调用可任意超过 25s；
4. **分节写作串行**：6 次调用逐个跑；
5. **419s 混入 50.5s 人工等待**：`duration_ms` 取墙钟差，展示时无法区分“系统慢”和“人在看”。

### 修复

| 修复 | 位置 | 验证 |
| --- | --- | --- |
| 超时只发一次请求，不叠加格式回退；仅 400/422 回退纯文本；429/5xx/连接错误快速重试一次；SDK `max_retries=0` | `llm_client._chat` / `_sdk` | `test_llm_timeout_policy.py`（超时 1 次调用、400 回退 2 次、503 重试 1 次、SDK 参数） |
| 长输出调用分档 `AGENT_LLM_TIMEOUT_LONG=75`：证据提取、大纲、分节、重写 | `LLMClient.long_timeout`；`AGENT_SUMMARIZE_TIMEOUT` 与 summon 超时同源 | 真实调用实测：**121.3s + 降级 → 33.2s 成功（mode=openai，6 条证据）** |
| 分节写作并发（默认 3，`AGENT_DRAFT_CONCURRENCY`，1~4），按大纲顺序拼装，`previous_titles` 预计算保持提示词不变，token 累计加锁 | `LLMClient._draft_by_sections` / `_run_section_job` | `test_draft_concurrency.py`（并发度、顺序与串行逐字节一致、usage 计数） |
| 总耗时拆分：`compute_ms` = span 耗时之和，`hitl_wait_ms` = 差值；前端「总耗时 / 其中人工确认等待」、Word 导出同口径 | `graph._timing_split` / `finalize` / `index.html` / `web.py` | `test_timing_split.py`（含 trace 缺失不编造） |
| trace `started_at` 改为开始时间（原为完成时刻，导致时间线重叠/负间隔） | `tracing.TraceRecorder.add_span` | `test_trace_timestamp.py` |
| 写操作确认点校验收件人格式（原在执行时才报错，运行以 `completed_with_action_failure` 收尾） | `graph.human_confirm_actions` | `test_email_precheck.py` |

### 结果

- 全量测试 **226 → 247 passed**（新增 5 个测试文件 / 21 例）；
- 证据提取两轮：**242.9s（降级） → 66s（成功拿到 LLM 证据）**；
- 分节写作：**80.3s → 约 20~30s**（6 次调用并发，默认并发 3）；
- 总耗时展示：计算 368.8s + 人工 50.5s 分开呈现，不再把人工审稿算成系统耗时；
- 端到端实测（真实 glm-4-flash + 本地固定 3 页面，排除抓取波动；`trace_id=3f40a11e`）：
  **48.4s**（计算 48.4s / 人工 0.05s），证据提取 **13.0s 且成功**，大纲 14.3s，
  6 次分节写作 **15.5s**，3 来源 / 3 证据 / 覆盖 5/5；
- 剩余瓶颈：模型吐字速度（glm-4-flash ~23 token/s）与 2 轮检索结构；换更快模型/减少轮次是下一步，
  但会牺牲质量或覆盖度，暂不做。

**遗留风险**：超时预算调大后，网络极差时单节点等待上限从 30s 提高到 75s；可用
`AGENT_LLM_TIMEOUT_LONG` 调小，代价是长输出调用更容易降级。

## 15. 实施记录（抓取失败降级：垃圾结果过滤 + 搜索摘要级证据，2026-11-23）

用户反馈一次运行「完成（无可靠来源）、内容太少」（`trace_id=8742bda5`）。
拆日志后根因不是模型，而是**检索源全灭**：

| 现象 | 证据 |
| --- | --- |
| CSDN 结果全部打不开 | 4 个 URL 均 HTTP 521（源站/中间层错误，重试无效） |
| 知乎结果被反爬 | 2 个 URL HTTP 403 |
| 视频/搜索聚合页混进结果 | `tv.360kan.com/s?q=…`（200 但无正文）、`ai.so.com/search/…`（搜索页） |
| 其他搜索通道不可用 | DuckDuckGo / Wikipedia 连接超时；Bing 对“2027”返回节假日/百科噪声 |
| 结果 | 8 个抓取尝试全失败 → 0 来源 → 报告只剩「未找到可靠来源」+ 空推断 |

### 修复

1. **结果层过滤** `_is_junk_result_url()`：剔除 `*.360kan.com`、`*.so.com/search`、
   Bing/Baidu/Google 搜索页与非法 URL；检索日志记录「过滤 N 条视频/搜索聚合页」，
   把有限的 8 次抓取预算留给可能成功的页面（抓取候选再做一次防御）。
2. **零页兜底**：正文全失败时用搜索摘要拼「摘要级页面」（`snippet_only=True`，
   最多 6 条、同域最多 2 条、摘要 ≥40 字），让 `summarize_sources` 仍能产出证据；
   彻底没有任何摘要时保持原来的“未找到可靠来源”行为，不编造。
3. **不足补齐**：正文页数少于目标页数时，用**尚未抓取 URL 的摘要**补到目标页数
   （例如 1 个正文页 + 3 条摘要），避免“只抓到 1 页 → 报告太薄”。
4. **降级可解释**：证据逐条带 `snippet_only`；`evidence_note` 与报告「局限」注明
   “仅来自搜索摘要、需打开原文复核”；UI 报告页显示警告 chip；`final_output` 增加
   `snippet_only` / `snippet_evidence_count`。

### 验证

- 单元：`test_snippet_fallback.py` 5 例（过滤、零页兜底、无摘要保持诚实、补齐、证据/报告标注）；
- 真实运行（`trace_id=5dc82e2b`，真实联网 + 真实 LLM）：1 个正文页（638 字）
  + 补入 3 条搜索摘要 → 4 来源 / 5 证据 / 报告 **4740 字符**（修复前同条件 710 字符、
  0 来源 0 证据），状态 `completed`；`evidence_note` 明确写「其中 3 个来源未抓取到正文」；
- 真实运行（本地假网页 + 真实 LLM）验证零页路径：报告仍生成四节内容，并在「局限」
  与 UI 上标注摘要级证据。

**遗留**：摘要只有 40~120 字，信息密度低于正文；`check_coverage` 仍按词面判断，
会出现“有证据但覆盖 0/5”的保守结果（宁可少报覆盖）。提升方向是更可靠的抓取通道
（官方搜索 API / 可用的 reader 代理）以及把摘要与子问题做语义匹配，而不是放宽规则。

## 16. 实施记录（Prompt 护栏：知识库防污染 / 对比矩阵 / 篇幅与重复，2026-11-25）

三条用户反馈对应三类可复现问题：

1. **知识库污染**：问“Chroma vs Milvus”时本地 `knowledge/` 的项目文档（RAG 设计、评测指标）
   被召回，模型把“本项目怎么设计”写成“Chroma/Milvus 的能力”；
2. **对比报告质量差**：没有表格、A/B 属性张冠李戴、官方与第三方冲突不解释、大量空评价；
3. **又长又重复**：同一事实在摘要/发现/推演里反复出现，报告 5000+ 字符。

### 修复

| 修复 | 实现 | 验证 |
| --- | --- | --- |
| 外部产品/对比问题跳过本地知识库 | `retrieve_knowledge`：`prompts.is_comparison_question(question)` 且问题没有“本项目/我们的方案”等上下文 → 直接 `skipped`（日志写明原因）；保留时页面/证据打 `source_scope=project` | `test_prompt_guards.py`（外部对比跳过、项目上下文保留并标记） |
| Prompt 禁止项目文档当外部结论 | 六个系统提示词追加 `KB_SCOPE_RULE`；证据块来源后标注「（本地项目文档，仅代表本项目做法）」 | 同上（prompt 包含规则、证据块标注） |
| 对比报告专属指令 | `prompts.comparison_instructions()` 注入 `outline_user` / `draft_user` / `draft_section_user`：强制表格（≥3 维度）、禁止身份混淆、冲突消解、拒绝废话、缺证据标注 | 非对比问题不注入（单测）；真实运行生成对比表格 |
| 对比报告缺表格判不合格 | `reporting.has_markdown_table` + `validate_draft(comparison=True)` → `missing_comparison_table`；`REWRITE_SYSTEM` 要求补表格 | 单测（无表格报错、有表格通过） |
| 篇幅与重复 | Prompt：正文 ≤1000 字、同一事实最多两次；`validate_draft(question=...)` 记 `report_too_long` / `repeated_fact`；**确定性压缩** `compact_report()`：分节按句数/条数裁剪、重复表格只留一个、重复事实句最多两次、超限删非必需小节（保留摘要/关键发现/分析/局限与对比表格）；字数统计剔除引用 URL | 单测（压缩后 ≤ 上限、必需小节与唯一表格保留、重复小节被删） |

### 实测

真实运行 `Chroma vs Milvus`（`trace_id=58156c70`，glm-4-flash + 真实联网）：
- 两轮 `retrieve_knowledge` 都因“对比/外部产品问题”跳过知识库；
- 大纲 8 节（含「对比矩阵」），草稿 4627 字符 → `draft_review` 5 个问题、重写 1 次、修复 8 处；
- `compact_report` 删除 3 个重复表格小节、压缩发现/推演，最终正文 **806 字**（不含链接 URL），
  保留 1 个对比表格，`status=passed`；
- 对照修复前同题运行（`trace_id=7d70db4a`）：5342 字符、4 个重复表格。

**遗留**：表格内容正确性仍取决于证据质量（本次抓到的页面并非 Chroma/Milvus 官方文档，
模型会照着弱证据填表，需要更强的来源选择）；冲突消解目前靠 Prompt，没有程序化“矛盾检测”
（要检测“同一指标两个值”需要先做指标实体抽取，属于下一步）。

## 17. 实施记录（How 类检索 P0：代码块保留 / 首页降权 / 官方站查询，2026-11-25）

用户提出 4 条改进：搜索词强制加 site:/代码示例后缀、抓取拒绝只抓首页、提取宁缺毋滥、
How 专用报告模板。经讨论确认先做 P0 前置（代码块保留 + 首页识别 + 官方域名加权 + How 查询后处理），
理由：**不保留代码块，How 模板只会逼模型编代码**；而当前搜索不是 Tavily，`site:` 在 so360 上返回 0、
Bing 会忽略并返回首页。

### 实现

| 改动 | 位置 |
| --- | --- |
| `<pre>` 代码块转 ``` 围栏保留；行内 `<code>` 不单独成块；fetch 缓存 namespace 升级 `fetch:v2`（避免命中旧的无代码缓存） | `tools._extract_article` / `FETCH_CACHE_KIND` |
| 站点首页识别降权（`/`、`/index.html` 等 -0.25），文档内页路径加权（`/docs`、`/api`、`/reference`、`/guide`、`/tutorial` +0.12） | `graph._is_index_url` / `_rank_bonus` |
| 官方文档域名表扩充（python.org / fastapi.tiangolo.com / react.dev / nodejs.org / go.dev / pypi.org …），How 问题官方站再 +0.2 | `graph.OFFICIAL_HOST_HINTS` |
| How 问题查询兜底：`宽泛 + 代码示例 + site:` 三条；`site:` 0 结果自动回退无 site:；修复“教程 site:xxx”重复识别 | `graph._ensure_how_queries` / `search_web_node` |
| 规划 Prompt 增加 How 查询要求（程序仍会兜底） | `prompts.PLAN_SYSTEM` / `PLAN_JSON_SYSTEM` |

### 验证

- 抽取实测：runoob LangGraph 快速开始页，旧版 3652 字且 `pip/import/def/``` ` 均无；新版 **12000 字**，
  `pip install` / `import` / `def` / 代码围栏全部存在；
- 真实运行（`trace_id=915c6b3a`，问题「LangGraph 如何实现人工中断？」）：3 条唯一查询、
  抓到 3501 / 1830 / 12000 字三个页面，报告 966 字、≤1000，`draft_review` 通过；
- 但报告没有代码块：本次证据提取走 `heuristic_fallback`（只抽句子），
  说明「核心代码示例」必须等 P1（代码感知提取 + How 模板），P0 解决的是“代码不再在抓取阶段被丢掉”。

### 下一步（P1/P2）

- P1：证据提取增加 `code_snippet / api_names / packages / config` 字段；How 专用大纲与模板
  （实现原理 / 核心代码示例 / 依赖安装与配置 / 常见坑与验证）；无代码证据时明确写“未抓到可核验代码”，不编造；
- P1：首页/文档目录页 → 同域内页跟进 1 跳（最多 2 页），进一步落实“拒绝只抓首页”；
- P2：可选 Tavily/Serper（include_domains 才能真锁官方站）；How 评测集（代码块/API/包名覆盖率）。
