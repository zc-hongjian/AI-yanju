"""分节写作并发化：输出顺序必须与串行完全一致，且确实发生了并发。"""

from __future__ import annotations

import threading
import time

import pytest

import llm_client
import reporting
from llm_client import LLMClient

EVIDENCE = [
    {
        "claim": "人工中断需要 checkpointer 持久化状态",
        "source_url": "https://example.org/alpha",
        "quote": "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。",
        "confidence": 0.9,
    },
    {
        "claim": "写操作前必须人工确认",
        "source_url": "https://example.org/beta",
        "quote": "在 LangGraph 中，human-in-the-loop 通常用于写操作前的确认。",
        "confidence": 0.8,
    },
]
SOURCES = [{"url": "https://example.org/alpha", "title": "Alpha"}, {"url": "https://example.org/beta", "title": "Beta"}]


def _outline() -> dict:
    return {
        "sections": [
            {"title": reporting.SUMMARY_TITLE, "key": "summary"},
            {"title": reporting.FINDINGS_TITLE, "key": "findings"},
            {"title": reporting.ANALYSIS_TITLE, "key": "analysis"},
            {"title": "挑战一"},
            {"title": "挑战二"},
            {"title": reporting.LIMITS_TITLE, "key": "limits"},
        ],
        "uncovered": [],
    }


def _client_with_tracker() -> tuple[LLMClient, dict[str, object]]:
    client = LLMClient(mode="openai", api_key="test-key", timeout=5)
    state: dict[str, object] = {"active": 0, "max_active": 0, "calls": 0, "threads": set()}
    lock = threading.Lock()

    def fake_chat(system, user, **kwargs):
        with lock:
            state["active"] = int(state["active"]) + 1
            state["max_active"] = max(int(state["max_active"]), int(state["active"]))
            state["calls"] = int(state["calls"]) + 1
            state["threads"].add(threading.current_thread().name)  # type: ignore[union-attr]
        time.sleep(0.15)
        with lock:
            state["active"] = int(state["active"]) - 1
        return "小节正文。"

    client._chat = fake_chat  # type: ignore[method-assign]
    return client, state


def _headings(text: str) -> list[str]:
    return [line.strip("# ").strip() for line in text.splitlines() if line.startswith("## ")]


def test_sections_run_concurrently_and_keep_outline_order(monkeypatch):
    monkeypatch.setenv("AGENT_DRAFT_CONCURRENCY", "3")
    client, state = _client_with_tracker()
    text = client.draft_report("研究问题", EVIDENCE, SOURCES, outline=_outline())[0]
    assert int(state["max_active"]) >= 2, "分节写作应当并发执行"
    # summary / analysis / 2 个自定义小节 = 4 次 LLM 调用（关键发现和局限是程序生成）
    assert int(state["calls"]) == 4
    assert _headings(text) == [
        reporting.SUMMARY_TITLE,
        reporting.FINDINGS_TITLE,
        reporting.ANALYSIS_TITLE,
        "挑战一",
        "挑战二",
        reporting.LIMITS_TITLE,
    ]


def test_concurrency_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_DRAFT_CONCURRENCY", "1")
    client, state = _client_with_tracker()
    client.draft_report("研究问题", EVIDENCE, SOURCES, outline=_outline())
    assert int(state["max_active"]) == 1, "AGENT_DRAFT_CONCURRENCY=1 时必须逐节串行"


def test_parallel_and_serial_output_are_identical(monkeypatch):
    monkeypatch.setenv("AGENT_DRAFT_CONCURRENCY", "4")
    parallel, _ = _client_with_tracker()
    serial_client, _ = _client_with_tracker()
    monkeypatch.setenv("AGENT_DRAFT_CONCURRENCY", "1")
    serial_text = serial_client.draft_report("研究问题", EVIDENCE, SOURCES, outline=_outline())[0]
    monkeypatch.setenv("AGENT_DRAFT_CONCURRENCY", "4")
    parallel_text = parallel.draft_report("研究问题", EVIDENCE, SOURCES, outline=_outline())[0]
    assert parallel_text == serial_text


def test_parallel_usage_is_counted_exactly(monkeypatch):
    # 真实客户端里 _record_usage 会被并发调用：用假响应验证加锁后计数不丢。
    client = LLMClient(mode="openai", api_key="test-key", timeout=5)
    monkeypatch.setenv("AGENT_DRAFT_CONCURRENCY", "3")

    def fake_chat(system, user, **kwargs):
        time.sleep(0.05)
        client._record_usage(_fake_usage())
        return "正文。"

    client._chat = fake_chat  # type: ignore[method-assign]
    client.draft_report("研究问题", EVIDENCE, SOURCES, outline=_outline())
    assert client.usage_snapshot()["calls"] == 4
    assert client.usage_snapshot()["total_tokens"] == 4 * 15


def _fake_usage():
    from types import SimpleNamespace

    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    )
