"""持久化层：SQLite checkpointer（断点恢复）+ 会话索引表（历史列表）。

设计要点：

1. **checkpointer** 交给 LangGraph 的 ``SqliteSaver``，每个 super-step 的状态都落盘，
   服务重启后仍能用同一个 ``thread_id`` 从人工审批中断处 ``Command(resume=...)``。
2. **会话索引表** 单独存一份轻量元数据（问题、状态、父会话、更新时间），
   用于 "历史会话" 列表；不要把 UI 查询压到 checkpointer 的 checkpoint BLOB 上。
3. 两个库默认都在 ``data/`` 下，可用环境变量覆盖（测试里指到临时目录）：
   - ``AGENT_CHECKPOINT_DB``：checkpoints.sqlite
   - ``AGENT_SESSIONS_DB``：sessions.sqlite
4. 全部使用 ``check_same_thread=False`` + WAL + busy_timeout：
   FastAPI 的同步端点跑在线程池里，连接必须跨线程可用。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id       TEXT PRIMARY KEY,
    trace_id        TEXT NOT NULL DEFAULT '',
    question        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'running',
    parent_thread_id TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _env_path(env_key: str, filename: str) -> Path:
    raw = (os.getenv(env_key) or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DATA_DIR / filename


def checkpoint_db_path() -> Path:
    return _env_path("AGENT_CHECKPOINT_DB", "checkpoints.sqlite")


def sessions_db_path() -> Path:
    return _env_path("AGENT_SESSIONS_DB", "sessions.sqlite")


def write_audit_db_path() -> Path:
    return _env_path("AGENT_WRITE_AUDIT_DB", "write_audit.sqlite")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def build_checkpointer(db_path: str | os.PathLike[str] | None = None) -> SqliteSaver:
    """构造落盘到 SQLite 的 checkpointer；不传路径时读 AGENT_CHECKPOINT_DB。"""
    path = Path(db_path) if db_path else checkpoint_db_path()
    return SqliteSaver(_connect(path))


class SessionStore:
    """会话索引表：只存元数据，正文/日志仍在 checkpointer 与 JSONL 里。"""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(db_path) if db_path else sessions_db_path()
        self._lock = threading.RLock()
        self._conn = _connect(self.path)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ 写
    def upsert(
        self,
        thread_id: str,
        *,
        trace_id: str = "",
        question: str = "",
        status: str = "running",
        parent_thread_id: str = "",
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions
                    (thread_id, trace_id, question, status, parent_thread_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    status  = excluded.status,
                    trace_id = CASE WHEN excluded.trace_id <> '' THEN excluded.trace_id ELSE sessions.trace_id END,
                    question = CASE WHEN excluded.question <> '' THEN excluded.question ELSE sessions.question END,
                    updated_at = excluded.updated_at
                """,
                (
                    thread_id,
                    trace_id,
                    question[:500],
                    status,
                    parent_thread_id,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def set_status(self, thread_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE thread_id = ?",
                (status, _now(), thread_id),
            )
            self._conn.commit()

    def delete(self, thread_id: str) -> bool:
        """删除一条会话索引；返回是否真的删到了行。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_running_as_interrupted(self) -> int:
        """服务启动时调用：上一个进程退出后遗留的 running 一定是被中断的。

        只有“进程活着”才可能有 running 会话；新进程启动时还看到 running，
        说明上次执行时后端被杀/崩溃了，直接标成 interrupted，历史列表才不会骗人。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET status = 'interrupted', updated_at = ? WHERE status = 'running'",
                (_now(),),
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------ 读
    def get(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT thread_id, trace_id, question, status, parent_thread_id, created_at, updated_at "
                "FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                "SELECT thread_id, trace_id, question, status, parent_thread_id, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def trace_id(self, thread_id: str) -> str:
        row = self.get(thread_id)
        return str((row or {}).get("trace_id") or "")

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = (
            "thread_id",
            "trace_id",
            "question",
            "status",
            "parent_thread_id",
            "created_at",
            "updated_at",
        )
        return dict(zip(keys, row))


def get_session_store() -> SessionStore:
    """模块级单例；测试可以直接构造 SessionStore(tmp_path) 替换。"""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore()
    return _STORE


def delete_thread_data(
    thread_id: str,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, int]:
    """删除某个 thread 在 checkpointer 里的 checkpoint / writes 行。

    - 只删这条 thread 的流程状态，不动 logs/ 与 trace 文件（保留审计线索）；
    - 和历史列表删除配合使用，避免“列表删了、100MB 的 checkpoint 还在”。
    """
    thread_id = str(thread_id or "").strip()
    path = Path(db_path) if db_path else checkpoint_db_path()
    if not thread_id or not path.exists():
        return {"checkpoints": 0, "writes": 0}
    conn = _connect(path)
    try:
        writes = conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,)).rowcount
        checkpoints = conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)).rowcount
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"checkpoints": checkpoints, "writes": writes}


_STORE: SessionStore | None = None


# ============================================================================
# 写操作审计 + 幂等
# ============================================================================
_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS write_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key      TEXT NOT NULL,
    action       TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    mode         TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_write_actions_key
    ON write_actions(run_key, action, payload_hash);
"""


def payload_hash(action: str, payload: dict[str, Any]) -> str:
    """同一 (动作, 参数) 的稳定指纹；参数改过（换收件人/改正文）就是新的 key。"""
    try:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical = repr(payload)
    return hashlib.sha1(f"{action}\n{canonical}".encode("utf-8", "replace")).hexdigest()


class WriteAuditStore:
    """写操作审计表：既记录“谁执行过什么”，也用来做幂等去重。

    为什么需要它：``execute_actions`` 只在人工确认后调用一次，但如果进程在
    “写成功”与“checkpoint 落盘”之间被杀，恢复时节点会重跑。没有幂等键就会
    重复发邮件 / 重复写 Notion —— 这是真实存在的风险，不是理论问题。
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(db_path) if db_path else write_audit_db_path()
        self._lock = threading.RLock()
        self._conn = _connect(self.path)
        with self._lock:
            self._conn.executescript(_AUDIT_SCHEMA)
            self._conn.commit()

    def lookup(self, run_key: str, action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """查这次运行里是否已经成功执行过同样的写操作；异常按“没记录”处理。"""
        if not run_key:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT status, mode, detail, error FROM write_actions "
                    "WHERE run_key = ? AND action = ? AND payload_hash = ?",
                    (run_key, action, payload_hash(action, payload)),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            detail = json.loads(row[2]) if row[2] else {}
        except json.JSONDecodeError:
            detail = {}
        return {
            "status": str(row[0]),
            "ok": str(row[0]) == "ok",
            "mode": str(row[1]),
            "detail": detail,
            "error": str(row[3]),
        }

    def record(
        self,
        run_key: str,
        action: str,
        payload: dict[str, Any],
        *,
        ok: bool,
        mode: str = "",
        detail: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        """记一条审计（同一 key 成功过就不再覆盖成失败）；失败只降级不抛异常。"""
        if not run_key:
            return
        now = _now()
        try:
            detail_text = json.dumps(detail or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            detail_text = "{}"
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO write_actions
                        (run_key, action, payload_hash, status, mode, detail, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_key, action, payload_hash) DO UPDATE SET
                        status = excluded.status,
                        mode = excluded.mode,
                        detail = excluded.detail,
                        error = excluded.error,
                        updated_at = excluded.updated_at
                    WHERE write_actions.status <> 'ok'
                    """,
                    (
                        run_key,
                        action,
                        payload_hash(action, payload),
                        "ok" if ok else "failed",
                        mode,
                        detail_text,
                        error[:500],
                        now,
                        now,
                    ),
                )
                self._conn.commit()
        except sqlite3.Error:
            # 审计失败不能影响已完成的写操作
            return

    def entries(self, run_key: str) -> list[dict[str, Any]]:
        """某次运行的全部写操作记录（审计/测试用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, status, mode, error, created_at FROM write_actions "
                "WHERE run_key = ? ORDER BY id",
                (run_key,),
            ).fetchall()
        return [
            {"action": row[0], "status": row[1], "mode": row[2], "error": row[3], "created_at": row[4]}
            for row in rows
        ]


_AUDIT: WriteAuditStore | None = None
_AUDIT_LOCK = threading.Lock()


def get_write_audit_store() -> WriteAuditStore | None:
    """模块级单例；建库失败时返回 None（调用方直接跳过幂等/审计）。"""
    global _AUDIT
    with _AUDIT_LOCK:
        if _AUDIT is None:
            try:
                _AUDIT = WriteAuditStore()
            except (sqlite3.Error, OSError) as exc:
                print(f"[研究助手] 写操作审计不可用（不影响研究流程）：{exc}", flush=True)
                return None
        return _AUDIT


def reset_write_audit_store() -> None:
    """测试用：丢掉单例，让下一次调用按当前环境变量重新建库。"""
    global _AUDIT
    with _AUDIT_LOCK:
        _AUDIT = None


def write_idempotent_enabled() -> bool:
    raw = os.getenv("AGENT_WRITE_IDEMPOTENT", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}
