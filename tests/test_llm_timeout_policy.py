"""超时策略回归：一次 30s 超时不能被放大成 4×30s（线上实测 121s 的根因）。

历史问题：SDK 内部 max_retries=1 叠加「去掉 response_format 再试一次」，
把一次超时变成 4 次 30s 请求；而 glm-4-flash 输出 750 token 本来就需要 27~33s。
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import llm_client
from llm_client import LLMClient, LLMError


class _Timeout(Exception):
    """按类名模拟 openai.APITimeoutError。"""


class _BadRequest(Exception):
    def __init__(self, status_code: int = 400) -> None:
        super().__init__("bad request")
        self.status_code = status_code


class _ServerError(Exception):
    def __init__(self, status_code: int = 503) -> None:
        super().__init__("server error")
        self.status_code = status_code


def _response(content: str = "ok", prompt: int = 10, completion: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        ),
    )


class _FakeSDK:
    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, object]] = []

    def with_options(self, **kwargs: object) -> "_FakeSDK":
        return self

    @property
    def chat(self) -> SimpleNamespace:
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def _client(monkeypatch, script: list[object]) -> tuple[LLMClient, _FakeSDK]:
    client = LLMClient(mode="openai", api_key="test-key")
    sdk = _FakeSDK(script)
    monkeypatch.setattr(client, "_sdk", lambda: sdk)
    return client, sdk


def test_timeout_is_not_retried_and_not_fell_back(monkeypatch):
    client, sdk = _client(monkeypatch, [_Timeout("Request timed out.")])
    with pytest.raises(LLMError) as excinfo:
        client._chat("s", "u", response_format={"type": "json_object"})
    assert "超时" in str(excinfo.value)
    assert len(sdk.calls) == 1, "超时后不允许再发请求，否则 30s 会被放大成 120s"


def test_bad_request_falls_back_without_response_format(monkeypatch):
    client, sdk = _client(monkeypatch, [_BadRequest(400), _response("json ok")])
    assert client._chat("s", "u", response_format={"type": "json_object"}) == "json ok"
    assert len(sdk.calls) == 2
    assert "response_format" not in sdk.calls[1]


def test_transient_error_retries_once_fast(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda _seconds: None)
    client, sdk = _client(monkeypatch, [_ServerError(503), _response("ok")])
    assert client._chat("s", "u") == "ok"
    assert len(sdk.calls) == 2


def test_sdk_is_created_without_hidden_retries(monkeypatch):
    client = LLMClient(mode="openai", api_key="test-key")
    captured: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    client._sdk()
    assert captured["max_retries"] == 0
    assert captured["timeout"] == client.timeout


def test_summarize_uses_long_timeout(monkeypatch):
    monkeypatch.delenv("AGENT_SUMMARIZE_TIMEOUT", raising=False)
    client = LLMClient(mode="openai", api_key="test-key")
    captured: dict[str, object] = {}

    def fake_chat(system, user, **kwargs):
        captured.update(kwargs)
        return '{"evidence": [], "note": "ok"}'

    monkeypatch.setattr(client, "_chat", fake_chat)
    client.summarize_evidence("问题", [{"url": "https://e.test/a", "title": "t", "content": "正文"}])
    assert captured["timeout"] == client.long_timeout
    assert client.long_timeout >= 60


def test_summarize_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_SUMMARIZE_TIMEOUT", "123")
    client = LLMClient(mode="openai", api_key="test-key")
    captured: dict[str, object] = {}

    def fake_chat(system, user, **kwargs):
        captured.update(kwargs)
        return '{"evidence": [], "note": "ok"}'

    monkeypatch.setattr(client, "_chat", fake_chat)
    client.summarize_evidence("问题", [{"url": "https://e.test/a", "title": "t", "content": "正文"}])
    assert captured["timeout"] == 123


def test_long_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_TIMEOUT_LONG", "90")
    assert LLMClient(mode="offline").long_timeout == 90
