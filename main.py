"""命令行入口：跑一次完整研究流程，遇到人工确认点时暂停等待输入。

用法：
    python main.py "你的研究问题"
    python main.py "你的研究问题" --auto      # 演示用：自动批准
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from dotenv import load_dotenv
from langgraph.types import Command

from graph import build_graph, render_final_output
from human_review import build_resume
from logging_utils import ensure_utf8_stdout, render_log_summary
from persistence import build_checkpointer
from state import new_state


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LangGraph + Function Calling 研究助手（人工确认版 MVP）"
    )
    parser.add_argument("question", nargs="?", help="研究问题；不填则进入交互输入")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="自动批准草稿与写操作，仅用于演示/测试（等价 AGENT_AUTO_APPROVE=1）",
    )
    parser.add_argument("--thread-id", default=None, help="LangGraph checkpoint 的 thread_id")
    parser.add_argument("--log-dir", default=None, help="JSONL 日志目录，默认 AGENT_LOG_DIR 或 logs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ensure_utf8_stdout()
    args = parse_args(argv)

    question = (args.question or "").strip()
    if not question:
        try:
            question = input("请输入研究问题：").strip()
        except EOFError:
            print("未提供研究问题。用法：python main.py \"你的研究问题\"", file=sys.stderr)
            return 2
    if not question:
        print("研究问题不能为空。", file=sys.stderr)
        return 2

    auto = args.auto or os.getenv("AGENT_AUTO_APPROVE", "0").strip().lower() in {"1", "true", "yes"}
    trace_id = uuid.uuid4().hex[:8]
    state = new_state(question, trace_id=trace_id, log_dir=args.log_dir)
    graph = build_graph(build_checkpointer())
    thread_id = args.thread_id or f"research-{trace_id}"
    config = {"configurable": {"thread_id": thread_id}}

    print("=" * 72)
    print(f"研究助手启动 | trace_id={trace_id} | thread_id={thread_id} | 自动模式={auto}")
    print(f"问题：{question}")
    print("=" * 72)

    try:
        result = graph.invoke(state, config)
        while isinstance(result, dict) and "__interrupt__" in result:
            interrupts = result.get("__interrupt__") or []
            payload = interrupts[0].value if interrupts else {}
            resume_value = build_resume(payload, auto=auto)
            result = graph.invoke(Command(resume=resume_value), config)
    except KeyboardInterrupt:
        print("\n[中断] 已停止。状态已写入 SQLite checkpointer，可用同一 --thread-id 再次运行查看。")
        print(f"已写日志：{state.get('run_log_path')}")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，给出可定位信息
        print(f"\n[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"已写日志：{state.get('run_log_path')}", file=sys.stderr)
        return 1

    final = result.get("final_output") or {}
    print("\n" + "=" * 72)
    print(render_final_output(final))
    print("\n" + "=" * 72)
    print("## 结构化日志摘要（按 step_id 排序）")
    print(render_log_summary(result.get("logs") or []))
    print(f"\n完整 JSONL 日志：{state.get('run_log_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
