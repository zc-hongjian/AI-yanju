"""确定性的本地假网页（仅用于 eval --mode mock）。

真实网页会随时间变化、也可能被限流，因此流水线闭环、引用、延迟类指标用本地 fixture 测；
真实检索质量用 eval --mode real 单独跑，并在报告里分开标注。
"""

from __future__ import annotations

import re
from typing import Any, Callable


def _one_line(text: str) -> str:
    """去掉句号/问号，避免把 fixture 句子在分句时切断。"""
    return re.sub(r"\s+", " ", re.sub(r"[。．？?！!]+", " ", str(text or ""))).strip()


def build_fixtures(entry: dict[str, Any], count: int = 3) -> list[dict[str, str]]:
    """为一道题生成 3 个内容互不重复的假网页。"""
    question = _one_line(entry.get("question")) or "研究问题"
    facts = [_one_line(fact) for fact in (entry.get("expected_facts") or []) if _one_line(fact)]
    if not facts:
        facts = [f"{question} 的公开资料信息"]

    pages: list[dict[str, str]] = []
    for index in range(count):
        part = facts[index::count] or facts[:1]
        # 每条事实句都同时包含「问题原文 + 事实」，保证离线启发式能按关键词选中它
        sentences = [f"关于「{question}」，据公开资料，{fact}。" for fact in part]
        sentences.append("该资料由离线评测的本地网页提供，用于验证抓取、引用与去重流程。")
        content = "".join(sentences)
        if len(content) < 160:
            content += "该本地网页只用于评测研究助手流水线，不代表真实互联网内容。"
        pages.append(
            {
                "url": f"https://mock.example/{entry.get('id', 'q0').lower()}/{index + 1}",
                "title": f"{question}（公开资料 {index + 1}）",
                "content": content,
            }
        )
    return pages


def install(entry: dict[str, Any]) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """返回 (fake_search, fake_fetch)，由调用方塞进 tools.TOOL_IMPL_REGISTRY。"""
    pages = build_fixtures(entry)
    by_url = {page["url"]: page for page in pages}

    def fake_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
        return [
            {
                "title": page["title"],
                "url": page["url"],
                "snippet": page["content"][:60],
                "query": query,
                "score": round(0.95 - index * 0.05, 2),
            }
            for index, page in enumerate(pages[: int(max_results or 5)])
        ]

    def fake_fetch(url: str) -> dict[str, Any]:
        page = by_url.get(str(url))
        if page is None:
            raise RuntimeError(f"404: {url}")
        return {
            "url": page["url"],
            "title": page["title"],
            "content": page["content"],
            "chars": len(page["content"]),
            "encoding": "utf-8",
            "dropped_blocks": 0,
        }

    return fake_search, fake_fetch
