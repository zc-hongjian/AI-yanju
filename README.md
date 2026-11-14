# LangGraph 研究助手（Research Assistant MVP）

> 一个只做**研究助手窄场景**的 Agent：研究问题 → 拆解检索 → 联网搜索/抓取 → 提取证据 →
> 生成带引用的报告 → **三处人工确认** → 发邮件 / 存 Notion → 输出最终结果。
>
> 14 节点 LangGraph 图 · 3 次 human-in-the-loop · 引用可回溯 · 断点恢复 · 可观测 Trace/成本 · 本地知识库 RAG

**当前状态**：166 个测试全绿 · 9 项功能验收 9/9 · 知识库验收 6/6 · 真实联网 40 题评测已完成

---

## 目录

- [这是什么](#这是什么)
- [3 分钟跑起来](#3-分钟跑起来)
- [界面与用法](#界面与用法)
- [核心能力](#核心能力)
- [架构与流程](#架构与流程)
- [评测数据](#评测数据)
- [环境变量](#环境变量)
- [接口](#接口)
- [测试与验收](#测试与验收)
- [目录与文档](#目录与文档)
- [已知限制](#已知限制)
- [常见问题](#常见问题)

## 这是什么

一个**可运行的 MVP，不是通用 Agent 框架**：工具固定 5 个，写操作必须人工确认，
没有来源时宁可写「未找到可靠来源」也不编造。同一个图有两个入口：

- 命令行：[`main.py`](main.py)
- 网页版：[`web.py`](web.py)（FastAPI）+ [`index.html`](index.html)（单页前端）

两者共用同一张图、同一套工具与写操作门禁。

**五个亮点**：

1. **Human-in-the-loop**：草稿、发邮件前、存 Notion 前共 3 次 `interrupt`，`Command(resume=...)` 恢复；
2. **引用闭环**：`[S#]` 角标 → 逐字校验证据 → 报告 URL 白名单清洗 → 无来源兜底，四重防编造；
3. **研究 + 报告双层质量闭环**：子问题覆盖率/缺口补检 + 证据卡片化/大纲/引用硬校验；
4. **可恢复、可观测**：SQLite checkpointer 断点恢复、节点级 Trace 与 token/成本归因；
5. **知识库 RAG**：BM25 + 可选向量 + RRF，命中片段以 `kb://` 来源进入同一套引用链路。

技术栈：Python 3.14 · LangGraph（`StateGraph` / `interrupt` / `SqliteSaver`）· Function Calling
（OpenAI / 兼容网关）· FastAPI · 原生 HTML/CSS/JS（无构建）· SQLite。

![一键启用启动页](原型图/web-ui-launch.png)

## 3 分钟跑起来

### 方式 A：网页版（推荐）

```bash
# 1) 装依赖（Python 3.11+）
python -m venv .venv
.venv\Scripts\activate            # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2) 一键启动（Windows：起后端 + 自动打开启动页）
start.bat
# 或者手动：
python web.py                     # 打开 http://127.0.0.1:8000
```

浏览器会打开**一键启用启动页**：点「🚀 一键启用」→ 进入主界面并**自动弹出模型设置**，
填 Key（或点「跳过设置，直接进入」用离线模式）→ 输入问题 → 开始研究。

### 方式 B：命令行

```bash
python main.py "LangGraph 如何实现人工中断？"          # 交互式三次人工确认
python main.py "LangGraph 如何实现人工中断？" --auto   # 演示/CI：自动批准
```

### 没有 LLM Key 也能跑

不配置 Key 时自动进入 `offline` 模式：启发式规划 + 逐句摘录证据 + 模板报告，
仍会**真实联网**搜索/抓取，引用同样可回溯。适合先看流程。

### 接入真实模型

复制模板并填 Key（`.env` 已 gitignore；也可以完全跳过 `.env`，在网页右上角「模型设置」里填）：

```bash
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

| 服务 | `OPENAI_BASE_URL` | `OPENAI_MODEL` 示例 |
| --- | --- | --- |
| OpenAI 官方 | 留空（默认 `https://api.openai.com/v1`） | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |

```bash
python check_llm.py               # 真实调用一次，确认 Key/Base URL/模型可用
```

> 改 `.env` 后必须重启 `web.py`；网页里填的 Key 只存后端进程内存，重启失效、不回传明文。

## 界面与用法

![网页版主界面](原型图/web-ui.png)

网页端一次研究的流程：

1. 输入问题 → **开始研究**（实时日志 + 14 段进度条）；
2. **草稿确认**：通过并继续 / 填修改意见重新生成 / 终止；
3. **邮件确认**：就地改收件人、主题 → 发送 或 跳过；
4. **Notion 确认**：就地改标题、标签 → 保存 或 跳过；
5. **最终结果**：报告（可下载 Markdown）、来源、证据表、写操作结果、错误与降级、完整日志。

**执行进度**：14 段节点格子逐个点亮 + 阶段表情/文案 + 百分比；「收起日志 / 展开日志」只折叠日志表，
进度条始终可见。补检回环只增不减，不会倒退。

![执行进度](原型图/web-ui-progress.png)

**历史会话**：列表可「继续审批 / 查看 / 删除」；删除会同时清理该会话的 checkpoint。
执行中后端退出/断网时，进度**冻结标红**（`已中断 · 第 N/14 步`），并出现「会话未完成」卡片
（重新开始 / 删除），不会伪装成“完成”。

![历史会话：中断状态与删除](原型图/web-ui-history-delete.png)

![后端被杀后的中断态](原型图/web-ui-interrupted.png)

**模型设置**：提供方预设、Key 掩码显示、测试连接、保存到进程内存、清除运行时配置。

![模型设置面板](原型图/web-ui-settings.png)

**深链参数**：`?settings=1` 打开设置、`?history=1` 打开历史、`?session=<thread_id>` 加载会话、
`?q=...&autostart=1` 自动开始；直接双击 `index.html`（`file://`）进入静态演示模式。

## 核心能力

| 能力 | 做了什么 |
| --- | --- |
| 研究质量闭环 | 子问题覆盖度 → 缺口补检（最多再 1 轮）→ 自适应抓取 → 内容指纹去重 → claim→quote 支持校验 |
| 报告质量闭环 | 证据卡片化（子问题/类型/营销标记/去重）→ 大纲分节 → 分节写作（摘要三段式 + 推演）→ 引用硬校验（重写更差则丢弃） |
| 人工审批与写门禁 | 3 次 `interrupt`；`human_approved=True` 只出现在 `execute_actions`；未确认直接 `WriteActionNotApproved` |
| 会话持久化与多轮 | SQLite checkpointer 断点恢复 + 会话索引；`parent_thread_id` 注入上一轮结论（≤1200 字） |
| 可观测 | 节点 Span（耗时/token/成本）、价目表最长前缀匹配、未知模型标「估算」、`/api/trace` 与成本汇总 |
| 知识库 RAG | 标题分块（800/120 重叠）→ BM25（中文 bigram）→ 可选向量 → RRF；无 Key 自动降级 BM25 |

更细的实现记录见 [开发文档/OPTIMIZATION.md](开发文档/OPTIMIZATION.md)，
防编造/重试/编码/报告生成的细节见 [开发文档/质量与可靠性设计.md](开发文档/质量与可靠性设计.md)。

![调用链 Trace](原型图/web-ui-trace.png)

![知识库来源](原型图/web-ui-rag.png)

## 架构与流程

```mermaid
flowchart TD
    A[receive_question] --> B[plan_research]
    B --> C[search_web]
    C --> D[fetch_pages]
    D --> K[retrieve_knowledge]
    K --> E[summarize_sources<br/>证据卡片化]
    E --> Q{check_coverage}
    Q -- 覆盖不足 --> C
    Q -- 覆盖达标 --> O[plan_outline]
    O --> F[draft_report<br/>分节写作]
    F --> R[draft_review<br/>引用硬校验]
    R --> G{human_review_draft<br/>interrupt ①}
    G -- 通过 --> H{human_confirm_actions<br/>interrupt ②③}
    G -- 修改意见 --> O
    G -- 终止 --> J[finalize]
    H --> I[execute_actions<br/>human_approved=True]
    I --> J
```

| # | 节点 | 作用 |
| --- | --- | --- |
| 1 | `receive_question` | 校验问题、记录 LLM 模式、token 基线 |
| 2 | `plan_research` | Function Calling/JSON/启发式生成 2~3 条 query + 3~5 个子问题 |
| 3 | `search_web` | 多提供方搜索（Bing/360/DDG/Wikipedia）、URL 去重、多轮合并 |
| 4 | `fetch_pages` | 自适应页数、候选重排、编码/正文清洗、URL + 内容指纹去重 |
| 5 | `retrieve_knowledge` | 知识库混合检索，命中转 `kb://` 来源 |
| 6 | `summarize_sources` | 提取证据 + claim 校验 + 证据卡片化 |
| 7 | `check_coverage` | 子问题覆盖度；不足时生成缺口 query（最多补 1 轮） |
| 8 | `plan_outline` | 按子问题排大纲并分配 `[S#]`；改稿先改大纲 |
| 9 | `draft_report` | 分节写作：摘要三段式、关键发现程序生成、推演带【推断】 |
| 10 | `draft_review` | 引用/结构硬校验 → 最小改动重写（更差则丢弃）→ 程序修复 |
| 11 | `human_review_draft` | **interrupt ①**：通过 / 修改 / 终止 |
| 12 | `human_confirm_actions` | **interrupt ②③**：发邮件前、存 Notion 前分别确认 |
| 13 | `execute_actions` | 只对已确认动作调用写工具 |
| 14 | `finalize` | 最终报告、来源、证据、覆盖率、token/成本/耗时、日志摘要 |

**关键机制**：

- 图编译时挂 `SqliteSaver`（`data/checkpoints.sqlite`），重启后同一 `thread_id` 可 `resume`；
- 两条条件回环：覆盖不足回搜索、草稿被修改回大纲；
- 写操作门禁统一在 `tools.call_tool()`，`execute_actions` 是唯一传 `human_approved=True` 的地方；
- 模型不可用时全链路降级（规划/证据/报告都有离线兜底），不会让图崩掉。

**目录结构**（细节见 [backend/README.md](backend/README.md)、[frontend/README.md](frontend/README.md)）：

```
.
├── graph.py / state.py / tools.py / llm_client.py / prompts.py …  # Agent 核心（14 节点）
├── web.py / index.html / launch.html / start.bat                  # 网页入口与启动
├── quality.py / reporting.py / rag.py / tracing.py / persistence.py
├── 原型图/ 开发文档/  frontend/ backend/ docs/                     # 截图 / 开发文档 / 代码索引
├── evaluation/ knowledge/ config/ data/ logs/ outbox/             # 评测 / 知识库 / 配置 / 运行产物
└── tests/                                                          # 23 文件 / 166 用例
```

## 评测数据

真实联网 40 题（`real_p0`，离线 LLM + 真实检索）：

| 指标 | 结果 |
| --- | --- |
| 任务成功率 | **85.0%**（多跳 70.0%、对比 70.0%） |
| 引用可追溯 / 可点击 | **100% / 100%** |
| 工具调用正确率 | 90.0% |
| 子问题证据覆盖率 | 51.2% |
| 证据断言支持率 | 99.4%（规则代理） |
| 平均检索轮次 | 1.68 |
| 平均 / P95 延迟 | 15.6s / 26.7s |

mock 流水线基线：成功率 100%、覆盖率 90%、支持率 100%、延迟 21ms。

> **口径说明**：以上是“离线启发式 LLM + 真实联网”的结果，token/成本为 0，不代表真实模型写作质量。
> 完整表格、失败案例、消融与指标定义见 [开发文档/评测方法.md](开发文档/评测方法.md) 与
> [开发文档/eval_report.md](开发文档/eval_report.md)。

## 环境变量

复制 `.env.example` 为 `.env` 后按需修改；网页「模型设置」可覆盖 Key/Base/模型（仅进程内存）。

| 常用变量 | 说明 |
| --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | 模型接入；Key 留空进入离线模式 |
| `AGENT_LLM_MODE` | `auto`（默认）/ `openai` / `offline` |
| `AGENT_WEB_PORT` | 网页端口，默认 `8000` |
| `AGENT_CHECKPOINT_DB` / `AGENT_SESSIONS_DB` | 默认 `data/checkpoints.sqlite` / `data/sessions.sqlite` |
| `AGENT_KB_DIR` / `AGENT_KB_INDEX` | 知识库目录（默认 `knowledge/`）/ 索引缓存（`data/kb_index.json`） |
| `AGENT_LOG_DIR` / `AGENT_TRACE_DIR` | 日志与 trace 目录，默认 `logs` |
| `AGENT_OUTBOX_DIR` | dry-run 输出目录，默认 `outbox` |

<details>
<summary>完整环境变量表（点击展开）</summary>

| 变量 | 说明 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 或兼容服务 Key；留空自动离线 |
| `OPENAI_BASE_URL` | 兼容 OpenAI 的网关地址，可留空 |
| `OPENAI_MODEL` | 模型名，默认 `gpt-4o-mini` |
| `AGENT_LLM_MODE` | `auto` / `openai` / `offline` |
| `AGENT_MAX_PAGES` / `AGENT_MAX_PAGES_HARD` | 基础抓取页数 3 / 自适应硬上限 6 |
| `AGENT_MAX_RETRIEVAL_ROUNDS` | 总检索轮次上限，默认 2 |
| `AGENT_COVERAGE_MIN` / `AGENT_COVERAGE_THRESHOLD` | 触发补检 0.75 / 单子问题覆盖阈值 0.6 |
| `AGENT_MAX_GAP_QUERIES` | 一次补检最多几条 query，默认 2 |
| `AGENT_EVIDENCE_MAX` / `AGENT_EVIDENCE_PER_SOURCE` | 离线证据总条数/每来源条数，16 / 3 |
| `AGENT_TOOL_MAX_ATTEMPTS` / `AGENT_RETRY_BASE_DELAY` | 重试次数 3 / 退避基数 0.5s |
| `AGENT_SEARCH_PROVIDERS` | 搜索提供方顺序，默认 `bing,so360,duckduckgo,wikipedia` |
| `AGENT_SEARCH_TIMEOUT` / `AGENT_FETCH_TIMEOUT` / `AGENT_SUMMARIZE_TIMEOUT` | 单次工具预算 20 / 15 / 25s |
| `AGENT_SEARCH_NODE_BUDGET` / `AGENT_FETCH_NODE_BUDGET` | 节点时间预算 40 / 45s |
| `AGENT_FETCH_MAX_ATTEMPTS` | 抓取阶段最多尝试 URL 数，默认 8（成功 3 个即止） |
| `AGENT_PROVIDER_FAILURE_LIMIT` | 提供方连续失败几次后跳过，默认 2 |
| `AGENT_MOJIBAKE_RATIO` | 乱码段落丢弃阈值，默认 0.3 |
| `AGENT_LLM_TIMEOUT` | 单次 LLM 请求超时，默认 30s |
| `AGENT_PRICE_INPUT_PER_MTOK` / `AGENT_PRICE_OUTPUT_PER_MTOK` | token 单价（元/百万），1.0 / 4.0 |
| `AGENT_LINK_CITATIONS` | `[S#]` 是否可点击，默认 1；0 用于消融 |
| `AGENT_PLAN_SUB_QUESTIONS` | 是否生成子问题，默认 1；0 用于消融 |
| `DEFAULT_EMAIL_TO` | 邮件默认收件人 |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | 配置后真实发信，否则 dry-run 落盘 |
| `NOTION_TOKEN` + `NOTION_PAGE_ID`/`NOTION_DATABASE_ID` | 配置后真实写 Notion，否则 dry-run |
| `AGENT_OUTBOX_DIR` | dry-run 输出目录，默认 `outbox` |
| `AGENT_LOG_DIR` | JSONL 日志目录，默认 `logs` |
| `AGENT_AUTO_APPROVE` | `1` 时自动批准（仅演示/CI） |
| `AGENT_WEB_PORT` | 网页端口，默认 `8000` |
| `AGENT_CHECKPOINT_DB` / `AGENT_SESSIONS_DB` | SQLite 路径（断点恢复 / 历史列表） |
| `AGENT_TRACE_DIR` | Span 落盘目录，默认跟随 `AGENT_LOG_DIR` |
| `AGENT_KB_DIR` / `AGENT_KB_INDEX` / `AGENT_KB_TOP_K` | 知识库目录 / 索引缓存 / 返回片段数 6 |
| `AGENT_KB_CHUNK_SIZE` / `AGENT_KB_OVERLAP` | 分块 800 / 重叠 120 |
| `AGENT_EMBEDDING_MODEL` | embedding 模型，默认 `text-embedding-3-small`；失败降级 BM25 |
| `AGENT_EVIDENCE_CARDS` / `AGENT_EVIDENCE_CARDS_MAX` | 证据卡片化开关 / 上限 20 |
| `AGENT_DRAFT_SECTIONS_MAX` / `AGENT_DRAFT_REVIEW` / `AGENT_DRAFT_MAX_REWRITE` | 分节数 3 / 校验开关 / 重写次数 1 |

</details>

密钥优先级：**网页「模型设置」（进程内存）→ `.env` → 离线模式**；代码里没有硬编码 Key。

## 接口

<details>
<summary>完整接口表（点击展开）</summary>

| 接口 | 说明 |
| --- | --- |
| `POST /api/research` | 启动一次研究，返回第一次中断或最终结果（可带 `parent_thread_id`） |
| `POST /api/resume` | 提交人工确认，返回下一次中断或最终结果 |
| `POST /api/research/stream` / `POST /api/resume/stream` | 同上，NDJSON 流式：`start` → `log`… → `result` / `error` |
| `GET /api/sessions?limit=20` | 历史会话列表 |
| `GET /api/sessions/{thread_id}` | 加载单个会话（含中断载荷） |
| `DELETE /api/sessions/{thread_id}` | 删除会话（索引 + 该 thread 的 checkpoint/writes） |
| `POST /api/sessions/{thread_id}/interrupt` | 把 `running` 会话标成 `interrupted` |
| `GET /api/health` | 健康检查、LLM 模式/模型、持久化方式 |
| `GET /api/trace/{trace_id}` | 节点级调用链：耗时 / token / 成本 / 错误 |
| `GET /api/cost/summary?days=7` | 按天/按模型聚合成本 |
| `GET/POST/DELETE /api/config`、`POST /api/config/test` | 模型配置（掩码、测试连接、清除） |
| 页面路由 | `/` 启动页、`/launch` 启动页、`/index.html` 主界面、`/app` 主界面 |

</details>

请求/响应字段、流式事件结构、人工确认载荷与错误码见 **[开发文档/接口文档.md](开发文档/接口文档.md)**；
数据库表结构见 [开发文档/数据库设计文档.md](开发文档/数据库设计文档.md)。

## 测试与验收

```bash
pytest -q                              # 166 passed
python evaluation/acceptance_check.py  # 9/9（生成 FUNCTIONAL_ACCEPTANCE.md）
python evaluation/kb_check.py          # 6/6（只给 knowledge/、不联网）
```

测试分层、验收对照表与夹具说明见 [开发文档/测试与验收.md](开发文档/测试与验收.md)。

## 目录与文档

| 位置 | 内容 |
| --- | --- |
| [原型图/](原型图/) | 15 张页面原型/界面截图（含索引） |
| [开发文档/](开发文档/) | 接口文档、数据库设计、质量与可靠性设计、测试与验收、评测方法、优化记录、验收/评测报告 |
| [frontend/](frontend/) · [backend/](backend/) | 前端/后端代码索引（代码仍在根目录） |
| [tests/](tests/) | 23 个测试文件 / 166 用例 |
| [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) | 项目快照（架构、能力矩阵、限制） |
| [RESUME_ROADMAP.md](RESUME_ROADMAP.md) | 简历 STAR 话术与面试问答（个人向） |
| [docs/](docs/) | 已迁移，仅指向 原型图/ |

## 已知限制

- **单机原型**：SQLite 单写者，多实例需换 `PostgresSaver`；无鉴权/限流，只监听 `127.0.0.1`。
- **写操作默认 dry-run**：未配置 SMTP/Notion 时只落盘 `.eml` / `.md`。
- **检索质量是瓶颈**：真实评测子问题覆盖率 51.2%，主要卡在对比题只抓到一方、站点 403/JS 空页。
- **规则代理指标**：覆盖率/支持率是词面与规则判断，不是语义蕴含，不能替代人工抽检。
- **延迟**：分节写作 + 补检让单题约 15~30s（真实联网）；P95 约 26.7s。
- **中断后只能重跑**：checkpoint 只保证人工审批点可恢复；普通节点执行中被杀只能重跑或删除会话。
- **真实 LLM 未全量评测**：40 题仍是离线 LLM + 真实联网；`glm-4-flash` 等免费模型成本真实为 0。
- **知识库规模**：纯 Python 余弦，约 1 万 chunk 上限；embedding 需服务支持 `/embeddings`。

完整清单见 [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) §9。

## 常见问题

**Q：`ModuleNotFoundError: No module named 'fastapi'`**
运行 `web.py` 的解释器不是项目 `.venv`。`web.py` 会自动尝试切到 `.venv`；手动方式：
`.\.venv\Scripts\python.exe web.py`。

**Q：页面一直显示「正在检索、抓取并总结证据…」**
流式接口每完成一步就会出现一行日志。若 20 秒内一行都没有，检查终端里 uvicorn 是否还在、
端口是否被占用；可用 `AGENT_SEARCH_TIMEOUT` / `AGENT_FETCH_NODE_BUDGET` 收紧预算。

**Q：改了代码/配置但页面没变化**
`uvicorn` 不是热重载，改 `web.py` 或 `.env` 后要重启；浏览器再 `Ctrl+F5` 强刷。

**Q：模型设置里保存了 Key，重启后又没了**
设计如此：Key 只存后端进程内存，不写 `.env`、不存浏览器；需要持久化请写 `.env`。

**Q：Windows 终端中文乱码**
先执行 `chcp 65001` 再运行。

---

> 文档分工：本 README 面向“快速了解与上手”；开发细节在 [开发文档/](开发文档/)；
> 项目全貌在 [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)；简历/面试材料在 [RESUME_ROADMAP.md](RESUME_ROADMAP.md)。
