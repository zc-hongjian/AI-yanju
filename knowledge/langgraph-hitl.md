# LangGraph 人工中断（human-in-the-loop）

## interrupt 与 resume

LangGraph 通过 `interrupt()` 在节点内部暂停图执行，并把控制权交还给调用方。
调用方拿到 `__interrupt__` 载荷后，可以用 `Command(resume=value)` 恢复；恢复时
被中断的节点会**从头重新执行**，`interrupt()` 这次直接返回 resume 传入的值。

因此有一条硬性约束：**不要把副作用放在 `interrupt()` 之前**。发邮件、写数据库这类
操作必须放在独立节点里，或在中断之后执行，否则恢复时可能重复执行。

## 为什么必须配 checkpointer

中断状态存在于 checkpoint 中。编译图时如果没有挂 checkpointer，`interrupt()` 会直接报错。
每个会话用 `thread_id` 隔离，同一个 `thread_id` 才能恢复。

本项目使用 SQLite checkpointer（`data/checkpoints.sqlite`）：服务重启后仍能从
草稿审核 / 邮件确认 / Notion 确认三处暂停点继续。会话索引另存在 `data/sessions.sqlite`，
用于历史会话列表与"继续审批"。

## 三种人工确认

研究助手在三个位置暂停：

1. 草稿审核：通过 / 修改（重新生成，最多 2 轮）/ 终止；
2. 发邮件前：展示收件人、主题、正文，可改可跳过；
3. 保存 Notion 前：展示标题、标签、正文，可改可跳过。

写操作工具要求 `human_approved=True` 才会执行；未确认时抛 `WriteActionNotApproved`。
