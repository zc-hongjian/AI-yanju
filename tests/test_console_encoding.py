"""控制台编码兜底：GBK 控制台打印 ¥ 不应把一次研究崩成 failed。"""

from __future__ import annotations

import io
import sys

from logging_utils import ensure_utf8_stdout


def test_ensure_utf8_stdout_never_raises():
    # pytest 下 stdout 通常已是 UTF-8；这里只保证接口幂等、不抛异常
    assert isinstance(ensure_utf8_stdout(), bool)
    assert isinstance(ensure_utf8_stdout(), bool)


def test_gbk_stream_is_reconfigured(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk")
    monkeypatch.setattr(sys, "stdout", stream)
    assert ensure_utf8_stdout() is True
    assert "utf" in (stream.encoding or "").lower()
    assert stream.encoding.lower() in {"utf-8", "utf8"}


def test_reconfigure_failure_is_swallowed(monkeypatch):
    class Broken(io.StringIO):
        encoding = "gbk"

        def reconfigure(self, **kwargs):  # noqa: D401 - 模拟平台不支持
            raise OSError("not supported")

    monkeypatch.setattr(sys, "stdout", Broken())
    assert ensure_utf8_stdout() is False
