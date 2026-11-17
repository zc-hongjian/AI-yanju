"""协作式取消登记表。

设计取舍：**不杀线程**，只在节点边界检查取消标志。
图节点里可能有正在进行的 HTTP 请求（搜索/抓取预算 15~20s），强杀线程会留下
半写的 checkpoint 和半落的 outbox，比“多等一个节点”更糟。因此：

- Web 层收到取消请求后只置一个 ``threading.Event``；
- ``graph._instrument`` 在每个节点入口检查，命中就抛 :class:`ResearchCancelled`；
- 运行线程捕获该异常，把会话状态标成 ``cancelled``，向前端推终态事件。

最坏延迟 = 当前节点的剩余时间（受 ``AGENT_*_TIMEOUT`` 预算约束）。
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_EVENTS: dict[str, threading.Event] = {}


class ResearchCancelled(Exception):
    """用户主动取消运行。

    刻意继承 ``Exception`` 而不是 ``BaseException``：调用方要能统一用
    ``except Exception`` 兜底，但图里的 ``_instrument`` 必须显式放行，
    不要把它记成节点 error span（见 ``graph._instrument``）。
    """


def request(trace_id: str) -> bool:
    """登记取消请求；返回是否是“新增”的取消（重复请求返回 False）。"""
    key = (trace_id or "").strip()
    if not key:
        return False
    with _LOCK:
        event = _EVENTS.get(key)
        if event is None:
            event = threading.Event()
            _EVENTS[key] = event
        already = event.is_set()
        event.set()
    return not already


def is_cancelled(trace_id: str) -> bool:
    """节点边界检查：只读，不加锁复制字典也能接受（漏判最多多跑一个节点）。"""
    key = (trace_id or "").strip()
    if not key:
        return False
    with _LOCK:
        event = _EVENTS.get(key)
    return bool(event is not None and event.is_set())


def clear(trace_id: str) -> None:
    """运行结束后清理，避免登记表随运行次数无限增长。"""
    key = (trace_id or "").strip()
    if not key:
        return
    with _LOCK:
        _EVENTS.pop(key, None)


def pending() -> list[str]:
    """当前仍在取消中的 trace 列表（调试/测试用）。"""
    with _LOCK:
        return sorted(key for key, event in _EVENTS.items() if event.is_set())
