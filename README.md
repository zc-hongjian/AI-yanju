# LangGraph 研究助手（Research Assistant MVP）

一个**只做研究助手窄场景**的 Agent：接收研究问题 → 规划检索 → 搜索网页 → 抓取正文 →
总结证据 → 生成报告草稿 → **人工确认** → 写邮件 / 存 Notion → 输出最终结果。

技术栈：Python + LangGraph（`StateGraph` + `interrupt` + checkpointer）+ Function Calling
（OpenAI / 兼容 OpenAI 的服务）。同一个 Agent 有两种入口：命令行 `main.py` 与网页版
`web.py`（FastAPI + 单页 HTML），两者共用同一张图、同一套工具与写操作门禁。

> 设计目标：可运行的 MVP，而不是通用 Agent 框架。工具固定 5 个，写操作必须人工确认。

---

## 1. 工作流

```mermaid
flowchart TD
    A[receive_question] --> B[plan_research<br/>Function Calling → search_web]
    B --> C[search_web 工具]
    C --> D[fetch_pages 工具]
    D --> K[retrieve_knowledge<br/>知识库 BM25 + 向量 RRF]
    K --> E[summarize_sources 工具<br/>网页证据 + 知识库证据 + 证据卡片化]
    E --> Q{check_coverage<br/>子问题覆盖度}
    Q -- 覆盖不足 --> C
    Q -- 覆盖达标 --> O[plan_outline<br/>按子问题分节 + 分配证据]
    O --> F[draft_report<br/>分节写作 + 摘要三段式 + 推演]
    F --> R[draft_review<br/>引用/结构硬校验]
    R --> G{human_review_draft<br/>interrupt ① 草稿}
    G -- 通过 --> H{human_confirm_actions<br/>interrupt ② 发邮件前<br/>interrupt ③ 存 Notion 前}
    G -- 修改意见 --> O
    G -- 终止 --> J[finalize]
    H --> I[execute_actions<br/>human_approved=True]
    I --> J
```

- **研究质量闭环（P0）**：`check_coverage` 会按 3~5 个子问题计算证据覆盖度；低于阈值时
  用缺口生成补检 query，最多再检索一轮（默认总轮次 2）；`summarize_sources` 进入报告前还会做
  claim→quote 支持性校验，明显外推/无关的“证据”会被过滤并记录。
- **报告质量闭环**：证据先卡片化（归属子问题 / 类型 / 去重 / 营销标记），`plan_outline` 按子问题
  排大纲并显式分配 `[S#]`，`draft_report` 逐节写作（摘要固定「结论 → 依据 → 边界」三段式，
  「分析与推演」每条带【推断】），`draft_review` 对引用编号、单句角标数、引用相关性、小节完整性
  做硬校验：不达标先让模型最小改动重写一次（**重写后问题更多就丢弃**），再程序剪枝/补节兜底。
- **三处 `interrupt()`**：草稿审核、发邮件前、存 Notion 前。后两处都在 `human_confirm_actions`
  节点内顺序执行，各自产生一次独立的暂停/恢复，因此邮件与 Notion 可以分别同意或拒绝。
- 图编译时挂 **SQLite checkpointer**（`persistence.build_checkpointer()`，库文件 `data/checkpoints.sqlite`），
  `invoke` 返回 `__interrupt__`，`Command(resume=...)` 恢复；进程重启后仍能继续。
- 草稿审核支持「通过 / 修改（重新生成，最多 2 轮）/ 终止」。
- 只有 `execute_actions` 会以 `human_approved=True` 调用写工具，且仅针对人工同意的动作。

### 检索规划：技术搜索专家

`plan_research` 先做**问题拆解**（3~5 个可独立检索的子问题），再用 `prompts.PLAN_SYSTEM` 的「技术搜索专家」规则转成 2~3 个关键词组合：

- **How 类问题**（如何实现 / 原理 / 教程）强制加入技术词：「代码示例」「tutorial」「documentation」「implementation」；
- **命中框架**（LangGraph / FastAPI / React 等）时加入官方文档限定词，如 `site:langchain.com`、`site:fastapi.tiangolo.com`、`site:github.com`；
- **去掉口语化表达**（「我想知道」「怎么弄」「请问」「帮我」）；
- 优先用 **Function Calling** 提交 `search_web` 调用；网关不支持 tools 时回退到 **JSON 数组输出**（`PLAN_JSON_SYSTEM`，输出 `["搜索词1", "搜索词2"]`）。

离线模式（无 Key）由 `LLMClient._heuristic_plan()` 在本地按同一套规则生成关键词，并做去重与 2~3 条上限；`heuristic_sub_questions()` 生成 4 个子问题。

## 2. 文件结构

```
.
├── 原型图/               # 页面原型/界面截图（15 张，原 docs/）
├── 开发文档/             # 接口文档、数据库设计文档、优化记录、验收/评测报告
├── frontend/             # 前端代码索引（代码仍在根目录：index.html / launch.html）
├── backend/              # 后端代码索引（代码仍在根目录：*.py）
├── docs/                 # 已迁移，仅保留指向 原型图/ 的索引
│
├── graph.py             # LangGraph 图：14 个节点 + 条件边 + interrupt + checkpointer
├── state.py             # ResearchState TypedDict + 初始状态
├── quality.py           # 研究质量闭环：子问题覆盖度 + claim→quote 支持性校验
├── reporting.py         # 报告质量：证据卡片化 + 大纲 + 引用硬校验/程序修复
├── rag.py               # 知识库：分块 + BM25 + 可选向量 + RRF 融合 + 索引缓存
├── tracing.py           # 可观测性：节点 Span + 模型价目表成本归因
├── tools.py             # 5 个工具、JSON Schema、重试装饰逻辑、写操作门禁
├── text_clean.py        # 网页编码探测（charset_normalizer）+ 乱码段落清洗
├── prompts.py           # 全部提示词
├── logging_utils.py     # 结构化日志（state + JSONL）
├── human_review.py      # 命令行人工确认（草稿 / 写操作参数）
├── llm_client.py        # OpenAI Function Calling 客户端 + 离线启发式回退
├── persistence.py       # 持久化：SQLite checkpointer（断点恢复）+ 会话索引表
├── main.py              # 命令行入口（中断-恢复循环）
├── check_llm.py         # 真实大模型接入自检（读 .env + 真实调用一次）
├── web.py               # 网页版入口（FastAPI，复用同一张图与同一套门禁）
├── launch.html          # 一键启用启动页（检查后端/模型状态 → 进主界面并弹模型设置）
├── start.bat            # Windows 一键启动：起后端 + 4 秒后自动打开启动页
├── index.html           # 单页前端（可直接双击演示；由 web.py 托管时连真实后端）
├── evaluation/          # 40 题评测集 + 跑分脚本 + mock fixture（报告见 开发文档/）
│   ├── dataset.jsonl    # 测试集（简单事实 / 对比 / 时效 / 多跳，各 10 题）
│   ├── run_eval.py      # 逐题跑图并统计指标
│   ├── mock_web.py      # 确定性 fixture（仅测流水线机制）
│   ├── make_report.py   # 汇总生成评测报告
│   ├── acceptance_check.py # 9 项功能闭环验收，生成功能验收报告
│   ├── kb_check.py      # 知识库验收：只给 knowledge/，问 6 个只能靠 KB 回答的问题
│   └── results/         # 每次评测的 JSONL / summary / 人工抽样（gitignore）
├── knowledge/           # 本地知识库（md/txt）：LangGraph HITL、评测口径、RAG 设计
├── config/pricing.json  # 模型价目表（元/百万 token，按模型最长前缀匹配）
├── data/                # 运行期 SQLite + 知识库索引（已 gitignore）
├── logs/ outbox/        # 运行期日志与 dry-run 产物（已 gitignore）
├── README.md            # 使用与验收文档（主文档）
├── PROJECT_SUMMARY.md   # 项目快照
├── RESUME_ROADMAP.md    # 简历/面试材料
├── requirements.txt / pytest.ini / .env.example / .gitignore
└── tests/               # 23 个测试文件 / 166 个用例
    ├── conftest.py
    ├── test_graph_flow.py        # 端到端：三次中断、确认前不写、日志链路、无来源降级
    ├── test_tools_retry.py       # 重试 3 次 + 指数退避 + 耗尽后结构化失败
    ├── test_tools_safety.py      # SSRF：内网 IP / 解析到内网的域名一律拒绝
    ├── test_web_api.py           # Web API：三次中断、确认前零写入、流式协议
    ├── test_write_guard.py       # 未确认写操作被拒绝 / 参数无法伪造 human_approved
    ├── test_evidence_integrity.py# 编造 URL、改写引文被过滤；报告 URL 白名单清洗
    ├── test_reporting.py         # 报告质量：卡片化/大纲/引用校验/修复
    ├── test_persistence.py       # 重启恢复、历史会话、删除与中断状态
    └── …                          # 其余 15 个测试文件见 tests/ 目录
```



```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 4. 环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

| 变量 | 说明 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 或兼容服务的 Key；**留空则自动进入离线启发式模式** |
| `OPENAI_BASE_URL` | 兼容 OpenAI 的网关地址，可留空 |
| `OPENAI_MODEL` | 模型名，默认 `gpt-4o-mini` |
| `AGENT_LLM_MODE` | `auto`（默认）/ `openai` / `offline` |
| `AGENT_MAX_PAGES` | 最多抓取页面数（基础值），默认 3；对比/多跳题会自动 +1，补检轮再 +1 |
| `AGENT_MAX_PAGES_HARD` | 自适应抓取页数硬上限，默认 6 |
| `AGENT_MAX_RETRIEVAL_ROUNDS` | 总检索轮次上限，默认 2（首轮 + 最多 1 轮补检） |
| `AGENT_COVERAGE_MIN` | 触发补检的子问题覆盖率阈值，默认 0.75 |
| `AGENT_COVERAGE_THRESHOLD` | 单个子问题算“已覆盖”的分数阈值，默认 0.6 |
| `AGENT_MAX_GAP_QUERIES` | 一次补检最多生成几条 query，默认 2 |
| `AGENT_EVIDENCE_MAX` / `AGENT_EVIDENCE_PER_SOURCE` | 离线抽取式证据总条数 / 每来源条数上限，默认 16 / 3 |
| `AGENT_TOOL_MAX_ATTEMPTS` | 工具最大尝试次数，默认 3 |
| `AGENT_RETRY_BASE_DELAY` | 指数退避基数，默认 0.5 秒（0.5 → 1 → 2 …） |
| `AGENT_SEARCH_PROVIDERS` | 搜索提供方顺序，默认 `bing,so360,duckduckgo,wikipedia`；中文宽泛问题自动把 360 提前 |
| `AGENT_SEARCH_TIMEOUT` / `AGENT_FETCH_TIMEOUT` / `AGENT_SUMMARIZE_TIMEOUT` | 单次工具时间预算（秒），默认 20 / 15 / 25；超预算停止重试 |
| `AGENT_SEARCH_NODE_BUDGET` / `AGENT_FETCH_NODE_BUDGET` | 整个节点时间预算（秒），默认 40 / 45 |
| `AGENT_FETCH_MAX_ATTEMPTS` | 抓取阶段最多尝试多少个 URL，默认 8（成功 3 个即止） |
| `AGENT_PROVIDER_FAILURE_LIMIT` | 某搜索提供方连续失败几次后在本进程内跳过，默认 2 |
| `AGENT_MOJIBAKE_RATIO` | 乱码段落丢弃阈值：可疑字符占比超过该值即丢弃，默认 `0.3` |
| `AGENT_LLM_TIMEOUT` | 单次 LLM 请求超时（秒），默认 30 |
| `AGENT_PRICE_INPUT_PER_MTOK` / `AGENT_PRICE_OUTPUT_PER_MTOK` | token 成本单价（元/百万 token），默认 1.0 / 4.0，用于 `final_output.cost_cny` |
| `AGENT_LINK_CITATIONS` | `1`（默认）把报告里的 `[S#]` 变成可点击来源；`0` 用于评测消融 |
| `AGENT_PLAN_SUB_QUESTIONS` | `1`（默认）生成 3~5 个子问题；`0` 用于评测消融 |
| `DEFAULT_EMAIL_TO` | 草稿确认后邮件收件人默认值 |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | 配置后真实发信；不配置则写入 `outbox/emails/`（dry-run） |
| `NOTION_TOKEN` + `NOTION_PAGE_ID`/`NOTION_DATABASE_ID` | 配置后真实写入 Notion；否则写入 `outbox/notion/`（dry-run） |
| `AGENT_OUTBOX_DIR` | dry-run 输出目录，默认 `outbox` |
| `AGENT_LOG_DIR` | JSONL 日志目录，默认 `logs` |
| `AGENT_AUTO_APPROVE` | `1` 时跳过交互自动批准（仅演示/CI） |
| `AGENT_WEB_PORT` | 网页版端口，默认 `8000` |
| `AGENT_CHECKPOINT_DB` | SQLite checkpointer 路径，默认 `data/checkpoints.sqlite`（服务重启后可恢复审批中断） |
| `AGENT_SESSIONS_DB` | 会话索引库路径，默认 `data/sessions.sqlite`（历史会话列表） |
| `AGENT_TRACE_DIR` | 节点 Span 落盘目录，默认跟随 `AGENT_LOG_DIR`（`<trace_id>.trace.jsonl`） |
| `AGENT_KB_DIR` | 本地知识库目录，默认 `knowledge/`；为空目录时 RAG 节点自动跳过 |
| `AGENT_KB_INDEX` | 知识库索引缓存路径，默认 `data/kb_index.json` |
| `AGENT_KB_TOP_K` | 每次检索返回的片段数，默认 6 |
| `AGENT_KB_CHUNK_SIZE` / `AGENT_KB_OVERLAP` | 分块大小 / 重叠字符数，默认 800 / 120 |
| `AGENT_EMBEDDING_MODEL` | embedding 模型名，默认 `text-embedding-3-small`；智谱用 `embedding-3`；失败自动降级 BM25 |
| `AGENT_EVIDENCE_CARDS` | `1`（默认）把证据升级成卡片（归属子问题/类型/去重/营销标记）；`0` 用于消融 |
| `AGENT_EVIDENCE_CARDS_MAX` | 证据卡片上限，默认 20 |
| `AGENT_DRAFT_SECTIONS_MAX` | 报告按几个子问题分节（不含摘要/推演/局限），默认 3 |
| `AGENT_DRAFT_REVIEW` | `1`（默认）做引用/结构硬校验并重写；`0` 只记录指标不重写 |
| `AGENT_DRAFT_MAX_REWRITE` | 校验不通过时最多重写几次，默认 1（重写效果更差会被丢弃） |

密钥来源优先级：**网页「模型设置」保存的运行时配置**（仅进程内存，重启失效）→ `.env` → 离线模式；
代码里没有硬编码 Key。运行时配置不会写回 `.env`。

### 4.1 接入真实大模型（OpenAI / 国内兼容接口 / 本地）

目前默认跑离线启发式；接入真实模型只需要三步（也可以跳过 `.env`，直接在网页右上角「模型设置」里填写）：

**① 创建 `.env` 并填 Key**（`.env` 已在 `.gitignore` 里，不会进仓库；**不要把 Key 填进 `.env.example`，它只是模板，程序不会读取**）：

```bash
copy .env.example .env        # Windows
# cp .env.example .env       # macOS / Linux
```

```ini
OPENAI_API_KEY=sk-你的Key
OPENAI_BASE_URL=            # OpenAI 官方可留空；兼容网关填到 /v1
OPENAI_MODEL=gpt-4o-mini
AGENT_LLM_MODE=auto         # auto：有 Key 用真实模型，没有则离线
```

**② 按服务商填写 Base URL / 模型名**（都走 OpenAI 兼容协议）：

| 服务 | `OPENAI_BASE_URL` | `OPENAI_MODEL` 示例 |
| --- | --- | --- |
| OpenAI 官方 | 留空（默认 `https://api.openai.com/v1`） | `gpt-4o-mini` / `gpt-4.1-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问（DashScope 兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b`（Key 随便填一个非空值） |

**③ 自检并重启**：

```bash
python check_llm.py          # 读取 .env，真实调用一次模型，打印 provider / token / 耗时
python web.py                # 重新启动网页版（改 .env 后必须重启）
```

自检成功会打印「已连接到真实模型」；如果仍是「离线模式」，按提示检查 Key / Base URL（常见问题：Base URL 忘了 `/v1`、Key 复制带了空格、超时时间太短）。

**如果 `check_llm.py` 显示 `.env 文件: 存在`，但 `API Key: （空）`、模型是 `gpt-4o-mini`**：说明当前终端用的是没装 `python-dotenv` 的系统 Python（`.env` 根本没被加载）。新版 `check_llm.py` 会自动切换到项目 `.venv`；也可以显式用：

```bash
.venv\Scripts\python.exe check_llm.py     # Windows
.venv/bin/python check_llm.py             # macOS / Linux
```

重新填 Key / Base URL / 模型也可以直接命令写入（Key 输入不回显）：

```bash
python check_llm.py --set-key --base-url https://api.deepseek.com/v1 --model deepseek-chat
```

接入后哪些环节会变化：

- `plan_research` 走 **Function Calling**（网关不支持 tools 时自动回退 JSON 规划）；
- `summarize_sources` 由模型按「严谨研究助理」规则提取证据，`filter_evidence` 继续逐字校验；
- `draft_report` 由模型生成三段式报告，`_sanitize_report` 继续做 URL 白名单与引用清洗；
- 模型报错/超时会自动降级到离线启发式，不会让整张图崩掉；
- `final_output.token_usage / cost_cny` 变成真实值（按 `AGENT_PRICE_INPUT_PER_MTOK` / `AGENT_PRICE_OUTPUT_PER_MTOK` 估算，人民币）。

建议接入后的下一步：

```bash
python check_llm.py
python evaluation/run_eval.py --mode real --llm openai --tag real_llm --limit 10   # 先跑 10 题看质量/成本
python evaluation/run_eval.py --mode real --llm openai --tag real_llm              # 全量 40 题
python evaluation/make_report.py                                                    # 更新 开发文档/eval_report.md
```

## 5. 运行

### 5.1 交互模式（推荐）

```bash
python main.py "LangGraph 如何实现人工中断（human-in-the-loop）？"
```

流程中会先后暂停三次：

1. 打印报告草稿，选择 `[1] 通过 / [2] 修改意见 / [3] 终止`；
2. 打印邮件收件人、主题、正文预览，确认是否发送，并可就地修改收件人/主题；
3. 打印 Notion 标题、标签、内容预览，确认是否保存，并可就地修改标题/标签。

### 5.2 自动模式（演示 / CI）

```bash
python main.py "你的研究问题" --auto
```

自动批准草稿与两项写操作，便于一次跑完看最终输出；**不会绕过工具层的 `human_approved` 校验**。

### 5.3 没有 LLM Key 也能跑

不配置 `OPENAI_API_KEY` 时自动进入 `offline` 模式：

- 检索计划：关键词启发式；
- 证据总结：**逐句摘录抓取到的正文**（不调用模型，不改写）；
- 报告草稿：模板生成，事实全部来自摘录，并标注来源 URL。

离线模式仍会真实调用 `search_web` / `fetch_page`：

- 搜索默认按 `Bing → 360 → DuckDuckGo → Wikipedia` 顺序尝试，任一可用即可；中文宽泛问题会自动把
  360（so.com）提前（Bing 对「2025 年 AI Agent 企业落地」这类问题常返回统计公报/百科等不相关结果），
  命中 LangGraph 等具体技术词时仍优先 Bing（官方文档更全）；可用 `AGENT_SEARCH_PROVIDERS` 调整顺序或删减；
- 抓取到的页面 URL 是真实来源，报告中的引用均可回溯。

若要完整的模型写作与真实 Function Calling，填 `OPENAI_API_KEY` 即可。

### 5.4 输出

- 终端：最终报告、写操作结果、错误与降级记录、按 `step_id` 排序的日志表；
- `logs/research-<trace_id>.jsonl`：完整结构化日志；
- `outbox/emails/*.eml`、`outbox/notion/*.md`：未配置 SMTP/Notion 时的 dry-run 产物。

> Windows 终端若出现中文乱码，先执行 `chcp 65001` 再运行。

### 5.5 网页版（浏览器）

```bash
python web.py
# 或
uvicorn web:app --host 127.0.0.1 --port 8000
```

页面文件是项目根目录的 `index.html`，有两种用法：

- **连真实后端**：先 `python web.py`，访问 <http://127.0.0.1:8000> 看到**一键启用启动页**；
  主界面在 <http://127.0.0.1:8000/index.html>（`/app` 等价），启动页的按钮会自动带上模型设置参数；
- **纯静态演示**：直接双击 `index.html`（`file://`）→ 页面自动进入「静态演示模式」，
  用内置示例数据走完「草稿 → 邮件 → Notion」三次确认，不需要后端；页面顶部会明确标注
  「静态演示模式：数据是示例，未连接后端」。也可以带参数直接看演示：
  `index.html?q=你的研究问题&autostart=1`。

#### 一键启用（launch.html + start.bat）

- **双击 `start.bat`**：在项目根目录起 `web.py`，4 秒后自动打开启动页 `http://127.0.0.1:8000/`；
  端口跟随 `AGENT_WEB_PORT`（默认 8000），脚本失败会打印依赖/端口提示并 `pause`。
- 手动 `python web.py` 也一样：**根路径 `/` 就是启动页**（`/launch` 是同一个页面），
  主界面在 `/index.html` 或 `/app`。
- 启动页会先探测 `/api/health`：显示「后端已连接 · 模式 · 模型」或「后端未连接 + 启动命令」；
  主按钮「🚀 一键启用」= 打开主界面并**自动弹出模型设置**（`index.html?settings=1`），
  填完 Key 直接开跑；另有「跳过设置，直接进入」「跑一题示例」「历史会话」三个入口。
- 直接双击 `launch.html`（`file://`）也能用：检测不到后端时自动降级为
  「一键启用（静态演示）」，`?history=1` 入口会隐藏（静态模式没有后端）。

![一键启用启动页](原型图/web-ui-launch.png)

![一键启用后自动弹出模型设置](原型图/web-ui-launch-settings.png)

![静态演示模式的草稿确认界面](原型图/web-ui-demo.png)

打开 <http://127.0.0.1:8000/index.html> 后的真实流程：

![网页版界面](原型图/web-ui.png)

1. 输入研究问题 → **开始研究**（页面实时显示结构化日志与进度）；
2. **草稿确认**：通过并继续 / 填写修改意见重新生成 / 终止流程；
3. **邮件确认**：可就地修改收件人、主题 → 确认发送 或 跳过；
4. **Notion 确认**：可就地修改标题、标签 → 确认保存 或 跳过；
5. **最终结果**：状态、写操作结果、错误与降级、完整报告（可下载 Markdown）、完整日志表。

**执行进度卡片**：14 个分段节点进度条（补检回环只增不减，不会倒退）+ 阶段表情/轻松文案 +
当前百分比；「收起日志 / 展开日志」只折叠日志表格，**进度条与阶段文案始终可见**（默认展开，
运行中也能收起来省地方）。深链 `?collapse=1` 可直接以收起态打开，`?q=...&autostart=1` 自动跑静态演示。

![执行进度：分段节点进度条](原型图/web-ui-progress.png)

![执行进度收起后的样子](原型图/web-ui-progress-collapsed.png)

网页与 CLI 共用同一张 LangGraph 图、同一套工具和写操作门禁，后端接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/research` `{"question": "...", "parent_thread_id": "可选"}` | 启动一次研究，返回第一次中断或最终结果；带 `parent_thread_id` 即多轮追问 |
| `POST /api/resume` `{"thread_id": "...", "value": {...}}` | 提交人工确认，返回下一次中断或最终结果 |
| `POST /api/research/stream` / `POST /api/resume/stream` | 同上，但用 NDJSON **实时推送日志**，最后一行是 `result` 或 `error` |
| `GET /api/sessions?limit=20` | 历史会话列表（问题、状态、更新时间、父会话） |
| `GET /api/sessions/{thread_id}` | 加载单个会话，返回与 `/api/research` 同构的结构；中断态可直接继续审批 |
| `DELETE /api/sessions/{thread_id}` | 删除单条历史会话：会话索引 + 该 thread 的 checkpoint/writes（logs/trace 保留） |
| `POST /api/sessions/{thread_id}/interrupt` | 前端发现流中断时把 `running` 会话标成 `interrupted` |
| `GET /api/health` | 健康检查、当前 LLM 模式/模型、持久化方式 |
| `GET /api/trace/{trace_id}` | 节点级调用链：耗时 / token / 成本 / 错误，按开始时间排序 |
| `GET /api/cost/summary?days=7` | 按天 / 按模型聚合调用次数、token 与成本 |
| `GET /api/config` | 当前模型配置视图（`masked_key`，不含明文 Key） |
| `POST /api/config` | 保存页面填写的 Key / Base URL / 模型到**进程内存** |
| `POST /api/config/test` | 用当前表单做一次最小真实调用，验证 Key 是否可用 |
| `DELETE /api/config` | 清除运行时配置，回到 `.env` 或离线模式 |

网页默认走流式接口：搜索、抓取每完成一步，进度表就会实时出现一行日志，不会整段“看起来卡住”。

### 5.5.1 在页面里配置模型（右上角「模型设置」）

深色主题页面右上角有 **模型设置** 按钮，打开后可以：

- 选择提供方预设（DeepSeek / OpenAI / 通义千问 / 智谱 GLM / 本地 Ollama / 自定义），
  自动填充 Base URL 与模型名；
- 输入 API Key（`type=password`，可点「显示 / 隐藏」切换）；
- **测试连接**：用当前表单真实调用一次模型，显示模型名与延迟；
- **保存并使用**：只保存到 `web.py` 进程内存，不写入 `.env`、不写入浏览器
  localStorage/sessionStorage；保存后页面只显示 `sk-****abcd` 这类掩码；
- **清除运行时配置**：回到启动时 `.env` 的配置。

![模型设置面板](原型图/web-ui-settings.png)

> 安全边界：Key 只存在本机后端进程内存，服务重启即失效；接口只监听 `127.0.0.1`，
> 没有鉴权，不要暴露到公网。`.env` 中的 Key 仍会被读取，请确保 `.env` 不被提交（已在 `.gitignore`）。

![深色主题网页版](原型图/web-ui-dark.png)

### 5.5.2 会话持久化与多轮追问（P0-1）

会话状态落在 **SQLite checkpointer**（`data/checkpoints.sqlite`），不再随进程退出丢失：

- 到「草稿 / 邮件 / Notion」任一个人工暂停点时，状态已经写盘；
- 关掉 `web.py` 再启动，用同一个 `thread_id` 调 `POST /api/resume`（或页面里点“继续审批”）即可从暂停点继续；
- `GET /api/sessions` 列出历史会话（问题、状态、更新时间），`GET /api/sessions/{thread_id}` 加载详情；
- **历史会话可删除**：列表每行右侧「删除」→ 页面内二次确认 → `DELETE /api/sessions/{id}`，
  同时清掉该 thread 的 checkpoint/writes（`logs/` 与 trace 保留用于审计），删除后不可恢复；
- 会话索引单独存 `data/sessions.sqlite`，索引写失败只影响历史列表，不影响研究主流程。

**执行中后端退出 / 断网**：流式接口第一行会先推 `start` 事件（含 `thread_id`），前端中途断流时能定位会话：

- 进度条**冻结在当前节点**并标红（`已中断 · 第 3/14 步`），已产生的日志保留；
- 不再渲染一张空白的「最终结果（完成）」卡片，改为「会话未完成」卡片，提供「重新开始 / 删除该会话」；
- 前端尽力调 `POST /api/sessions/{id}/interrupt` 落库；后端重启时还会把遗留的 `running`
  会话统一标成 `interrupted`（进程都换了，不可能还在跑），历史列表不会一直显示「进行中」。

![历史会话：已中断状态与删除按钮](原型图/web-ui-history-delete.png)

![后端被杀后的中断态：进度冻结 + 未完成卡片](原型图/web-ui-interrupted.png)

多轮追问：`POST /api/research` 带 `parent_thread_id` 时，会读取父会话的结论摘要（问题 + 子问题 + 报告正文，
截断到 1200 字符）注入本轮 `history_context`，规划阶段据此避免重复检索；响应里的会话行会记录 `parent_thread_id`。

![历史会话抽屉](原型图/web-ui-history.png)

从历史会话点「继续审批」后，页面会还原完整日志与草稿审核面板（会话由上一个进程写入）：

![恢复后的待审批会话](原型图/web-ui-recover.png)

深链：`?history=1` 直接打开历史面板，`?session=<thread_id>` 直接加载某个会话（便于演示和排障）。

```bash
# 重启后继续一个待审批会话（thread_id 来自 /api/sessions）
curl -X POST http://127.0.0.1:8000/api/resume \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "web-xxxx", "value": {"decision": "approve", "comment": ""}}'

# 基于上一轮结论追问
curl -X POST http://127.0.0.1:8000/api/research \
  -H "Content-Type: application/json" \
  -d '{"question": "那 checkpointer 具体解决什么问题？", "parent_thread_id": "web-xxxx"}'
```

> 仍然是单机原型：单进程、无鉴权、无并发限流；SQLite 适合本机演示，多实例部署应换
> `PostgresSaver` 并把长任务转到队列（见 `RESUME_ROADMAP.md` 的 P1/P2）。

### 5.5.3 调用链 Trace 与成本归因（P0-2）

每次运行会额外写一份 `logs/<trace_id>.trace.jsonl`：每个节点一条 `span`，最后一条 `summary`。
结果页底部「调用链 Trace」展示节点耗时瀑布、每节点 token 与成本；未知模型会标记「估算」。

- 价目表在 `config/pricing.json`，按模型名**最长前缀**匹配（`glm-4-flash-0914` 命中 `glm-4-flash`）；
- 未命中时回退 `AGENT_PRICE_INPUT_PER_MTOK` / `AGENT_PRICE_OUTPUT_PER_MTOK`，`cost_estimated=true`；
- `final_output` 增加 `token_usage_by_node`、`cost_by_node`、`cost_estimated`、`cost_price_source`；
- 节点 token 之和等于运行总量（有单测断言），未知模型不会抛异常。

![调用链 Trace](原型图/web-ui-trace.png)

### 5.5.4 本地知识库 RAG（P0-3）

`knowledge/` 下的 `.md` / `.txt` 会被切成片段（800 字 / 120 重叠、保留标题路径），
`retrieve_knowledge` 节点用 **BM25 + 可选向量 + RRF** 检索 top-6，作为 `kb://` 来源
追加进 `fetched_pages`，与网页证据一起走同一套引用 / 校验链路。

- **无 embedding Key 也能用**：BM25 始终可用；embedding 失败自动降级并打印提示；
- 向量走 OpenAI 兼容 `/embeddings`，默认 `text-embedding-3-small`；智谱填 `AGENT_EMBEDDING_MODEL=embedding-3`；
- 索引缓存在 `data/kb_index.json`（文档 mtime/size + 模型名，变更自动重建）；
- 来源清单里知识库显示为 `📚 知识库 · 文档 · 小节`，不做成可点击链接（`kb://` 点击无意义）；
- 知识库为空时节点 `skipped`，行为与之前完全一致。

验收脚本（不联网、不调用付费 API，只给 `knowledge/`）：

```bash
python evaluation/kb_check.py   # 6/6 passed
```

![知识库来源与调用链](原型图/web-ui-rag.png)

### 5.6 常见问题：`ModuleNotFoundError: No module named 'fastapi'`

原因：运行 `web.py` 的 Python 解释器不是项目虚拟环境。例如从根目录 `D:\Build` 的 uv 环境
（`D:\Build\.venv`，其 `pyproject.toml` 里 `dependencies = []`）去跑本项目的文件，该环境里
没有 fastapi / langgraph。

现在 `web.py` 会自动检测：缺依赖时优先切换到项目自带的 `.venv` 再启动，并打印提示。手动方式：

```powershell
cd D:\Build\AI-hy
.\.venv\Scripts\python.exe web.py
```

或者给当前解释器安装依赖：

```powershell
D:\Build\.venv\Scripts\python.exe -m pip install -r D:\Build\AI-hy\requirements.txt
```

### 5.7 常见问题：页面一直显示「正在检索、抓取并总结证据…」

- 网页版走的是流式接口，**每一步完成就会在「执行进度」里出现一行日志**，并显示已完成的步数与耗时；
  如果 20 秒内一行都没有，说明请求还没到后端或服务已退出，看终端里 uvicorn 是否还在。
- 正常情况下一次研究约 5~20 秒；遇到坏页面（证书错误、JS 空页面）会快速跳过，不再重试 3 次。
- 若确实卡了很久：检查终端日志、确认没有配置无法访问的 `OPENAI_BASE_URL`；
  可用 `AGENT_SEARCH_TIMEOUT` / `AGENT_FETCH_NODE_BUDGET` 进一步收紧预算。

## 6. 结构化日志

每条日志固定字段（`S001` 递增，按 `step_id` 即可还原整条链路）：

```json
{
  "step_id": "S007",
  "node_name": "fetch_pages",
  "tool_name": "fetch_page",
  "input_summary": "https://example.org/alpha",
  "output_summary": "抓到正文 5231 字符",
  "status": "ok",
  "retry_count": 0,
  "latency_ms": 812,
  "error": null,
  "timestamp": "2026-11-04T21:30:12.345+08:00",
  "trace_id": "a1b2c3d4"
}
```

`status` 取值：`ok` / `degraded` / `error` / `rejected` / `skipped` / `approved` / `revise` / `terminated`。
日志同时写入 `state["logs"]` 和 JSONL 文件，`finalize` 输出 `log_summary`（步骤数、错误数、重试次数、总耗时）。

## 7. 失败重试与降级

- 所有工具经 `tools.call_tool()` 调用，内部 `retry_call()`：**最多 3 次、指数退避（0.5s → 1s）**；
  参数校验错误、写操作拒绝、**确定性失败不重试**（404、证书错误、非 HTML、JS 空页面等）。
- 每类工具都有**时间预算**（`AGENT_SEARCH_TIMEOUT` / `AGENT_FETCH_TIMEOUT` / `AGENT_SUMMARIZE_TIMEOUT`），
  超出预算立即停止重试；`search_web` / `fetch_pages` 节点还有整体预算（40s / 45s），
  抓取阶段最多尝试 5 个 URL，保证一次请求不会无限拖长。
- 搜索提供方有简单熔断：某个源连续失败 2 次，本进程内后续查询直接跳过它（避免 DDG/Wikipedia
  超时把整条链路拖住）。
- 重试耗尽后返回结构化 `ToolResult(ok=False, attempts=3, error=...)`，节点记录 `errors` 与日志，
  然后进入降级路径，不静默失败：
  - `search_web` 全失败 → 无候选 URL，报告写「未找到可靠来源」；
  - `fetch_page` 单页失败 → 跳过该页，继续抓其他页；
  - `summarize_sources` 失败 → 证据为空并记录错误；
  - 写操作失败 → `status=completed_with_action_failure`，最终结果里列出失败动作，可人工重发。
- 抓取带连接/读取超时、2MB 上限、内网地址拦截（SSRF 基础防护）。

### 编码探测与乱码清洗

抓取统一走 `resp.content` 原始字节，不使用 `resp.text`（它依赖服务器声明的 charset，
国内站点经常缺失或写错）。`text_clean.decode_body()` 按优先级探测：
**BOM → 严格 UTF-8 → HTTP 头 / `<meta charset>` / XML 声明 → charset_normalizer（未安装则 chardet）
→ gb18030 → `gbk` + `errors="ignore"` 兜底**；能解码但“可疑字符”过多的候选会被跳过，
避免把 GBK 页面按 latin-1 解出一屏 `åÃ¤`。搜索页（Bing / 360 / DuckDuckGo）也走同一套解码。

入库前再做一次正则过滤：一段文本里 **Latin-1 扩展字符、控制符、□/▪ 等符号** 的占比
超过 `AGENT_MOJIBAKE_RATIO`（默认 30%）就整段丢弃；标题乱码则回退成 URL；整页都乱码时
返回确定性失败（`NonRetryableToolError`，不重试），不会把脏数据送进证据提取。
每页的 `encoding` 与 `dropped_blocks` 会写进抓取日志（如「编码 gb18030，丢弃乱码 2 段」），方便排查。

## 8. 不编造来源

四重约束：

1. **提示词**：只允许使用给定 sources，quote 必须逐字摘录；
2. **证据校验**：`filter_evidence()` 丢弃 URL 不在抓取集合、或 quote 不能逐字匹配正文的“证据”；
3. **报告清洗**：`_sanitize_report()` 出报告前按抓取 URL 白名单清洗——未抓取的链接降级为纯文本、
   「来源清单/References」整段删除后按真实来源重建，并记录 `citation_mismatch`；
4. **无来源兜底**：无证据时强制写入「未找到可靠来源」，且来源清单为空。

### 证据提取：严谨研究助理

`summarize_sources` 的提示词（`prompts.SUMMARIZE_SYSTEM`）按「严谨研究助理」规则执行：

1. **不提取废话**：广告、欢迎语、博主感慨、导航栏文字一律不要；
2. **必须是完整句子**：程序侧 `_is_complete_sentence()` 再校验一次，拒掉以逗号、`and`、`the` 等结尾的片段；
3. **宁缺毋滥**：只介绍概念、没回答具体步骤/方法/细节的来源直接不给证据，全部不相关时 `evidence=[]`
   → 最终报告写「未找到可靠来源」，不允许拿泛泛介绍凑数；
4. **保留代码/API 名**：如 `interrupt` 等标识必须完整保留在 `quote` 里；
5. **必须带出处**：每条证据包含 `source_snippet`（小标题/段落定位）和 `relevance_reason`。

输出仍是 JSON（用 `{"evidence": [...]}` 包一层，便于逐条校验来源），每条含
`evidence_text / quote / source_url / source_snippet / relevance_reason / confidence`。
离线模式由 `heuristic_evidence()` 执行同样的「关键词相关 + 宁缺毋滥」策略。

### 报告结构：大纲 → 分节写作 → 引用校验

针对「摘要太水 / 逻辑割裂 / 引用堆叠 / 缺乏推演」四个实测问题，报告生成拆成三步（`reporting.py`）：

1. **证据卡片化**（`summarize_sources`）：给每条证据补 `sub_question`（归属子问题）、`kind`
   （定义/机制/数据/案例/观点）、`promo`（营销/证言标记），同源同义证据合并并记录 `merged_urls`；
2. **大纲规划**（`plan_outline`，新增节点）：模型输出 `{"sections":[{title, goal, evidence_ids}]}`，
   失败/离线时用规则大纲（摘要 → 按子问题分的内容节 → 分析与推演 → 局限与未证实）；
3. **分节写作**（`draft_report`）：摘要单独用「结论 → 关键依据 → 适用边界」三段式提示词，
   每个内容节只喂本节证据（引用编号仍是全局 `[S#]`），推演单独成节、每条以【推断】开头；
4. **引用校验**（`draft_review`，新增节点）：校验编号合法性、单句 ≤2 个角标、引用与句子相关、
   关键发现条条有引用、摘要非空判断、小节完整；不达标先让模型最小改动重写一次，
   **重写后问题更多就丢弃**，再程序剪枝（保留最相关 2 个角标）与补节兜底。

`draft_review` 的结果写入 `final_output.draft_review`（issues / rewrites / rejected_rewrites /
remaining_issues），前端节点名也做了中文化（"撰写报告"、"引用与结构校验"）。

文末 `## 来源清单` 由程序按证据编号自动重建（`prompts.source_list_lines()`）：同一 URL 的多条证据
合并为一行 `- [标题](URL)（[S1][S2]）`，未被引用的抓取页面也如实列出；模型自己写的来源章节会被
`_strip_source_sections()` 删除，不会出现伪造来源。离线模式 `heuristic_draft()` 生成同样结构
（含【推断】小节），并合并重复结论。

## 9. 测试

```bash
pytest -q
```

覆盖：

- 正常流程：三次中断 → 批准 → 邮件/Notion 各写一次；**每次确认前 outbox 必须为空**；
- 重试：第 3 次成功（`attempts=3`，退避 `[0.5, 1.0]`）；3 次全失败返回结构化错误；
- 写操作门禁：未确认直接拒绝、参数里伪造 `human_approved` 无效、确认后才产生 dry-run 文件；
- 网页版：首页可访问、API 三次中断顺序正确、每次确认前 `outbox` 为空；
- **持久化（P0-1）**：换新图实例（模拟重启）后仍能从草稿中断处 resume；`/api/sessions` 列表与详情；
  重启后 `/api/resume/stream` 不再 400（trace_id 从索引回源）；`parent_thread_id` 注入 `history_context`；
- **调用链/成本（P0-2）**：价目表最长前缀匹配、未知模型回退 env 并标记估算；节点 span 与
  `node_usage` 一致；`summarize` 节点 token 之和等于总量；`/api/trace` 与 `/api/cost/summary` 可用；
- **知识库 RAG（P0-3）**：标题分块与重叠、BM25 召回、无 embedding 时降级、`kb://` 来源经
  `_sanitize_report` 保留且不做成点击链接、图级流程里报告出现「📚 知识库」来源；
- 模型设置：Key 保存后不落盘、不回传明文、只显示掩码；测试连接失败不泄露 Key；
  清除运行时配置后回到 `.env` / 离线模式；设置面板与深色主题存在于 `index.html`；
- 证据完整性：编造 URL/改写引文被过滤、报告 URL 白名单清洗；无来源时报告显式声明；
- 报告模板：摘要 2~3 句、关键发现逐条带 `[S#]`、同义证据合并、固定三段式结构、来源清单按证据编号重建；
- 安全：内网 IP、解析到内网的域名、非 http(s) URL 一律拒绝抓取；
- 编码：无 charset 声明 / 声明写错时仍能解出中文；乱码段落被丢弃；整页乱码确定性失败；
- 抓取清洗：广告 / 导航 / Cookie / 脚本被删除，重复段落只保留一次；
- 可观测：`finalize` 的 token 增量、成本公式、耗时、JSONL 日志字段完整；
- 失败降级：模型报错时证据提取/报告生成回退到启发式，规划失败时降级为单查询；
- 修改/终止分支、拒绝写操作分支、搜索全失败降级；
- **P0 质量闭环**：子问题覆盖度只把“命中主题实体与语义块”算覆盖；缺口补检受最大轮次约束；
  claim→quote 校验会丢弃明显无关的“证据”，但保留同义改写；图级测试验证补检轮会合并新页面。
- **报告质量闭环**：证据卡片化（子问题/类型/去重/营销标记）、规则大纲与 LLM 大纲校验、
  引用硬校验（编号越界/单句堆叠/引用错配/摘要空判断/关键发现无引用）、程序修复（剪枝、补节）、
  “重写后更差就丢弃”的回归保护、两个消融开关（`AGENT_EVIDENCE_CARDS`、`AGENT_DRAFT_REVIEW`）。

当前：**149 passed**。

## 10. 验收对照

| 验收标准                     | 实现位置                                                                                    |
| ------------------------ | --------------------------------------------------------------------------------------- |
| 跑通完整流程                   | `main.py` + `tests/test_graph_flow.py::test_full_flow_writes_only_after_both_approvals` |
| 草稿确认前不写邮件/Notion         | `graph.py` 条件边；测试断言每次确认前 `outbox` 为空                                                    |
| 三处人工暂停（草稿 / 邮件 / Notion） | `graph.py` 三个 `interrupt()`；`human_review.py` 三个确认函数                                    |
| 未确认写操作直接拒绝               | `tools.call_tool()` 写操作门禁 + `WriteActionNotApproved`                                    |
| 失败重试 3 次并记录日志            | `tools.retry_call()`；测试断言退避与重试次数                                                        |
| 日志按 step_id 串起链路         | `logging_utils.py` + JSONL + 日志表                                                        |
| 结论带可验证引用              | 报告用 `[S#]` 角标；`_sanitize_report()` 按证据重建来源清单并清洗未抓取 URL                        |
| 问题拆解 3~5 个子问题           | `plan_research` → `plan.sub_questions`；日志 S002 记录子问题数                                      |
| 子问题覆盖 / 缺口补检（P0）       | `check_coverage` + `quality.compute_coverage`；覆盖率/轮次写入 `final_output`                     |
| claim 支持性校验（P0）         | `quality.verify_evidence`；支持率写入 `final_output.verification`                              |
| 可观测（token/成本/耗时）        | `final_output` 含 `token_usage / cost_cny / duration_ms / log_summary`                       |
| 40 题评测 + 报告             | `evaluation/`：`run_eval.py` + `make_report.py` → `开发文档/eval_report.md`               |
| 9 项功能闭环验收             | `evaluation/acceptance_check.py` → [开发文档/FUNCTIONAL_ACCEPTANCE.md](开发文档/FUNCTIONAL_ACCEPTANCE.md) |
| 会话持久化 / 重启恢复（P0-1）     | `persistence.py` + `web.py`：SQLite checkpointer；`tests/test_persistence.py` |
| 调用链 Trace / 节点成本（P0-2）  | `tracing.py` + `graph._instrument`；`/api/trace`、`/api/cost/summary`、`config/pricing.json` |
| 知识库混合检索（P0-3）         | `rag.py` + `retrieve_knowledge`；`knowledge/`、`evaluation/kb_check.py`（6/6） |

## 11. 已知限制（MVP 边界）

- 持久化到 SQLite 单文件（`data/checkpoints.sqlite`）：单进程可用，多 worker / 多实例部署需要换
  `PostgresSaver`；会话索引表只存元数据，正文仍在 checkpoint 与 JSONL 里。
- 没有鉴权与限流，默认只监听 `127.0.0.1`，不要暴露到公网；多用户隔离仅靠 `thread_id`。
- 未配置 SMTP/Notion 时写操作是 dry-run（落盘模拟），不会真正发送/同步。
- 搜索按 Bing / 360 / DuckDuckGo / Wikipedia 回退，可能被限流或区域不可达；已做提供方回退与重试。
- 编码探测是启发式的：极短页面、或正文本身在源头就是乱码（例如被上游转码坏掉的镜像站）无法还原，
  这类页面会被整段丢弃或抓取失败，而不是“猜”出正常内容。
- P0 的「子问题覆盖率」是代理指标：真实评测 51.2%，主要卡在对比题只抓到一方、站点 403/JS 空页；
  补检只能缓解，不能替代“按实体拆分检索 / 官方搜索 API / PDF 解析”。
- P0 的「证据断言支持率」是规则版校验（bigram、数字、英文实体、否定词一致性），
  不是语义蕴含判断；只用于拦截明显外推，不能替代人工抽检。
- 补检轮会提高延迟：真实评测平均 15.6s、P95 26.7s（平均 1.68 轮）；下一步用并发抓取与缓存控制。
- 知识库向量用纯 Python 余弦计算，规模上限约 1 万 chunk；再大应换 FAISS/Chroma 或专用向量库。
  embedding 需要服务提供 `/embeddings`（DeepSeek 不提供，可换智谱 `embedding-3` 或纯 BM25）。
- 价目表是手写的，模型调价后需要更新 `config/pricing.json`；未录入的模型按 env 单价估算并标记。
- `glm-4-flash` 等免费模型成本真实为 0，不是“没统计到”。
- 只面向“研究问题 → 带来源报告 → 交付”这一条窄链路，不做通用工具编排。

## 12. 评测（40 题测试集）

`evaluation/dataset.jsonl` 收录 40 道题（简单事实 / 对比分析 / 时效性 / 多跳推理各 10 题），
`evaluation/run_eval.py` 逐题跑完整图并统计验收指标，`evaluation/make_report.py` 生成
**[开发文档/eval_report.md](开发文档/eval_report.md)**（测试集说明、指标定义、当前结果、失败案例、消融对比）。

```bash
# 本地确定性 fixture：验证流水线、引用、去重、延迟（快）
python evaluation/run_eval.py --mode mock --tag mock_current
python evaluation/run_eval.py --mode mock --tag mock_p0
# 消融基线：关闭本轮新增的引用可点击化与问题拆解
python evaluation/run_eval.py --mode mock --tag mock_baseline --no-link-citations --no-sub-questions
# 真实联网：检索质量、成功率与延迟（慢，受网络影响）
python evaluation/run_eval.py --mode real --tag real_current
python evaluation/run_eval.py --mode real --tag real_p0
# 真实联网 + 真实大模型（读取 .env；先跑 check_llm.py）
python evaluation/run_eval.py --mode real --llm openai --tag real_llm --limit 10
python evaluation/make_report.py
# 功能闭环验收（9 项逐条检查，生成 开发文档/FUNCTIONAL_ACCEPTANCE.md）
python evaluation/acceptance_check.py
```

指标口径：任务成功率、引用可追溯率、关键结论可点击来源率、工具调用正确率、预期要点覆盖率、
证据逐字可回溯率、平均/P95 延迟、单任务成本、来源去重率、子问题拆解达标率；
P0 追加：子问题证据覆盖率、证据断言支持率、平均检索轮次。
没有 `OPENAI_API_KEY` 时 LLM 走离线启发式，token/成本为 0，报告中会明确标注这一口径。

最近一次真实联网评测（40 题，`real_p0`，离线 LLM + 真实检索）：任务成功率 **85.0%**、
引用可追溯/可点击 **100%**、工具调用正确率 **90.0%**、多跳类 **70.0%**、
子问题覆盖率 **51.2%**、断言支持率 **99.4%**、平均 **1.68 轮**、P95 **26.7s**。
相比 `real_after_360`：成功率 +2.5pp、多跳 +10pp、事实覆盖 +5pp；覆盖率仍未达到 75% 目标，
主要受“对比题只抓到一方 / 站点 403/JS 空页”限制，见 [开发文档/OPTIMIZATION.md](开发文档/OPTIMIZATION.md) 第 9 节。
