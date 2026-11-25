# 开发文档

本目录集中存放“开发/工程类”文档，面向开发者与面试官；产品介绍与使用说明仍在根目录
[`README.md`](../README.md)，项目快照在 [`PROJECT_SUMMARY.md`](../PROJECT_SUMMARY.md)。

## 文档索引

| 文件 | 内容 | 维护方式 |
| --- | --- | --- |
| [接口文档.md](接口文档.md) | 全部 HTTP/NDJSON 接口：页面路由、流式协议、研究/会话/Trace/配置接口、人工确认载荷、示例 | 手写，随接口变更同步 |
| [前端契约.md](前端契约.md) | 视觉重做/外部交付契约：页面路由与模式、接口与 NDJSON 事件、测试强制的 DOM id/JS 函数/文案/样式字面量清单、深链与验收 | 手写，随前端与测试变更同步 |
| [视觉重做需求.md](视觉重做需求.md) | 前端视觉改版 brief：目标与范围、页面清单与状态、交互规则、设计 token、响应式/无障碍/打印、交付与验收、可直接复制的 Prompt | 手写 |
| [数据库设计文档.md](数据库设计文档.md) | 存储总览、`sessions.sqlite`、`checkpoints.sqlite`（LangGraph）、`kb_index.json`、JSONL 日志/trace、生命周期 | 手写，随 schema 变更同步 |
| [质量与可靠性设计.md](质量与可靠性设计.md) | 结构化日志字段、重试/降级、编码清洗、防编造四重约束、证据提取与报告分节、消融开关 | 手写（原 README §6–8） |
| [测试与验收.md](测试与验收.md) | 测试分层表、公共夹具、9 项功能验收对照、当前 300 用例/9-9/6-6 | 手写（原 README §9–10） |
| [评测方法.md](评测方法.md) | 40 题数据集、mock/real/real+llm 三种模式、指标定义、最新结果与口径限制 | 手写（原 README §12） |
| [OPTIMIZATION.md](OPTIMIZATION.md) | 优化路线图 + 历次改造实施记录（P0、P0-1/2/3、报告质量、UI 等） | 手写 |
| [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) | 早期独立验收报告（历史存档，保留原始数字） | 存档 |
| [FUNCTIONAL_ACCEPTANCE.md](FUNCTIONAL_ACCEPTANCE.md) | 9 项功能闭环验收报告 | 由 `evaluation/acceptance_check.py` 生成（见下） |
| [eval_report.md](eval_report.md) | 40 题评测报告（指标、失败案例、消融、P0 对比） | 由 `evaluation/make_report.py` 生成（见下） |

## 关于“自动生成”的两个文件

这两个文件原本由脚本直接写到**原路径**（根目录 / `evaluation/`），本次整理只移动了文档、
**没有改任何代码**，因此：

- 重新运行 `python evaluation/acceptance_check.py` 会在**仓库根目录**重新生成
  `FUNCTIONAL_ACCEPTANCE.md`；
- 重新运行 `python evaluation/make_report.py` 会在 **`evaluation/`** 下重新生成 `eval_report.md`。

本目录里保留的是整理时的快照（内容一致）。若希望生成物直接落在 `开发文档/`，
需要改两个脚本的输出路径（属于代码改动，本次未做）。

## 相关目录

| 目录 | 内容 |
| --- | --- |
| [`../原型图/`](../原型图/) | 页面原型/界面截图（原 `docs/`） |
| [`../evaluation/`](../evaluation/) | 评测集、跑分/验收脚本、mock fixture |
| [`../tests/`](../tests/) | 46 个测试文件 / 300 个用例 |
| [`../backend/`](../backend/) | 后端代码索引（代码仍在根目录） |
| [`../frontend/`](../frontend/) | 前端代码索引（代码仍在根目录） |
