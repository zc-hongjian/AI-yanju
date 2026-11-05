"""失败处理验收：模型报错 / 工具失败时的降级路径（不静默失败、不崩图）。"""

from __future__ import annotations

import pytest

from llm_client import LLMClient, LLMError

SOURCE = {
    "url": "https://real.example/a",
    "title": "真实来源",
    "content": (
        "人工中断需要 checkpointer 持久化状态，否则无法恢复。"
        "LangGraph 用 interrupt 函数暂停图，并把控制权交还给调用方。"
    ),
}


def _failing_client(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    client = LLMClient(mode="openai", api_key="test-key")
    monkeypatch.setattr(client, "_chat", lambda *args, **kwargs: (_ for _ in ()).throw(LLMError("模拟模型报错")))
    return client


def test_summarize_evidence_degrades_when_llm_raises(monkeypatch):
    client = _failing_client(monkeypatch)
    result = client.summarize_evidence("人工中断需要什么？", [SOURCE])
    assert result["mode"] == "offline_fallback"
    assert result["evidence"], "模型报错时必须降级为逐句摘录，而不是返回空"
    assert "降级" in result["note"]


def test_draft_report_degrades_when_llm_raises(monkeypatch):
    client = _failing_client(monkeypatch)
    evidence = [
        {
            "claim": "人工中断需要 checkpointer 持久化状态",
            "source_url": SOURCE["url"],
            "quote": "人工中断需要 checkpointer 持久化状态，否则无法恢复。",
            "confidence": 0.8,
        }
    ]
    text, mode = client.draft_report("人工中断如何实现", evidence, [{"url": SOURCE["url"], "title": "真实来源"}])
    assert mode == "offline_fallback"
    assert "## 摘要" in text and "## 关键发现" in text


def test_plan_research_raises_after_both_paths_fail(monkeypatch):
    client = _failing_client(monkeypatch)
    monkeypatch.setattr(client, "_sdk", lambda: (_ for _ in ()).throw(RuntimeError("SDK 不可用")))
    with pytest.raises(LLMError):
        client.plan_research("LangGraph 如何实现人工中断？", tools=[])


def test_plan_research_degrades_to_single_query_in_graph(monkeypatch, tmp_path):
    """plan 节点捕获规划异常后必须降级，而不是让整张图崩掉。"""
    import graph
    from state import new_state

    client = _failing_client(monkeypatch)
    monkeypatch.setattr(client, "_sdk", lambda: (_ for _ in ()).throw(RuntimeError("SDK 不可用")))
    monkeypatch.setattr(graph, "get_llm", lambda: client)
    state = new_state("LangGraph 如何实现人工中断？", trace_id="plan-fail", log_dir=str(tmp_path / "logs"))
    updates = graph.plan_research(state)
    assert updates["plan"]["provider"] == "fallback"
    assert updates["plan"]["queries"]
    assert updates["errors"] and updates["errors"][-1]["type"] == "llm_error"
