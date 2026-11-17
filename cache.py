"""搜索 / 抓取结果的本地 TTL 缓存（SQLite）。

动机：同一问题反复跑评测、调试或看历史时，没必要每次都重新打站点——
既浪费时间（拉高 P95），也更容易被站点限流/封 IP。

设计约束（与项目“不能因为辅助功能拖垮主流程”一致）：
- **默认开启但可完全旁路**：``AGENT_CACHE_ENABLED=0`` 时所有读写直接返回；
- **只缓存成功结果**：搜索命中、抓取成功正文；403/超时/空页照常重试，不被缓存成“永久失败”；
- **任何缓存故障都静默降级**：库损坏、目录不可写、TTL 解析失败，都当作没缓存；
- 每次操作新建连接（并发抓取线程安全，WAL 模式写入不互相阻塞）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

_DEFAULT_DB = "data/cache.sqlite"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cache ("
    "kind TEXT NOT NULL, key_hash TEXT NOT NULL, value TEXT NOT NULL, "
    "created_at REAL NOT NULL, PRIMARY KEY (kind, key_hash))"
)


def enabled() -> bool:
    raw = os.getenv("AGENT_CACHE_ENABLED", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def ttl() -> float:
    try:
        value = float(os.getenv("AGENT_CACHE_TTL", "86400"))
    except ValueError:
        return 86400.0
    return max(0.0, value)


def db_path() -> Path:
    raw = os.getenv("AGENT_CACHE_DB", "").strip()
    return Path(raw) if raw else Path(_DEFAULT_DB)


def normalize_url(url: str) -> str:
    """去掉 fragment（同一页面的锚点不算不同资源），保留查询参数。"""
    return str(url or "").split("#", 1)[0].strip()


def _key_hash(kind: str, key: str) -> str:
    return hashlib.sha1(f"{kind}\n{key}".encode("utf-8", "replace")).hexdigest()


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    return conn


def get_value(kind: str, key: str) -> Any | None:
    """命中返回反序列化后的值；未命中 / 过期 / 关闭 / 异常一律返回 None。"""
    if not enabled() or ttl() <= 0:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value, created_at FROM cache WHERE kind = ? AND key_hash = ?",
                (kind, _key_hash(kind, key)),
            ).fetchone()
            if row is None:
                return None
            value_text, created_at = row
            if time.time() - float(created_at) > ttl():
                conn.execute(
                    "DELETE FROM cache WHERE kind = ? AND key_hash = ?",
                    (kind, _key_hash(kind, key)),
                )
                return None
        return json.loads(value_text)
    except (sqlite3.Error, OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def put_value(kind: str, key: str, value: Any) -> bool:
    """写入缓存；失败只返回 False（调用方不该因此报错）。"""
    if not enabled() or ttl() <= 0:
        return False
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (kind, key_hash, value, created_at) VALUES (?, ?, ?, ?)",
                (kind, _key_hash(kind, key), payload, time.time()),
            )
        return True
    except (sqlite3.Error, OSError):
        return False


def clear(kind: str = "") -> int:
    """清空缓存（可按 kind），返回删除行数。"""
    try:
        with _connect() as conn:
            if kind:
                cur = conn.execute("DELETE FROM cache WHERE kind = ?", (kind,))
            else:
                cur = conn.execute("DELETE FROM cache")
            return int(cur.rowcount or 0)
    except (sqlite3.Error, OSError):
        return 0


def stats() -> dict[str, Any]:
    """缓存现状（条数 / 大小 / TTL），供健康检查与调试。"""
    path = db_path()
    data: dict[str, Any] = {
        "enabled": enabled(),
        "ttl_seconds": ttl(),
        "path": str(path),
        "entries": 0,
        "bytes": 0,
    }
    try:
        if path.exists():
            data["bytes"] = path.stat().st_size
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM cache").fetchone()
            data["entries"] = int(row[0]) if row else 0
    except (sqlite3.Error, OSError):
        pass
    return data
