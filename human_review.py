"""人工确认逻辑：草稿审核 + 写操作参数确认（发邮件、存 Notion 各一处）。

三处人工暂停分别对应 graph.py 里的三个 `interrupt()` 调用点：
1) review_draft        —— 生成报告草稿后（通过 / 修改 / 终止）；
2) confirm_email       —— 调用 send_email 前（展示收件人、主题、正文）；
3) confirm_notion      —— 调用 save_to_notion 前（展示标题、内容、标签）。

人工输入会作为 LangGraph `Command(resume=...)` 的值回传给对应 interrupt。

非交互环境（CI / 演示）可以用 --auto 或 AGENT_AUTO_APPROVE=1 自动批准，
但自动批准只影响“是否同意”，不会绕过工具层的 human_approved 校验。
"""

from __future__ import annotations

from typing import Any

BAR = "=" * 72


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError as exc:  # 非交互环境
        raise RuntimeError(
            "当前环境没有标准输入，无法进行人工确认；请使用 --auto 或设置 AGENT_AUTO_APPROVE=1"
        ) from exc


def _yes_no(prompt: str) -> bool:
    while True:
        answer = _ask(prompt).strip().lower()
        if answer in {"y", "yes", "是", "确认"}:
            return True
        if answer in {"", "n", "no", "否", "取消"}:
            return False
        print("请输入 y 或 n。")


def _print_preview(text: str, limit: int = 24) -> None:
    lines = str(text or "").splitlines()
    for line in lines[:limit]:
        print("      " + line)
    if len(lines) > limit:
        print(f"      ……（还有 {len(lines) - limit} 行）")


def review_draft(payload: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
    """展示草稿，返回 {"decision": approve|revise|terminate, "comment": str}。"""
    question = str(payload.get("research_question") or "")
    draft = str(payload.get("draft_report") or "")
    sources = payload.get("sources") or []
    print(BAR)
    print("人工审核：研究报告草稿")
    print(BAR)
    print(f"研究问题：{question}")
    print(f"来源数：{len(sources)}    证据数：{payload.get('evidence_count', 0)}    已修改轮次：{payload.get('revision_count', 0)}")
    print("-" * 72)
    _print_preview(draft, limit=120)
    print("-" * 72)
    if auto:
        print("[自动模式] 直接通过草稿。")
        return {"decision": "approve", "comment": "automatic approval (--auto)"}
    while True:
        choice = _ask(
            "请选择：[1] 通过并继续  [2] 修改意见（重新生成草稿）  [3] 终止流程\n> "
        ).strip().lower()
        if choice in {"1", "y", "yes", "通过", "approve"}:
            return {"decision": "approve", "comment": ""}
        if choice in {"2", "revise", "修改"}:
            comment = _ask("请输入修改意见（不能为空）：").strip()
            if not comment:
                print("修改意见不能为空。")
                continue
            return {"decision": "revise", "comment": comment}
        if choice in {"3", "q", "terminate", "终止"}:
            print("已终止：不会发邮件，也不会写 Notion。")
            return {"decision": "terminate", "comment": "用户主动终止"}
        print("无效输入，请输入 1 / 2 / 3。")


def confirm_email(payload: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
    """人工暂停 ②：发送邮件前。返回 {"send": bool, "email_payload": {...}|None}。"""
    email = dict(payload.get("email") or {})
    print(BAR)
    print("人工确认 ②：发送邮件")
    print(BAR)
    print(f"收件人：{email.get('to') or '（未配置 DEFAULT_EMAIL_TO，需要手动填写）'}")
    print(f"主题：{email.get('subject', '')}")
    print("正文预览：")
    _print_preview(email.get("body", ""), limit=20)
    if auto:
        print("[自动模式] 同意发送邮件。")
        return {"send": True, "email_payload": email}
    if not _yes_no("是否发送这封邮件？[y/N] "):
        print("已取消发送邮件。")
        return {"send": False, "email_payload": None}
    edited = dict(email)
    new_to = _ask(f"收件人 [{email.get('to') or ''}]（回车保持不变）：").strip()
    if new_to:
        edited["to"] = new_to
    new_subject = _ask(f"主题 [{email.get('subject') or ''}]（回车保持不变）：").strip()
    if new_subject:
        edited["subject"] = new_subject
    if not str(edited.get("to") or "").strip():
        print("警告：收件人为空，该动作将被跳过（收件人必须人工填写）。")
    return {"send": True, "email_payload": edited}


def confirm_notion(payload: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
    """人工暂停 ③：保存到 Notion 前。返回 {"save": bool, "notion_payload": {...}|None}。"""
    notion = dict(payload.get("notion") or {})
    print(BAR)
    print("人工确认 ③：保存到 Notion")
    print(BAR)
    print(f"标题：{notion.get('title', '')}")
    print(f"标签：{', '.join(notion.get('tags') or []) or '（无）'}")
    print("内容预览：")
    _print_preview(notion.get("content", ""), limit=20)
    if auto:
        print("[自动模式] 同意保存到 Notion。")
        return {"save": True, "notion_payload": notion}
    if not _yes_no("是否保存到 Notion？[y/N] "):
        print("已取消保存 Notion。")
        return {"save": False, "notion_payload": None}
    edited = dict(notion)
    new_title = _ask(f"标题 [{notion.get('title') or ''}]（回车保持不变）：").strip()
    if new_title:
        edited["title"] = new_title
    new_tags = _ask(
        f"标签（逗号分隔）[{', '.join(notion.get('tags') or [])}]（回车保持不变）："
    ).strip()
    if new_tags:
        edited["tags"] = [tag.strip() for tag in new_tags.split(",") if tag.strip()]
    return {"save": True, "notion_payload": edited}


def build_resume(payload: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
    """把 interrupt 的 payload 分派到对应的人工确认流程。"""
    kind = str((payload or {}).get("type") or "")
    if kind == "draft_review":
        return review_draft(payload, auto=auto)
    if kind == "email_confirmation":
        return confirm_email(payload, auto=auto)
    if kind == "notion_confirmation":
        return confirm_notion(payload, auto=auto)
    raise ValueError(f"未知的 interrupt 类型：{kind!r}")
