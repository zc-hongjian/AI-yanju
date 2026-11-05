"""工具安全测试：SSRF 防护（域名解析到内网也必须拒绝）。"""

from __future__ import annotations

import socket

import tools


def test_fetch_blocks_hostname_resolving_to_private_ip(monkeypatch):
    private_info = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", 0))
    ]
    monkeypatch.setattr(tools.socket, "getaddrinfo", lambda *args, **kwargs: private_info)

    result = tools.fetch_page("https://internal.example.com/page")

    assert result.ok is False
    assert result.attempts == 0, "安全校验失败不应重试"
    assert "内网" in (result.error or "")


def test_fetch_blocks_literal_loopback_ip():
    result = tools.fetch_page("http://127.0.0.1/admin")
    assert result.ok is False
    assert "内网" in (result.error or "")


def test_fetch_rejects_non_http_scheme():
    result = tools.fetch_page("file:///etc/passwd")
    assert result.ok is False
    assert "http" in (result.error or "")
