"""端到端流程测试：三次人工中断、确认前不写、日志链路完整。"""

from __future__ import annotations

from email import message_from_string
from email.header import decode_header, make_header
from pathlib import Path

from langgraph.types import Command

import tools
from graph import build_graph
from state import new_state

QUESTION = "LangGraph 如何实现人工中断（human-in-the-loop）？"
REQUIRED_LOG_FIELDS = {
    "step_id",
    "node_name",
    "tool_name",
    "input_summary",
    "output_summary",
    "status",
    "retry_count",
    "latency_ms",
    "error",
    "timestamp",
}
ALL_NODES = {
    "receive_question",
    "plan_research",
    "search_web",
    "fetch_pages",
    "summarize_sources",
    "draft_report",
    "human_review_draft",
    "human_confirm_actions",
    "execute_actions",
    "finalize",
}


def _start(tmp_path: Path, thread_id: str):
    graph = build_graph()
    config = {"configurable": {"thread_id": thread_id}}
    state = new_state(QUESTION, trace_id=f"t-{thread_id}", log_dir=str(tmp_path / "logs"))
    return graph, config, state


def _outbox_has_files(outbox: Path) -> bool:
    return outbox.exists() and any(path for path in outbox.rglob("*") if path.is_file())


def test_full_flow_writes_only_after_both_approvals(tmp_path, monkeypatch, fake_web):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("DEFAULT_EMAIL_TO", "boss@example.com")
    graph, config, state = _start(tmp_path, "flow-ok")

    # 第一次中断：草稿审核
    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    draft_payload = result["__interrupt__"][0].value
    assert draft_payload["type"] == "draft_review"
    assert draft_payload["evidence_count"] >= 2
    assert "https://example.org/alpha" in draft_payload["draft_report"]
    assert "https://example.org/beta" in draft_payload["draft_report"]
    # 草稿确认前不得有任何写操作
    assert not _outbox_has_files(outbox)

    # 通过草稿 → 人工暂停 ②：发邮件前确认
    result = graph.invoke(Command(resume={"decision": "approve", "comment": ""}), config)
    assert "__interrupt__" in result
    email_confirm = result["__interrupt__"][0].value
    assert email_confirm["type"] == "email_confirmation"
    assert email_confirm["email"]["to"] == "boss@example.com"
    assert not _outbox_has_files(outbox)

    # 同意发邮件 → 人工暂停 ③：存 Notion 前确认
    result = graph.invoke(
        Command(resume={"send": True, "email_payload": email_confirm["email"]}), config
    )
    assert "__interrupt__" in result
    notion_confirm = result["__interrupt__"][0].value
    assert notion_confirm["type"] == "notion_confirmation"
    assert not _outbox_has_files(outbox)

    result = graph.invoke(
        Command(resume={"save": True, "notion_payload": notion_confirm["notion"]}), config
    )
    assert "__interrupt__" not in result
    # 三处人工暂停：草稿 → 邮件 → Notion
    assert [entry["status"] for entry in result["logs"] if entry["node_name"] == "human_review_draft"] == ["approved"]
    assert len(result["logs"]) >= 10

    final = result["final_output"]
    assert final["status"] == "completed"
    assert {action["action"] for action in final["actions"]} == {"send_email", "save_to_notion"}
    assert all(action["ok"] for action in final["actions"])
    # 可观测字段：问题拆解、token、成本、耗时
    assert 3 <= len(final["sub_questions"]) <= 5
    assert "token_usage" in final and "cost_cny" in final and final["duration_ms"] >= 0
    assert all("evidence" in final and item.get("quote") for item in final["evidence"])

    emails = list((outbox / "emails").glob("*.eml"))
    assert len(emails) == 1
    message = message_from_string(emails[0].read_text(encoding="utf-8"))
    subject = str(make_header(decode_header(str(message["Subject"]))))
    body = (message.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    assert str(message["To"]) == "boss@example.com"
    assert "研究报告" in subject
    assert "https://example.org/alpha" in body

    notion_files = list((outbox / "notion").glob("*.md"))
    assert len(notion_files) == 1
    assert "研究报告" in notion_files[0].read_text(encoding="utf-8")

    # 日志能按 step_id 串起完整链路，且每条都有全部字段
    logs = result["logs"]
    step_ids = [entry["step_id"] for entry in logs]
    assert step_ids == sorted(set(step_ids))
    assert len(logs) >= 10
    for entry in logs:
        assert REQUIRED_LOG_FIELDS <= set(entry), entry
    assert ALL_NODES <= {entry["node_name"] for entry in logs}
    assert final["log_summary"]["total"] == len(logs)

    # 来源必须是真实抓取到的 URL
    assert {source["url"] for source in final["sources"]} == {
        "https://example.org/alpha",
        "https://example.org/beta",
    }

    # 日志已落盘
    log_file = Path(state["run_log_path"])
    assert log_file.exists()
    assert len(log_file.read_text(encoding="utf-8").splitlines()) == len(logs)


def test_declining_actions_finishes_without_writes(tmp_path, monkeypatch, fake_web):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    graph, config, state = _start(tmp_path, "flow-decline")

    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume={"decision": "approve", "comment": ""}), config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["type"] == "email_confirmation"
    result = graph.invoke(Command(resume={"send": False}), config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["type"] == "notion_confirmation"
    result = graph.invoke(Command(resume={"save": False}), config)

    assert "__interrupt__" not in result
    final = result["final_output"]
    assert final["status"] == "completed"
    assert final["actions"] == []
    assert not _outbox_has_files(outbox)
    assert any(entry["status"] == "skipped" for entry in result["logs"])


def test_terminate_after_draft_skips_all_actions(tmp_path, monkeypatch, fake_web):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    graph, config, state = _start(tmp_path, "flow-terminate")

    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume={"decision": "terminate", "comment": "方向不对"}), config)

    assert "__interrupt__" not in result
    final = result["final_output"]
    assert final["status"] == "terminated_by_user"
    assert final["actions"] == []
    assert not _outbox_has_files(outbox)


def test_revise_loops_back_to_draft_before_actions(tmp_path, monkeypatch, fake_web):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("DEFAULT_EMAIL_TO", "boss@example.com")
    graph, config, state = _start(tmp_path, "flow-revise")

    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    result = graph.invoke(
        Command(resume={"decision": "revise", "comment": "请补充局限与未证实部分"}),
        config,
    )
    assert "__interrupt__" in result
    second_payload = result["__interrupt__"][0].value
    assert second_payload["type"] == "draft_review"
    assert second_payload["revision_count"] == 1
    assert "请补充局限" in second_payload["draft_report"]

    result = graph.invoke(Command(resume={"decision": "approve", "comment": ""}), config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["type"] == "email_confirmation"
    result = graph.invoke(Command(resume={"send": True}), config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["type"] == "notion_confirmation"
    result = graph.invoke(Command(resume={"save": False}), config)
    assert "__interrupt__" not in result
    final = result["final_output"]
    assert [action["action"] for action in final["actions"]] == ["send_email"]
    assert not (outbox / "notion").exists()


def test_search_failure_degrades_and_reports_no_reliable_source(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    def broken_search(**_kwargs):
        raise RuntimeError("搜索服务不可用")

    monkeypatch.setattr(
        tools, "TOOL_IMPL_REGISTRY", {**tools.TOOL_IMPL_REGISTRY, "search_web": broken_search}
    )
    monkeypatch.setattr(tools.time, "sleep", lambda _seconds: None)
    graph, config, state = _start(tmp_path, "flow-no-source")

    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    draft = result["__interrupt__"][0].value["draft_report"]
    assert "未找到可靠来源" in draft
    assert "http://" not in draft and "https://" not in draft

    result = graph.invoke(Command(resume={"decision": "approve", "comment": ""}), config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["type"] == "email_confirmation"
    result = graph.invoke(Command(resume={"send": False}), config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume={"save": False}), config)
    final = result["final_output"]
    assert final["status"] == "completed_no_sources"
    assert final["sources"] == []
    assert any(error["type"] == "tool_error" for error in final["errors"])


def test_graph_records_retry_count_in_logs(tmp_path, monkeypatch, fake_web):
    attempts = {"n": 0}

    def flaky_search(query: str, max_results: int = 5):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("临时网络错误")
        return [
            {
                "title": "Alpha",
                "url": "https://example.org/alpha",
                "snippet": "snippet",
                "score": 0.9,
                "query": query,
            }
        ]

    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "search_web", flaky_search)
    monkeypatch.setattr(tools.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    graph, config, state = _start(tmp_path, "flow-retry")

    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    search_logs = [
        entry
        for entry in result["logs"]
        if entry["tool_name"] == "search_web" and entry["status"] == "ok"
    ]
    assert search_logs, "应至少有一条成功的 search_web 日志"
    assert search_logs[0]["retry_count"] == 1
    assert result["retry_counts"]["search_web"] >= 1


class _StubOpenAILLM:
    """模拟 OpenAI 模式：模型草稿里塞入未抓取 URL，验证报告清洗闸。"""

    mode = "openai"

    def plan_research(self, question: str, *, tools=None) -> dict:
        return {
            "queries": [{"query": question, "max_results": 5}],
            "focus": question,
            "provider": "stub",
        }

    def summarize_evidence(self, question: str, sources, *, llm_failed: bool = False) -> dict:
        source = sources[0]
        quote = source["content"][:40]
        return {
            "evidence": [
                {
                    "claim": "真实结论",
                    "source_url": source["url"],
                    "quote": quote,
                    "confidence": 0.8,
                }
            ],
            "note": "stub",
            "mode": "openai",
        }

    def draft_report(self, question: str, evidence, sources, *, feedback: str = "", outline=None):
        real_url = sources[0]["url"]
        draft = (
            "# 报告\n\n## 关键发现\n\n"
            f"- 真实结论（[S1]({real_url})）\n"
            "- 编造结论（[伪造来源](https://fake.example/not-fetched)）\n\n"
            "## 来源清单\n\n1. [伪造来源](https://fake.example/not-fetched)\n"
        )
        return draft, "openai"


def test_openai_mode_fabricated_urls_are_sanitized(tmp_path, monkeypatch, fake_web):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(outbox))
    import graph as graph_module

    monkeypatch.setattr(graph_module, "get_llm", lambda: _StubOpenAILLM())
    graph, config, state = _start(tmp_path, "fake-url")

    result = graph.invoke(state, config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume={"decision": "approve", "comment": ""}), config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume={"send": False}), config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume={"save": False}), config)

    final = result["final_output"]
    report = final["report"]
    assert "fake.example" not in report, "未抓取的 URL 必须从最终报告中清除"
    assert "https://example.org/alpha" in report
    assert report.count("## 来源清单") == 1, "来源清单必须按真实来源重建且只有一份"
    assert any(err.get("type") == "citation_mismatch" for err in final["errors"])
