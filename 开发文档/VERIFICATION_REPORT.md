# 独立验收报告：LangGraph + Function Calling 研究助手 MVP

- **审计对象**：`D:\Build\AI-hy`（作者已完成实现，本次审计未修改任何源码/测试）
- **审计角色**：独立验收者（不采信作者/README 结论，全部以源码与实测证据为准）
- **审计时间**：初检 2026-11-04 21:50 – 22:02；**复审 F1–F6 修复 22:03 – 22:12**；**最终确认 R1–R5 修复 22:13 – 22:16**（Asia/Shanghai）
- **审计手段**：逐文件源码核对 → pytest（初检 23 / 复审 29 / 最终确认 30 项）→ 真实网络端到端 dry-run demo → stdin 管道交互路径 → 定向对抗探针（门禁绕过/状态注入/重试/无来源/写失败/修订/终止/伪造 URL/SSRF/日志落盘失败）
- **产物隔离**：所有验证产物写入 `.tmp\verify-*`、`.tmp\reverify-*`、`.tmp\final-*`（独立 outbox/logs），项目根 `outbox\`、`logs\` 为 Lead/作者先前产物，本次未写入

---

## 一、总体结论

**初检：有条件通过（CONDITIONAL PASS）；复审后：通过（PASS）。**

初检时 14 条验收条目中 **12 项 PASS、2 项 PARTIAL、0 项 FAIL**。核心安全边界全部成立：

- 写操作门禁不可被参数伪造或状态注入绕过；人工确认前**未产生任何写入**；
- interrupt/checkpoint 恢复**不丢状态、不重复日志**，`step_id` 连续可串联；
- 重试恰为 3 次（0.5s → 1.0s 指数退避），失败降级并记录；
- 离线（无 Key，默认路径）报告中的 URL 均可回溯到实际抓取结果；无来源时明确写「未找到可靠来源」。

初检遗留的 2 个 PARTIAL（F1 伪造 URL 残留、F2 三处暂停与两处 interrupt 不一致）已由作者修复。**复审实测确认**（详见 §八）：F1–F6 全部修复，原 2 项 PARTIAL 转为 PASS，**14/14 验收条目通过**；新增的 3 处人工暂停顺序/分支、URL 白名单清洗、`log_summary.total == len(logs)`、发邮件延迟、SSRF 校验、落盘告警均验证成立。仅剩 **1 项低风险边缘绕过 R1**（URL scheme 大小写/非 http(s) 不被清洗，需模型输出非典型 URL 才触发）及 2 项文档/边缘提示，不阻塞验收。

**最终确认（详见 §九）**：作者已修复 R1/R2/R4/R5 与 R3 文档；本轮复验 pytest **30 passed**、demo 16 条日志/`日志步骤=16`/三处 interrupt，大写 HTTPS 与 ftp 伪造 URL 已从 `final.report` 清除并记录 `citation_mismatch`，重定向在内网目标**请求发出前**即被拒绝（仅 1 次外网请求），来源章节 h1–h6/加粗/冒号/CRLF 变体均可整段删除重建，人工 `edited_report` 同样被清洗。**最终结论：通过（PASS）**。仍存在的唯一残余为 R1'（低）：中文紧邻裸 URL 时 `\b` 词边界导致漏匹配（建议用 `(?<![A-Za-z0-9])` 替代），不阻塞验收。

---

## 二、验收环境与方法说明

### 2.1 环境

| 组件 | 版本/状态 |
| --- | --- |
| Python | 3.14.5（`.venv`） |
| langgraph / checkpoint | 1.2.19 / 4.3.1 |
| openai SDK | 3.45.0（未配置 Key，运行于 offline 模式） |
| pytest | 9.2.0 |
| `.env` | 不存在（仅 `.env.example`），未配置 OPENAI/SMTP/NOTION，符合审计要求 |

### 2.2 pytest 环境问题（**非项目缺陷**，需记录）

按题述环境说明先用 `--basetemp` 仍失败：

```
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp="D:\Build\AI-hy\.tmp\verify-bd57f680"
→ 10 passed, 13 errors / PermissionError: [WinError 5] 拒绝访问。: 'D:\Build\AI-hy\.tmp\verify-bd57f680'
→ pytest sessionfinish 的 cleanup_dead_symlinks 处再次 PermissionError，进程退出码 1
```

**根因定位（独立探针）**：pytest 9.2.0 用 `Path.mkdir(mode=0o700)` 创建 basetemp / 每个用例的 tmp 目录；在本机 DSH Windows `workspace-write` 沙箱下，`mkdir(mode=0o700)` 产生的目录对任何进程（包括创建者）都拒绝列出/写入，而 `mkdir()` 默认 mode 的目录正常。证据：

```
python -c: pathlib.Path(r'...\.tmp\mode-probe').mkdir(mode=0o700); (p/'child0').mkdir()
  → PermissionError WinError 5
python -c: p.mkdir(); (p/'sub').mkdir()  → OK
```

因此这是 **pytest 9 + Python 3.14 + Windows 沙箱** 的环境组合问题，与被测项目无关；项目测试本身在可用临时目录下可正常执行。

本会话为受限 delegated 会话，审批被禁用，无法按题述再申请 `danger-full-access`。改用**不侵入项目的 runner 级 workaround**（不修改任何文件）：

```powershell
.\.venv\Scripts\python.exe -c @"
import pathlib, _pytest.tmpdir as _t
_orig = pathlib.Path.mkdir
def _mkdir(self, mode=0o777, parents=False, exist_ok=False):
    return _orig(self, 0o777, parents=parents, exist_ok=exist_ok)
pathlib.Path.mkdir = _mkdir
_t.cleanup_dead_symlinks = lambda root: None
import pytest, sys
sys.exit(pytest.main(['-q','-p','no:cacheprovider','--basetemp',r'D:\Build\AI-hy\.tmp\verify-run4']))
"@
```

结果：**23 passed, 13 warnings, 0 failed, 0.55s**（13 条 warning 全部是第三方 `langsmith` 的 `asyncio.iscoroutinefunction` DeprecationWarning）。与 Lead 给出的 23 passed 基准一致。

### 2.3 证据文件

| 证据 | 路径 |
| --- | --- |
| 全套 pytest 结果 | 本文 §5.1（runner workaround 输出） |
| 端到端 demo 日志（15 条） | `.tmp\verify-demo-v1\logs\research-8c7308f2.jsonl` |
| demo dry-run 产物 | `.tmp\verify-demo-v1\outbox\emails\*.eml`、`.tmp\verify-demo-v1\outbox\notion\*.md` |
| 分阶段（确认前不写）探针 | `.tmp\verify-staged-v1\logs\research-staged01.jsonl` |
| 交互路径（管道 stdin） | `.tmp\verify-interactive-v1\logs\research-39c9abf7.jsonl` |
| 无来源/写失败/修订/门禁探针 | `.tmp\verify-edge-v1` … `verify-edge-v7` |

---

## 三、逐条验收对照（PASS / PARTIAL / FAIL）

### 3.1 八条边界要求

| # | 边界要求 | 结论 | 证据（源码位置 + 实测） |
| --- | --- | --- | --- |
| B1 | 窄场景流程：`receive_question → plan_research → search_web → fetch_pages → summarize_sources → draft_report → human_review_draft → human_confirm_actions → execute_actions → finalize` | **PASS** | `graph.py:631-660`（10 节点注册/连边）；demo 日志 S001–S015 覆盖率 10/10 节点；分阶段探针 13 条日志含全部 10 个 `node_name` |
| B2 | 5 个工具：`search_web / fetch_page / summarize_sources / send_email / save_to_notion` | **PASS** | `tools.py:652-658`（注册表）、`tools.py:750-869`（5 份 JSON Schema，含 required/additionalProperties）；`tests/test_write_guard.py:115-128` 断言工具集与 `WRITE_TOOLS` |
| B3 | 写操作必须 `human_approved=True`，且门禁不可绕过 | **PASS** | `tools.py:41,54-60,661-677`（门禁 + 剥离参数里的 `human_approved`）、`tools.py:727-746`；全仓库 `human_approved=True` 仅出现于 `graph.py:534`（已确认动作）与工具 helper；实测：无确认 `rejected=True/attempts=0`、参数夹带 `human_approved:True` 仍拒绝、公共 helper 抛 `WriteActionNotApproved`、初始 state 注入 `approved_actions/email_payload` 经两处 interrupt 后最终 `actions=[]`、outbox 不存在 |
| B4 | **三处**人工暂停（草稿后、发邮件前、存 Notion 前），用 `interrupt`/checkpoint 实现 | **PASS（复审后；初检 PARTIAL）** | 初检时仅 2 个 `interrupt()` 调用点（原 F2）。复审确认已拆为 **3 个 `interrupt()`**：`graph.py:423`（草稿）、`graph.py:496`（发邮件前）、`graph.py:521`（存 Notion 前）；`human_review.py:82-147` 三个确认函数；实测顺序 `draft_review → email_confirmation → notion_confirmation`，每次暂停后 outbox 均为空，邮件与 Notion 可分别同意/拒绝。详见 §8.2 |
| B5 | 结构化日志 10 字段：`step_id, node_name, tool_name, input_summary, output_summary, status, retry_count, latency_ms, error, timestamp` | **PASS** | `logging_utils.py:20-32`（LOG_FIELDS）、`logging_utils.py:79-91`（逐条生成，另含 `trace_id` 串联字段）、`state.py:17-31`；demo 15/15 条日志字段齐全；分阶段/交互/无来源探针 100% 字段齐全 |
| B6 | 工具最多重试 3 次、指数退避（0.5/1.0）、失败降级并记录 | **PASS** | `tools.py:35-36`（默认 3 次、基数 0.5s）、`tools.py:108-143`（`retry_call`：第 n 次失败后退避 `base*2^(n-1)`，参数错误/写拒绝不重试，耗尽抛 `ToolExecutionError(attempts=3)`）；实测 `retry_call`：`attempts=3, calls=3, sleeps=[0.5,1.0]`；无来源探针 3 条 `search_web` 日志 `retry_count=2`（=3 次尝试）且 `status=error`；写失败探针 `attempts=3 / retry_count=2` 且最终 `status=completed_with_action_failure` |
| B7 | 不得编造来源：事实必须对应抓取到的 URL；无来源必须写「未找到可靠来源」 | **PASS（复审后；初检 PARTIAL）** | 初检时 OpenAI 模式存在伪造 URL 残留（原 F1）。复审确认新增 `graph._sanitize_report()`（`graph.py:321-372`）：URL 白名单清洗 + 模型来源清单删除并按真实 sources 重建 + 无证据强制声明（`graph.py:358-359`、`graph.py:388-396`）。graph 级 M2 复现：`M2_FINAL_HAS_FAKE_URL=False`、真实来源清单 1 份、`citation_mismatch` 记录在案、`completed_no_sources`；repo 测试 `test_openai_mode_fabricated_urls_are_sanitized` PASS。剩余低风险边缘绕过 R1（URL scheme 大小写/非 http(s)），见 §8.3 |
| B8 | 代码可运行、配置走 `.env`、不硬编码密钥 | **PASS** | `main.py:15,40`（`load_dotenv()`）；`.env.example:1-53` 覆盖 LLM/搜索/重试/SMTP/Notion/日志全部配置；密钥扫描（`sk-*`/`secret_*`/`ntn_*`/BEGIN PRIVATE KEY/硬编码 password）在全部源码/README/example 中 0 命中；demo 未配置任何密钥即完整跑通（exit 0）；离线模式无需 Key（`llm_client.py:83-94`） |

### 3.2 六条验收标准

| # | 验收标准 | 结论 | 证据 |
| --- | --- | --- | --- |
| A1 | 完整流程可端到端跑通（两次中断→恢复→执行写操作→finalize） | **PASS** | demo：`status=completed`、S011/S012 两次 `interrupt` 均 `approved`、S013/S014 执行两项写操作、S015 finalize；`tests/test_graph_flow.py:53-125` |
| A2 | 测试覆盖正常流程 | **PASS** | `tests/test_graph_flow.py` 6 个用例（完整流程/拒绝写操作/终止/修订/搜索全失败/重试计数）；`tests/conftest.py:16-22,27-47,60-93` 强制 offline + 假检索/抓取，测试无网络 |
| A3 | 测试覆盖工具失败重试 | **PASS** | `tests/test_tools_retry.py` 4 个用例，其中 `:10-29` 断言 `attempts=3, sleeps=[0.5,1.0]`，`:32-51` 断言耗尽后结构化失败；`tests/test_graph_flow.py:217-248` 断言图内 `retry_count` 落日志 |
| A4 | 测试覆盖未确认写操作被拒绝 | **PASS** | `tests/test_write_guard.py` 8 个用例：直接拒绝、参数伪造 `human_approved` 无效、确认前不产生文件、确认后才产生 dry-run 文件、非法收件人 |
| A5 | 日志按 `step_id` 串起全链路；interrupt/resume 不丢状态、不重复日志 | **PASS** | demo：15 条日志 `S001…S015` 连续无重复，JSONL 15 行 = state logs 15 条；分阶段探针：两次恢复后 13 条 `S001…S013` 连续唯一，JSONL 13 行 = logs 13 条，恢复后 `research_question/sources/evidence` 状态完整；修订分支 16 条 `S001…S016` 连续唯一；无重复节点日志（interrupt 节点在恢复时重入但 `StepLog` 从 checkpoint 计数继续，未重复） |
| A6 | 端到端 dry-run demo：报告来源可回溯；outbox 文件只在人工确认后出现 | **PASS** | demo：报告 3 个 URL == S006–S008 实际抓取 URL；outbox 两文件 mtime `21:53:18.745` 晚于 S012（人工确认完成）`21:53:09.221`，且运行前 outbox 不存在；分阶段探针：第一次 interrupt 后 outbox=False、第二次 interrupt 后仍=False、仅在批准后生成 `outbox\emails\*.eml` |

### 3.3 测试覆盖清单（初检 23 项；复审 29 项全部通过）

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_graph_flow.py` | 6 | 完整流程/确认前零写入/拒绝写操作/终止/修订/搜索全失败降级/图内重试计数 |
| `tests/test_tools_retry.py` | 4 | 第 3 次成功、3 次耗尽、参数错误不重试、未知工具 |
| `tests/test_write_guard.py` | 8 | 写门禁 6 种绕过/拒绝路径 + Schema 完整性 |
| `tests/test_evidence_integrity.py` | 5 | 编造 URL/改写引文过滤、无来源声明、真实 URL 引用、离线摘录工具 |
| **合计** | **23** | 全部通过 |

> 复审新增：`test_graph_flow.py` +1（stub-LLM 报告清洗）、`test_tools_safety.py` 3（SSRF）、`test_evidence_integrity.py` +2（`_sanitize_report`），合计 **29 passed**（§8.1）。

---

## 四、发现的缺陷与风险

> 严重级别：严重 / 高 / 中 / 低 / 提示。以下为**初检**发现；F1–F6 已在复审中确认修复（每项末尾附「复审结论」），复审新发现的边缘项见 §8.3。

### F1【中 · 复审已修复 PASS】OpenAI 模式下伪造 URL 可残留进最终报告，仅记录错误不清理

- **位置**：`graph.py:321-341`（`_ensure_report_integrity`）、`llm_client.py:236-258`（openai 草稿路径）、`prompts.py:62-68`（仅提示词约束）。
- **问题**：
  1. `URL_RE` 检出的“未抓取 URL”只被追加进 `errors`（`graph.py:337-341`），**不删除/替换**，最终 `final_output["report"]` 仍携带伪造链接；
  2. `graph.py:330` 仅在草稿完全不含「## 来源清单」时才补真实来源清单——若模型自带的「来源清单」里全是伪造 URL，真实来源清单不会被补写、该章节被伪来源“占位”；
  3. `filter_evidence()` 只约束 `summarize_sources` 抽出的 evidence，约束不到 `draft_report` 自由文本里新写入的 URL。
- **影响**：验收标准「不得编造来源：事实必须对应抓取到的 URL」在 OpenAI 模式（本项目的目标真实模式）下无法由程序保证，只能靠提示词与事后错误记录。
- **复现步骤**：
  1. 用 stub LLM 替换 `llm_client.set_llm()`（`mode="openai"`）：`summarize_evidence` 返回 1 条真实 evidence（quote 逐字来自抓取正文），`draft_report` 返回草稿，正文写 `[额外来源](https://fake.example/not-fetched)` 且自带「## 来源清单」含该伪造 URL；
  2. 用假 `search_web/fetch_page` 跑 `build_graph().invoke(...)` → 草稿 interrupt → approve → decline 写操作；
  3. 实测输出：`M2_FAKE_URL_PRESENT=True`、`M2_DECLARED_NO_SOURCE=False`、`M2_FINAL_HAS_FAKE_URL=True`、`citation_mismatch` 错误存在但报告未清洗。
- **建议**：出报告前对最终 Markdown 做 URL 白名单清洗（未抓取 URL 剔除或替换为「未验证链接」）；「## 来源清单」章节无条件按真实 `sources` 重建；把 citation_mismatch 升级为可配置的阻断项。
- **说明**：离线默认模式（无 Key）由 `heuristic_draft` 模板生成，实测无法触发该问题；因此该缺陷不影响默认 demo 的验收结论，但影响「OpenAI 真实模式」的合规性。
- **复审结论（已修复 PASS）**：`_sanitize_report()` 已落地（`graph.py:321-372`，在 `draft_report:388` 调用）。graph 级 M2 复现（原始场景，evidence=0）：`fake.example` 不在 `final.report`、`未找到可靠来源` 存在、真实来源清单 1 份、`citation_mismatch` 记录；evidence>0 场景：MD 伪造链接与裸 URL 均被移除、真实 URL 保留、email 正文同样已清洗。仅有边缘绕过 R1（§8.3）。

### F2【低 · 复审已修复 PASS】需求「三处人工暂停」，实现为 2 个 `interrupt()` 调用点

- **位置**：`graph.py:390`（草稿审核）、`graph.py:463`（写操作确认）；`human_review.py:100-129`（同一次暂停内先后询问邮件、Notion）；`README.md:30-33`（自述两处）。
- **问题**：邮件确认与 Notion 确认合并在 `human_confirm_actions` 一个 `interrupt()` 内；从 checkpoint 视角只有 2 个暂停点，与需求「草稿后、发邮件前、存 Notion 前」三处字面不一致（尽管两项写都确实在人工确认后执行）。
- **复现**：`Select-String graph.py 'decision = interrupt'` → 仅 390、463 两行。
- **建议**：由需求方确认「合并确认」可接受；若必须三处，则把写操作确认拆为 `confirm_email` / `confirm_notion` 两个独立节点/中断（并各自写 checkpoint）。
- **复审结论（已修复 PASS）**：已拆为 3 个 `interrupt()`（`graph.py:423 / 496 / 521`），`human_review.py` 拆成 `review_draft / confirm_email / confirm_notion`（`:82-147`）并在 `build_resume` 分派（`:138-147`）；确认日志拆为两条（`graph.py:547-566`，实测 S012/S013）。四种分支矩阵（同意/拒绝 × 邮件/Notion）、每次暂停后 outbox 为空、无重复日志均实测通过（§8.2/§8.4）。

### F3【低 · 复审已修复 PASS】dry-run 发邮件延迟 5–9.5s：`make_msgid()` 触发阻塞 DNS 查询

- **位置**：`tools.py:462`（SMTP 路径）、`tools.py:489`（outbox 路径）。
- **问题**：`email.utils.make_msgid()` 不传 `domain` 时会调用 `socket.getfqdn()` 做 DNS 查询；本机单次约 4.5–5s，导致 dry-run 发邮件 `latency_ms` 被放大（demo S013=9523ms，交互路径 S013=9593ms）。
- **实测证据**：`socket.getfqdn()`=4950ms；`make_msgid()`=4531ms；`make_msgid(domain="research-assistant.local")`=0ms。
- **复现**：`python -c "import socket,time;from email.utils import make_msgid;t=time.perf_counter();make_msgid();print('ms',int((time.perf_counter()-t)*1000),socket.getfqdn())"` → 约 4500–5000ms；对照 demo S013 `latency_ms=9523` 且 `.eml` mtime 比 S012 晚约 9.5s。
- **影响**：功能正确（文件内容/收件人无误），仅性能与日志耗时失真，容易被误判为网络问题。
- **建议**：`make_msgid(domain=os.getenv("SMTP_FROM_DOMAIN", "research-assistant.local"))`，或缓存 FQDN。
- **复审结论（已修复 PASS）**：`tools.py:496`、`tools.py:523` 已固定 `domain="research-assistant.local"`。复审 demo S014 `send_email latency_ms=33`、交互 A `32ms`、独立探针 `1ms`（原 9.5s）；`.eml` 正常生成。

### F4【低 · 复审已修复 PASS】`final_output.log_summary.total` 比实际日志条数少 1（finalize 自身日志未计入）

- **位置**：`graph.py:602`（`log_stats` 在 finalize 追加自身日志之前计算）vs `graph.py:619-625`（finalize 的 `sl.add`）。
- **问题**：`log_summary.total` 统计的是进入 finalize 时的日志数，不含 finalize 自己那条。
- **实测**：demo JSONL 共 15 行，但 finalize 日志 `output_summary` 打印「日志步骤=14」；交互路径 14 行 vs「日志步骤=13」。测试仅断言 `total >= 10`（`tests/test_graph_flow.py:114`），未覆盖该偏差。
- **复现**：对比 `.tmp\verify-demo-v1\logs\research-8c7308f2.jsonl` 行数（15）与最后一行 S015 的 `output_summary`（「日志步骤=14」）；交互证据 `.tmp\verify-interactive-v1\logs\research-39c9abf7.jsonl` 为 14 vs 13。
- **建议**：finalize 先记录自身日志再统计，或把字段改名为 `total_before_finalize`。
- **复审结论（已修复 PASS）**：`graph.py:664-670` 先 `sl.add` 自身日志，`graph.py:686` 用 `log_stats(sl.logs)` 统计；复审 demo 日志 16 条、finalize 打印「日志步骤=16」、`final_output.log_summary.total == len(logs)`（staged 四种分支实测 True）；repo 测试已改为 `assert final["log_summary"]["total"] == len(logs)`（`tests/test_graph_flow.py:121`）并 PASS。

### F5【低/安全加固 · 复审已修复 PASS（残留 R2）】URL 安全校验只拦字面内网 IP，DNS 解析/重定向未复检

- **位置**：`tools.py:168-183`（`_assert_safe_url`）、`tools.py:352-361`（`requests.get` 默认跟随重定向）。
- **问题**：`host` 仅做 `ipaddress` 字面判断；域名解析到 `127.0.0.1`/私网，或公网 URL 通过 302 跳转到内网，均不会被拦截。另外抓取仅校验初始 URL。
- **复现（已实测检查函数行为）**：`_assert_safe_url("http://127.0.0.1/x")` → 拒绝；`_assert_safe_url("http://localhost/x")` → 拒绝；但 `_assert_safe_url("http://localtest.me/x")` → **放行**（该公网域名常被用于解析到 127.0.0.1，说明“非字面 IP”会直接通过）；此外 `requests.get` 默认跟随重定向、跳转后的内网地址不再经过校验。
- **影响**：MVP 演示场景无风险；作为可复用工具存在 SSRF 攻击面。
- **建议**：`allow_redirects=False` 后逐跳校验，或解析后校验所有 A/AAAA 记录。
- **复审结论（已修复 PASS，残留 R2）**：新增 `_assert_public_ip` / `_resolve_ips` / DNS 校验（`tools.py:169-214`）与重定向 hop 复检（`tools.py:395-396`），`tests/test_tools_safety.py` 3 项 PASS。复审实测：DNS 解析到 `10.0.0.7` → 拒绝且 `attempts=0`（不可重试）；`127.0.0.1` / `::1` / `localhost` / `file://` 拒绝；`localtest.me` 解析到 127.0.0.1 被拒绝；伪造 302 内网响应被拒（`attempts=0`）。**残留 R2（低）**：`fetch` 未显式 `allow_redirects=False`，上述重定向校验发生在 `requests` 已跟随重定向之后（内网请求已发出、响应体未被读取/返回）；建议后续改为手动逐跳。

### F6【低 · 复审已修复 PASS】JSONL 日志落盘失败被静默吞掉

- **位置**：`logging_utils.py:101-111`（`_persist` 捕获 `OSError` 后 `pass`）。
- **问题**：磁盘满/权限异常时 JSONL 可能缺行，无任何告警；state 内日志仍完整，但离线按 `step_id` 回放会缺条目。
- **复现**：将 `AGENT_LOG_DIR` 指向不可写目录（或 mock `Path.open` 抛 `OSError`）跑一次流程 → 终端日志照常、`run_log_path` 不存在/缺行，既无 stderr 告警也无 errors 记录。
- **建议**：至少 `print(..., file=sys.stderr)` 或在 final_output.errors 记录 `log_persist_failed`。
- **复审结论（已修复 PASS）**：`logging_utils.py:102-112` 捕获 `OSError` 后输出 `[WARN] 结构化日志落盘失败（路径）：异常` 到 stderr；复审探针（`run_log_path` 指向不可创建路径）实测 stderr 捕获到该告警，流程不中断。

### 提示级观察（非缺陷）

- **O1**：`AGENT_AUTO_APPROVE=1` / `--auto` 会跳过交互并自动批准写操作（`main.py:54`、`human_review.py:96-98`），是 README 明确标注的演示/CI 开关；它走的是“自动确认”而非绕过工具门禁（工具仍收到 `human_approved=True`）。生产环境不得开启。
- **O2**：Function Calling 仅用于 `plan_research` 生成 `search_web` 调用（`llm_client.py:136-177`）；其余 4 个工具由节点确定性调用。对本需求是合理且更安全的设计，但若预期“LLM 自主编排全部工具”，需扩展。
- **O3**：默认离线模式下即使成功生成报告，`draft_report` 日志 `status="degraded"`（`graph.py:367` 仅 `mode=="openai"` 时给 ok）；`finalize` 汇总的 `degraded` 计数会偏高，监控口径需知情。
- **O4**：写操作也走 3 次重试；若 SMTP 已投递但客户端超时，存在重复发信风险（低概率）。建议写操作使用幂等键或禁用自动重试。

---

## 五、测试与 demo 实际输出摘要（初检；复审更新见 §八）

### 5.1 pytest（初检 23 passed；复审 29 passed）

```
23 passed, 13 warnings in 0.55s
（13 条 warning 均为第三方 langsmith 的 asyncio.iscoroutinefunction DeprecationWarning）
```

- 题述的 `-q -p no:cacheprovider --basetemp=...` 在本机沙箱下因临时目录 ACL 问题产生 13 errors（全部是 `tmp_path` fixture 创建目录失败，非断言失败），详见 §2.2；runner workaround 后 23/23 通过，与 Lead 基准一致。**复审为 29 passed（§8.1）**。
- 断言结果抽查：`attempts=3`、退避 `[0.5, 1.0]`、耗尽错误文本含「重试 3 次后仍失败」、未确认写操作 `rejected=True` 且 outbox 不存在、确认前 outbox 为空、完整流程两次中断与两文件写入。

### 5.2 端到端 demo（dry-run，真实 Bing 搜索 + 真实抓取，无任何密钥）

命令（隔离目录）：

```powershell
$env:PYTHONIOENCODING='utf-8'; $env:DEFAULT_EMAIL_TO='verify@example.com'; $env:AGENT_AUTO_APPROVE='1'
$env:AGENT_OUTBOX_DIR='D:\Build\AI-hy\.tmp\verify-demo-v1\outbox'
.\.venv\Scripts\python.exe main.py "LangGraph 如何实现人工中断（human-in-the-loop）？" --auto --log-dir "D:\Build\AI-hy\.tmp\verify-demo-v1\logs"
```

结果：**exit 0**，`trace_id=8c7308f2`，起止 21:53:01→21:53:18，`status=completed`，来源 3 / 证据 6 / 日志 15 条。

```
S001 receive_question  ok
S002 plan_research     ok        3 条查询 provider=offline-heuristic
S003-S005 search_web   ok        Bing 返回 5/5/5 条（各 ~1s）
S006-S008 fetch_pages  ok        https://langgraph.com.cn/index.html (1232 字)
                                https://www.langchain.com/langgraph (2976 字)
                                https://www.runoob.com/ai-agent/langgraph-quick-start.html (3623 字)
S009 summarize_sources ok        6 条证据 mode=offline
S010 draft_report      degraded  2907 字符草稿（离线模板）
S011 human_review_draft  approved  ↑ 人工中断①（--auto 批准）
S012 human_confirm_actions approved ↑ 人工中断②（--auto 批准 send_email + save_to_notion）
S013 execute_actions send_email    ok  mode=dry_run  latency_ms=9523（DNS 原因，见 F3）
S014 execute_actions save_to_notion ok mode=dry_run latency_ms=1
S015 finalize          ok        status=completed
```

- 报告中的 3 个来源 URL 与 S006–S008 抓取 URL **完全一致**（无未抓取 URL）。
- `outbox` 运行前不存在；生成文件：
  - `outbox\emails\...-verify@example.com.eml`（5937 字节，含完整报告与收件人）
  - `outbox\notion\...-研究报告：LangGraph-如何实现人工中断（human-in-the-loop）？.md`（4266 字节，含 front-matter 与报告）
  - 两文件 mtime 均为 `21:53:18.745`，晚于 S012 时间戳 `21:53:09.221` → 写入发生在人工确认之后。
- JSONL 15 行 = state logs 15 条；`step_id` 连续；每条 10 字段齐全。

### 5.3 分阶段探针（确认前零写入 + interrupt 状态/日志一致性）

用真实 `build_graph()` + 假检索/抓取（不联网），在两次 interrupt 处逐步检查：

```
第一次 interrupt: type=draft_review   | outbox 存在? False
  payload.sources = [alpha, beta]；草稿含两个真实抓取 URL
第二次 interrupt: type=action_confirmation | outbox 存在? False
批准 email=True / notion=False 后：
  final.status=completed；actions=[('send_email', True, 'dry_run')]
  outbox 仅 emails\*.eml
  logs: S001–S013 连续、唯一；JSONL 13 行 == state logs 13 条
  final.sources = [alpha, beta]；final.report 中 URL 100% ⊆ 已抓取 URL
  research_question 等状态在两次恢复后保持完整
```

### 5.4 交互路径（stdin 管道，非 --auto）

```
stdin: "1" (草稿通过) / "y" (发邮件) / "changed@example.com" (改收件人) / "Custom Subject" (改主题) / "n" (不存 Notion)
```

结果：**exit 0**，`status=completed`；仅生成 1 封邮件且收件人为 `changed@example.com`、主题 `Custom Subject`；未生成 `outbox\notion`；无写操作越权。说明交互确认分支与就地修改参数均正常。

### 5.5 对抗/边界探针

| 探针 | 结果 |
| --- | --- |
| 无确认 `call_tool("send_email", ...)` | `ok=False, rejected=True, attempts=0`，outbox 不存在 |
| 参数夹带 `human_approved: True` | 仍 `rejected=True`（被 `payload.pop` 剥离） |
| 公共 helper `send_email(...)` 无确认 | 抛 `WriteActionNotApproved` |
| 初始 state 注入 `approved_actions/email_payload` | 两处 interrupt 后最终 `actions=[]`、outbox 不存在（确认节点覆盖） |
| `retry_call` 合约 | `attempts=3, calls=3, sleeps=[0.5, 1.0]`；参数错误不重试（attempts=0） |
| 搜索全失败（真实 graph 路径） | 3 条 `search_web` 日志 `retry_count=2`、`status=error`；草稿写明「未找到可靠来源」且 0 个 URL；`final.status=completed_no_sources`；错误记录 3 条 |
| 写操作失败（monkeypatch 写盘抛 OSError） | `attempts=3 / retry_count=2`；`final.status=completed_with_action_failure`；errors 含 `write_failed` |
| 修订分支（revise ×2，第 3 次触达 `MAX_REVISIONS=2`） | 第 1/2 次回到 draft_review 且 revision_count=1/2；第 3 次按通过处理并记录 `revision_limit` 错误；16 条日志 `S001–S016` 连续唯一 |
| 终止分支 | `status=terminated_by_user`、`actions=[]`、无 outbox |
| `edited_report` 人工改稿 | 确认写操作时使用的是人工编辑后的草稿（正文以「# 人工改写的报告」开头） |
| 未知 interrupt 类型 | `build_resume` 抛 `ValueError` |

---

## 六、未能验证的部分（受环境/权限限制）

1. **真实 OpenAI Function Calling 路径**：未配置 `OPENAI_API_KEY`（审计要求不得配置），仅通过 stub LLM 验证草稿/证据分支逻辑；`openai` 模式下的真实网络请求、模型输出质量、真实工具调用稳定性未验证。
2. **真实 SMTP 发信 / 真实 Notion API 写入**：按要求只跑 dry-run；SMTP 认证、TLS、Notion API 字段映射未实测。
3. **DuckDuckGo / Wikipedia 搜索提供方回退**：Bing 可用；DDG/Wikipedia 在本机可能超时（题述预期）。提供方回退逻辑仅代码审查（`tools.py:191-217,223-346`），未实测“Bing 全失败→DDG→Wikipedia”全链路。
4. **跨进程/重启的 checkpoint 恢复**：实现为 `InMemorySaver`（`graph.py:661`，README:214-216 已声明限制），进程退出后中断态丢失；未验证 `SqliteSaver/PostgresSaver` 替换方案。
5. **真实 TTY 交互**（非管道）：使用 stdin 管道验证；终端逐行输入/EOF 异常路径未在真实控制台验证。
6. **长时间/并发运行**、多 `thread_id` 并发、日志文件并发写：未验证。
7. **Windows 沙箱下原生 `pytest -q`**：受环境权限限制无法原样通过（见 §2.2），已用 runner workaround 得到等价测试结论；未申请 `danger-full-access`（本会话审批被禁用）。

---

## 七、初检结论与建议（历史，复审后结论已更新为 §八）

- **核心安全与流程边界（B1/B2/B3/B5/B6/B8 + A1–A6）全部 PASS**：写操作门禁、确认前零写入、重试/退避、失败降级、日志链路、来源可回溯（离线模式）、run/interrupt/resume 状态一致性均经实测成立。
- **初检遗留的 2 个 PARTIAL 项（F1/F2）**：已由作者修复；复审确认转为 PASS，见 §八。
- **低风险建议（F3–F6）**：均已修复并由复审确认（§八）。
- **复审最终结论**：**通过（PASS）**；剩余 R1（URL 大小写/非 http(s) scheme 边缘绕过）与 R2（重定向校验时序）为低风险后续加固项，不阻塞验收。

---

## 八、复审记录：F1–F6 修复验证（2026-11-04 22:03 – 22:12）

> 复审基于作者修复后的最新代码（最终一轮 pytest / demo / 探针均在 `graph.py` 最后一次变更 22:07:03 之后执行；复审期间未修改任何源码/测试）。

### 8.1 复审方法与结果

- 重读：`graph.py`、`human_review.py`、`tools.py`、`logging_utils.py`、`README.md`、`tests/test_graph_flow.py`、`tests/test_evidence_integrity.py`、`tests/test_tools_safety.py`。
- pytest（同一 runner workaround，见 §2.2）：**29 passed, 13 warnings, 0 failed, 0.57s**（`--collect-only` 亦为 29）；改动相关 3 个文件 verbose **17 passed**（`test_graph_flow` 7 + `test_tools_safety` 3 + `test_evidence_integrity` 7）。
- 复审证据：`.tmp\reverify-demo-v1\logs\research-f4a22848.jsonl`、`.tmp\reverify-interactive-A|B\logs\*.jsonl`、`.tmp\reverify-v1|v2|v3\`（定向探针）、`.tmp\reverify-pytest3`。

### 8.2 F1–F6 状态表（复审）

| 项 | 修复内容（当前代码） | 复审实测（证据） | 结论 |
| --- | --- | --- | --- |
| **F1** 伪造 URL | `graph._sanitize_report()`（`graph.py:321-372`）在 `draft_report:388` 调用：删除模型自带来源清单（`SOURCE_SECTION_RE`）、未知 Markdown 链接/裸 URL 替换为「（未验证链接已移除）」、无证据强制「未找到可靠来源」、按真实 sources 重建「## 来源清单」、差异记 `citation_mismatch`（`graph.py:389-396`） | graph 级 M2 原始场景（evidence=0）：`M2_FINAL_HAS_FAKE_URL=False`、`未找到可靠来源=True`、真实来源清单 1 份、`citation_mismatch` 在 errors、status `completed_no_sources`；evidence>0 场景：MD 伪造链接 False、裸 URL False、真实 URL True、来源清单 count=1、email 正文/评审草稿均已清洗；repo 测试 `test_graph_flow.py:308-329` PASS | **PASS**（边缘 R1） |
| **F2** 三处暂停 | `human_confirm_actions` 内两次独立 `interrupt()`：`graph.py:496`（`email_confirmation`）、`graph.py:521`（`notion_confirmation`）；草稿审核 `graph.py:423`；`human_review.py:82-135` 拆出 `confirm_email / confirm_notion`，`build_resume:138-147` 分派 | 实测顺序 `draft_review → email_confirmation → notion_confirmation`；四种分支（邮件/Notion 同意×同意、同意×拒绝、拒绝×同意、拒绝×拒绝）actions 与 outbox 全部正确；每次 interrupt 后 outbox 均为空；确认日志拆为 S012/S013 两条（approved/skipped 组合正确）；无重复日志、`step_id` 连续唯一 | **PASS** |
| **F3** 发邮件延迟 | `make_msgid(domain="research-assistant.local")`（`tools.py:496,523`） | demo S014 `latency_ms=33`、交互 A `32ms`、独立探针 `1ms`（修复前 9523/9593ms）；`.eml` 正常 | **PASS** |
| **F4** total 计数 | finalize 先 `sl.add` 自身日志（`graph.py:665-670`），再 `log_stats(sl.logs)` 写入 `log_summary`（`graph.py:686`） | demo 日志 16 条、finalize 打印「日志步骤=16」；staged 四种分支 `total == len(logs)` 均 True（15/15、14/14）；repo 测试改为 `total == len(logs)`（`test_graph_flow.py:121`）并 PASS | **PASS** |
| **F5** SSRF | 新增 `_assert_public_ip / _resolve_ips` 与 DNS 校验（`tools.py:169-214`），重定向 hop 复检（`tools.py:395-396`） | DNS 解析到 `10.0.0.7` → 拒绝且 `attempts=0`；`127.0.0.1`/`::1`/`localhost`/`file://` 拒绝；`localtest.me`（解析 127.0.0.1）拒绝；伪造 302 内网响应被拒（attempts=0）；`tests/test_tools_safety.py` 3 项 PASS | **PASS**（残留 R2） |
| **F6** 落盘告警 | `logging_utils.py:102-112` 捕获 `OSError` 后写 stderr `[WARN] 结构化日志落盘失败（…）：…` | 探针令 `run_log_path` 不可创建 → stderr 捕获到 `[WARN]`，流程继续；state 日志仍完整 | **PASS** |

### 8.3 复审新发现的边缘问题（均不阻塞验收）

- **R1【低】URL 清洗的大小写 / 协议边缘绕过**：`URL_RE`、`MD_LINK_RE`（`graph.py:32-33`）未加 `re.I`，且只覆盖 `https?://`。实测：`HTTPS://fake.example/upper`、`[伪造](HTTPS://fake.example/z)`、`ftp://fake.example/y` 不会被清洗；graph 级 M2 变体最终 `final.report` **保留** `HTTPS://fake.example/upper` 且无 `citation_mismatch`（`R1_UPPER_FAKE_IN_FINAL=True`）。建议两个正则加 `re.I`，并用更宽松的 `[a-z][a-z0-9+.-]*://` 扫描 + 非白名单 scheme 提示；或出报告前统一规范化大小写。
- **R2【低】F5 重定向校验时序**：`requests.get` 未显式 `allow_redirects=False`（`tools.py:383-396`），内网重定向校验发生在 `requests` 已跟随重定向之后——SSRF 请求已发出，但响应体在 `_assert_safe_url` 抛错前未被读取/返回。建议手动逐跳校验。
- **R3【提示/文档】**：`graph.py:1` 顶部 docstring 仍写「两处人工中断」（同文件 `:9-10` 已更新为三次）；`tests/test_graph_flow.py:1` docstring 仍写「两次人工中断」。建议同步。
- **R4【提示】来源清单正则变体**：`SOURCE_SECTION_RE` 只匹配 `#{1,3}` 且要求标题后直接换行、无冒号/加粗/CRLF；实测 `#### 来源清单`、`## 来源清单：`、`## **来源清单**`、CRLF 等变体不会删除旧章节，会在重建的真实清单前留下重复标题（伪造 URL 仍被 `URL_RE` 清除，不产生伪造来源）。建议放宽为 `#{1,6}` + 可选 `[：:]` + 兼容 `\r?$`。
- **R5【提示】** 人工 `edited_report`（`human_review_draft` 的 `updates["draft_report"]`）绕过 `_sanitize_report`；人工编辑属显式输入，但若要求“任何路径都不得出现未抓取 URL”，建议审核后也执行一次清洗。
- **R6【提示】** `human_confirm_actions` 的两条确认日志都在两次确认完成后才写入（`graph.py:547-566`），因此 email 确认日志时间戳晚于真实确认时刻、两条几乎同时；字段/链路完整，不影响验收。

### 8.4 复审实测摘要

- **pytest**：29 passed（0.57s），与作者声明一致；改动相关 17 项全 PASS。
- **端到端 demo**（isolated outbox，真实 Bing + 抓取，offline 无 Key）：`trace_id=f4a22848`，S001–S016 共 16 条日志；三处中断 S011（草稿）/S012（邮件）/S013（Notion）；S014 send_email `ok 33ms`、S015 save_to_notion `ok 1ms`；S016 finalize「日志步骤=16」；JSONL 16 行；outbox 两文件 mtime `22:07:56.630/.631` 晚于 S013 `22:07:56.596`；报告 3 个 URL 全部来自 S006–S008 抓取、来源清单 1 份、无「未验证链接已移除」。
- **交互路径（stdin 管道）**：A = 通过草稿 / 同意邮件并改收件人 `changed@example.com`、主题 `Custom Subject` / 拒绝 Notion → 仅 1 个 `.eml`，S012 approved、S013 skipped，15 条日志、`total=15`；B = 拒绝邮件 / 同意 Notion 并改标题 `Edited Notion Title`、标签 `tag1, tag2` → 仅 1 个 `.md`，S012 skipped、S013 approved。
- **分支矩阵**（staged，假工具）：T/T → 两个动作两个文件、15 条日志；T/F → 仅 email、14 条；F/T → 仅 notion、14 条；F/F → 无动作无文件、14 条。四种均满足：每处 interrupt 后 outbox 为空、`step_id` 连续唯一、JSONL==logs、`total==len(logs)`。
- **回归探针**：无来源三处暂停正常（`completed_no_sources`、报告声明、0 URL）；写失败 `attempts=3` → `completed_with_action_failure`（Notion 仍成功）；同意邮件但收件人为空 → `missing_email_to` 跳过、Notion 正常；注入 `approved_actions` 仍无法绕过（`actions=[]`、outbox 空）；修订一轮后三处暂停、16 条日志连续唯一。
- **M2 伪造 URL**：原始场景（evidence=0）与 evidence>0 场景均确认 fake URL 不再出现在 `final.report`/email 正文，真实来源清单仅 1 份；仅 R1 的大小写/非 http(s) 边缘变体仍可绕过。

### 8.5 复审结论

**F1–F6 全部修复并实测确认，原 2 个 PARTIAL 已消除，整体由「有条件通过」更新为「通过（PASS）」。** 复审未发现由拆分引入的功能性问题（拒绝邮件后 Notion 分支、拒绝 Notion 后邮件分支、重复日志、状态丢失、`step_id` 断链均已覆盖且通过）。建议后续处理 R1/R2 两个低风险加固项与 R3–R6 提示项，不阻塞本次验收。

> **更新（22:16）**：R1/R2/R4/R5 与 R3 文档已由作者修复，最终确认结果见 §九。

---

## 九、最终确认：R1–R5 修复验证（2026-11-04 22:13 – 22:16）

> 作者在复审后于 22:12:53–22:13:06 修改 `graph.py`、`tools.py`、`tests/test_evidence_integrity.py`、`tests/test_graph_flow.py`；本轮所有 pytest/demo/探针均在此之后执行，未修改任何源码/测试。

### 9.1 结论

**最终确认通过（PASS）。** R1/R2/R4/R5 的目标修复全部经实测确认，R3 文档已同步；pytest **30 passed**、demo 16 条日志/`日志步骤=16`/三处 interrupt 均与作者声明一致。仅发现 1 个**低风险新增边缘 R1'**（中文紧邻裸 URL 时 `\b` 词边界导致漏匹配），不阻塞验收，建议下一轮一并修掉。

### 9.2 逐项确认

| 项 | 修复（当前代码） | 本轮实测 | 结论 |
| --- | --- | --- | --- |
| **R1** URL 大小写/ftp | `URL_RE = (?i)\b(?:https?\|ftp)://…`、`MD_LINK_RE = (?i)\[…\]\(…\)`（`graph.py:33-34`）；新增测试 `test_sanitize_report_handles_uppercase_scheme_and_heading_variants`（`tests/test_evidence_integrity.py:114-125`） | 直测 `HTTPS://fake.example/upper`、`HtTpS://…`、`ftp://…` 均被清除并记入 dropped；graph 级 M2（stub LLM）：`final.report` 无 fake URL、来源清单 1 份、`citation_mismatch` 记录 `HTTPS://fake.example/upper, ftp://fake.example/file`；repo 新测试 PASS | **PASS**（残留 R1'） |
| **R2** 重定向请求前校验 | `_fetch_with_safe_redirects()`（`tools.py:380-402`）：`allow_redirects=False`，每跳先 `_assert_safe_url(current)` 再 `requests.get`，`urljoin` + 最多 4 跳；`_fetch_page_impl:405-408` 使用 | 伪造 302 → `http://127.0.0.1/secret`：**只发出 1 次请求**（`calls=[('https://example.com/start', False)]`），未向内网发请求，`ok=False / attempts=0`，错误「拒绝访问内网/保留地址：127.0.0.1」；合法 302→`/second` 可正确跟随并成功（2 次请求均 `allow_redirects=False`，`attempts=1`），说明未因禁用自动重定向产生抓取回归 | **PASS** |
| **R4** 来源章节变体 | `_strip_source_sections()`（`graph.py:41-55`）+ `SOURCE_TITLES`/`_HEADING_RE`（`:35-38`）：逐行解析，支持 h1–h6、加粗、全角/半角冒号、CRLF | 直测 `#### 参考来源：`+CRLF、`# 来源清单`、`**Sources**` 均整段删除；重建后「## 来源清单」仅 1 份、正常 `## 正文` 保留；fake URL 全部清除 | **PASS** |
| **R5** edited_report 清洗 | `human_review_draft` 对 `edited_report` 调用 `_sanitize_report` 并记录 `citation_mismatch`（`graph.py:476-492`） | 人工稿含 `HTTPS://fake.example/upper` 与 `ftp://fake2.example/f`：`final.report`/email 正文均无 fake、真实 URL 保留、errors 出现 `human_review_draft/citation_mismatch` | **PASS** |
| **R3** 文档同步 | `graph.py:1` 已改为「三处人工中断」；`tests/test_graph_flow.py:1` 已改为「三次人工中断」 | 与实现（3 个 `interrupt()`）一致 | **PASS** |

### 9.3 本轮新发现（低风险，不阻塞）

- **R1'【低】中文紧邻裸 URL 漏匹配**：`URL_RE` 使用 `\b` 词边界（`graph.py:33`），而中文字符属 Unicode 词字符——当裸 URL 紧跟中文且无空格时（如 `编造结论见HTTPS://fake.example/upper`），`\b` 不成立导致漏清洗。graph 级实测：`final.report` 保留该 URL 且无 `citation_mismatch`（`R1PRIME_CJK_FAKE_IN_FINAL=True`）；Markdown 链接形式不受影响（`MD_LINK_RE` 以 `[` 起始）。建议把 `\b` 改为 `(?<![A-Za-z0-9])`（或直接去掉 `\b`）。
- 其余 R4 边缘（如标题带说明文字“## 来源清单（共 N 条）”）不在本轮范围；如需要可进一步放宽 `_HEADING_RE` 的标题匹配。

### 9.4 本轮运行摘要

- **pytest**：`30 passed, 13 warnings in 0.62s`（30 项 = graph_flow 7 + tools_retry 4 + write_guard 8 + evidence_integrity 8 + tools_safety 3）；新测试 `test_sanitize_report_handles_uppercase_scheme_and_heading_variants` PASS。
- **端到端 demo**（isolated outbox，真实 Bing + 抓取，offline）：`trace_id=e21ea00f`，S001–S016 共 16 条日志；三处 interrupt S011（草稿）/S012（邮件）/S013（Notion）；S014 send_email `ok 37ms`、S015 notion `ok 1ms`；S016「日志步骤=16」；JSONL 16 行；outbox 两文件 mtime `22:14:59.820` 晚于 S013 `22:14:59.782`；报告 3 个 URL 全部来自 S006–S008 抓取、`## 来源清单` 1 份、无「未验证链接已移除」。
- **证据路径**：`.tmp\final-demo-v1\logs\research-e21ea00f.jsonl`、`.tmp\final-v1\`（R1/R2/R4/R5 探针）、`.tmp\final-v2\`（R1'/合法重定向探针）、`.tmp\final-pytest1`。

### 9.5 最终结论

**通过（PASS）**：R1/R2/R4/R5 修复目标全部达成，R3 文档同步，pytest 30 / demo 16 与作者声明一致；复审与初检发现的 F1–F6 及 R1–R5 均已闭环。唯一残余 R1'（低）为中文相邻裸 URL 的正则边界边缘，不影响默认 demo 与常规 LLM Markdown 输出路径，建议下一轮用 `(?<![A-Za-z0-9])` 收紧 `URL_RE` 后即可完全闭合。

---

## 附录：审计约束遵守说明

- 仅修改本 `VERIFICATION_REPORT.md`；所有 demo/探针产物位于 `.tmp\verify-*`、`.tmp\reverify-*`、`.tmp\final-*`（隔离 outbox/logs/临时目录），未修改任何源码或测试文件。初检阶段源文件 mtime 均早于 21:50；复审源码由作者在 22:03–22:07 修改、最终确认源码由作者在 22:12:53–22:13:06 修改（均非本审计者）。临时探针脚本仅位于 `.tmp\reverify_probe*.py`、`.tmp\final_probe*.py`。
- 项目根 `outbox\`、`logs\` 中 21:49–21:51 的文件为 Lead/作者先前运行的产物，本次审计未写入；审计中所有运行均通过 `AGENT_OUTBOX_DIR` / `--log-dir` 指向隔离目录。
