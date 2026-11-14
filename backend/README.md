# backend/（后端代码索引）

> 本次整理遵循「不做任何代码改动」：后端 Python 模块**仍保持在仓库根目录**，未物理搬入本目录。
> 原因：项目是平铺模块（`from graph import ...`、`from state import ...`）且 `web.py` 用
> `BASE_DIR / "index.html"` 定位前端文件；搬进 `backend/` 必须改 import 路径、`sys.path`/包结构、
> 文件路径与全部测试，属于代码改动。需要真正拆分时，按下面清单移动并同步修改引用即可。

## 后端模块清单（当前位置：仓库根目录）

| 文件 | 行数（约） | 职责 |
| --- | --- | --- |
| [`graph.py`](../graph.py) | 1718 | LangGraph 图：14 节点、条件边、3 处 `interrupt`、`_instrument` 节点 Trace、报告清洗 |
| [`state.py`](../state.py) | 172 | `ResearchState` TypedDict 与初始状态 |
| [`llm_client.py`](../llm_client.py) | 1063 | 模型客户端：Function Calling → JSON → 离线启发式三级降级、embedding、usage |
| [`prompts.py`](../prompts.py) | 408 | 全部提示词（规划/证据/大纲/分节/摘要/重写） |
| [`tools.py`](../tools.py) | 1133 | 5 个工具、重试/退避/熔断、SSRF 防护、写操作门禁 |
| [`quality.py`](../quality.py) | 361 | 子问题覆盖度、缺口补检、claim→quote 支持校验 |
| [`reporting.py`](../reporting.py) | 687 | 证据卡片化、大纲、引用硬校验与程序修复 |
| [`rag.py`](../rag.py) | 423 | 知识库分块/BM25/可选向量/RRF/索引缓存 |
| [`tracing.py`](../tracing.py) | 266 | 节点 Span、价目表成本归因 |
| [`text_clean.py`](../text_clean.py) | 240 | 网页编码探测、乱码/广告/导航清洗 |
| [`logging_utils.py`](../logging_utils.py) | 201 | 结构化日志 + JSONL + 实时日志 sink |
| [`persistence.py`](../persistence.py) | 223 | SQLite checkpointer + 会话索引（删除/中断清理） |
| [`human_review.py`](../human_review.py) | 147 | CLI 人工确认（草稿/邮件/Notion） |
| [`main.py`](../main.py) | 94 | CLI 入口（interrupt → resume 循环） |
| [`web.py`](../web.py) | 686 | FastAPI 入口：页面路由、NDJSON 流、会话/删除/中断、Trace/成本、模型配置 |
| [`check_llm.py`](../check_llm.py) | 266 | 真实模型配置自检脚本 |

## 附属目录

| 目录 | 作用 |
| --- | --- |
| [`config/`](../config/) | 模型价目表 `pricing.json` |
| [`knowledge/`](../knowledge/) | 本地知识库语料（3 篇 Markdown） |
| [`data/`](../data/) | 运行期 SQLite（checkpoints/sessions）与 KB 索引（gitignore） |
| [`evaluation/`](../evaluation/) | 40 题评测集、跑分/验收脚本、mock fixture |
| [`tests/`](../tests/) | 23 个测试文件 / 166 个用例 |

## 若要物理拆分到本目录

1. `git mv` 上述 `.py` 到 `backend/`（`config/`、`knowledge/`、`evaluation/`、`tests/` 按需一起搬）；
2. 把根目录改成包（加 `backend/__init__.py`），或保留根目录并把 `backend` 加入 `sys.path`；
3. 统一改 import：`from graph import ...` → `from backend.graph import ...`；
4. 改 `web.py` 里的 `BASE_DIR` 相对路径（`index.html`、`launch.html`、`.env`、`requirements.txt`）；
5. 改 `start.bat` 的启动入口与 `pytest.ini`；
6. 全量 `pytest -q` + 启动 `python web.py` 回归。
