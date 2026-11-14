# 原型图（界面截图）

本目录存放项目全部页面原型/界面截图，共 15 张。所有截图都来自真实运行的页面
（Chrome headless / CDP 驱动），不是设计稿。

| 文件 | 页面 | 说明 |
| --- | --- | --- |
| [web-ui.png](web-ui.png) | 主界面 | 研究问题输入、执行进度、历史会话入口 |
| [web-ui-dark.png](web-ui-dark.png) | 主界面（深色主题） | 深色科技风主题下的完整页面 |
| [web-ui-demo.png](web-ui-demo.png) | 静态演示模式 | `file://` 双击打开、无后端时的示例数据流程 |
| [web-ui-launch.png](web-ui-launch.png) | 一键启用启动页 | 后端/模型状态探测 + 一键进入主界面 |
| [web-ui-launch-settings.png](web-ui-launch-settings.png) | 启动页 → 模型设置 | 一键启用后自动弹出模型设置弹层 |
| [web-ui-progress.png](web-ui-progress.png) | 执行进度（展开） | 14 段节点进度条 + 阶段表情/文案 + 日志表 |
| [web-ui-progress-collapsed.png](web-ui-progress-collapsed.png) | 执行进度（收起日志） | 只折叠日志表格，进度条与阶段文案保留 |
| [web-ui-history.png](web-ui-history.png) | 历史会话抽屉 | 会话列表、状态、继续审批入口 |
| [web-ui-history-delete.png](web-ui-history-delete.png) | 历史会话删除 | 行内删除按钮、已中断状态标签 |
| [web-ui-interrupted.png](web-ui-interrupted.png) | 执行中断态 | 后端被杀后进度冻结标红 + 「会话未完成」卡片 |
| [web-ui-recover.png](web-ui-recover.png) | 断点恢复 | 服务重启后从待审批中断处继续 |
| [web-ui-trace.png](web-ui-trace.png) | 调用链 Trace | 节点耗时瀑布 + 每节点 token/成本 |
| [web-ui-rag.png](web-ui-rag.png) | 知识库来源 | 知识库证据与统一 `[S#]` 引用 |
| [web-ui-settings.png](web-ui-settings.png) | 模型设置面板 | Key/Base URL/模型/提供方预设、测试连接 |
| [web-ui-final-meta.png](web-ui-final-meta.png) | 最终结果元信息 | 长状态文本中文标签化，卡片不溢出 |

> 说明：本目录的前身是仓库根目录的 `docs/`；为区分“页面原型”与“开发文档”，
> 截图统一移到 `原型图/`，`docs/` 仅保留一个指向本目录的索引。
