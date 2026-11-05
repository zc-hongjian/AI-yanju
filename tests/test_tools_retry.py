"""工具重试测试：最多 3 次、指数退避 0.5s/1s、成功后记录 attempts、耗尽后结构化失败。"""

from __future__ import annotations

import pytest

import tools


def test_retry_succeeds_on_third_attempt(monkeypatch):
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky_search(query: str, max_results: int = 5):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"网络抖动 {calls['n']}")
        return [{"title": "ok", "url": "https://example.org/x", "snippet": "", "score": 0.9}]

    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "search_web", flaky_search)
    monkeypatch.setattr(tools.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = tools.search_web("LangGraph interrupt", max_results=3)

    assert result.ok is True
    assert result.attempts == 3
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]
    assert [event["attempt"] for event in result.retry_events] == [1, 2]


def test_retry_exhausted_returns_structured_error(monkeypatch):
    sleeps: list[float] = []
    calls = {"n": 0}

    def always_fail(query: str, max_results: int = 5):
        calls["n"] += 1
        raise RuntimeError("搜索服务 503")

    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "search_web", always_fail)
    monkeypatch.setattr(tools.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = tools.search_web("q")

    assert result.ok is False
    assert result.rejected is False
    assert result.attempts == 3
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]
    assert "重试 3 次后仍失败" in (result.error or "")
    assert "503" in (result.error or "")


def test_validation_error_is_not_retried(monkeypatch):
    # 空 query 在参数校验阶段就失败，不应触发重试
    result = tools.search_web("")
    assert result.ok is False
    assert result.attempts == 0
    assert "参数校验失败" in (result.error or "")


def test_unknown_tool_returns_error():
    result = tools.call_tool("delete_everything", {})
    assert result.ok is False
    assert "未知工具" in (result.error or "")


def test_non_retryable_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(url: str):
        calls["n"] += 1
        raise tools.NonRetryableToolError("HTTP 404（页面不可用）")

    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "fetch_page", fake_fetch)
    result = tools.fetch_page("https://example.org/not-found")

    assert result.ok is False
    assert calls["n"] == 1, "确定性失败不应重试"
    assert result.attempts == 1
    assert "不可重试失败" in (result.error or "")


def test_retry_stops_when_deadline_passed():
    import time as _time

    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise RuntimeError("超时")

    with pytest.raises(tools.ToolExecutionError) as excinfo:
        tools.retry_call(
            always_fail,
            attempts=3,
            deadline=_time.monotonic() - 1,
            sleep=lambda _seconds: None,
        )

    assert calls["n"] == 1, "已超过时间预算时不应再次尝试"
    assert "超过时间预算" in str(excinfo.value)
