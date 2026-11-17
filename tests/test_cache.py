"""搜索/抓取 TTL 缓存：默认旁路、命中、过期、清空、损坏数据降级。"""

from __future__ import annotations

import sqlite3
import time

import cache


def _enable(monkeypatch, tmp_path, ttl="86400"):
    monkeypatch.setenv("AGENT_CACHE_DB", str(tmp_path / "cache.sqlite"))
    monkeypatch.setenv("AGENT_CACHE_ENABLED", "1")
    monkeypatch.setenv("AGENT_CACHE_TTL", ttl)


def test_disabled_bypasses_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CACHE_DB", str(tmp_path / "cache.sqlite"))
    monkeypatch.setenv("AGENT_CACHE_ENABLED", "0")
    assert cache.put_value("fetch", "https://a.test/1", {"content": "x"}) is False
    assert cache.get_value("fetch", "https://a.test/1") is None


def test_put_get_roundtrip(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    payload = {"url": "https://a.test/1", "content": "正文", "chars": 2}
    assert cache.put_value("fetch", "https://a.test/1", payload) is True
    assert cache.get_value("fetch", "https://a.test/1") == payload
    assert cache.get_value("fetch", "https://a.test/2") is None
    assert cache.get_value("search", "https://a.test/1") is None, "kind 之间不能串"


def test_ttl_expiry(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, ttl="60")
    cache.put_value("fetch", "https://a.test/old", {"content": "old"})
    # 直接把 created_at 改老，比 sleep 稳定
    with sqlite3.connect(tmp_path / "cache.sqlite") as conn:
        conn.execute("UPDATE cache SET created_at = ?", (time.time() - 120,))
        conn.commit()
    assert cache.get_value("fetch", "https://a.test/old") is None
    with sqlite3.connect(tmp_path / "cache.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0, "过期行应被清掉"


def test_clear_and_stats(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    cache.put_value("search", "q1", [{"url": "https://a.test"}])
    cache.put_value("fetch", "u1", {"content": "x"})
    stats = cache.stats()
    assert stats["enabled"] is True
    assert stats["entries"] == 2
    assert cache.clear("fetch") == 1
    assert cache.stats()["entries"] == 1
    assert cache.clear() == 1
    assert cache.stats()["entries"] == 0


def test_corrupt_value_degrades_to_miss(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    cache.put_value("fetch", "https://a.test/bad", {"content": "ok"})
    with sqlite3.connect(tmp_path / "cache.sqlite") as conn:
        conn.execute("UPDATE cache SET value = '{not json'")
        conn.commit()
    assert cache.get_value("fetch", "https://a.test/bad") is None


def test_normalize_url_drops_fragment():
    assert cache.normalize_url("https://a.test/p?q=1#sec2") == "https://a.test/p?q=1"
    assert cache.normalize_url("  https://a.test/p  ") == "https://a.test/p"
