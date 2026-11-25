"""How 类问题 P0：代码块保留、首页降权、官方站查询与自动回退。"""

from __future__ import annotations

import graph
import prompts
import tools
from state import new_state

HOW_HTML = """<html><head><title>LangGraph 快速开始</title></head><body><article>
<h1>快速开始</h1>
<p>这是一段足够长的说明文字，用来通过抽取器对段落长度的最小门槛检查。</p>
<pre><code>pip install langgraph
from langgraph.graph import StateGraph</code></pre>
<p>结尾再放一段足够长的说明文字，保证整页正文不会因为过短而被判定为 JS 渲染空页。</p>
</article></body></html>"""


def test_extract_article_keeps_code_fences():
    title, parts, _ = tools._extract_article(HOW_HTML)
    content = "\n".join(parts)
    assert title == "LangGraph 快速开始"
    assert "```" in content, "代码块必须转成围栏保留，否则 How 报告永远拿不到代码"
    assert "pip install langgraph" in content
    assert "StateGraph" in content
    assert content.count("```") == 2


def test_how_queries_are_forced_for_how_questions():
    queries = [{"query": "LangGraph interrupt 实现", "max_results": 5}]
    ensured = graph._ensure_how_queries("LangGraph 如何实现人工中断？", queries)
    texts = [item["query"] for item in ensured]
    assert len(ensured) <= graph.MAX_QUERIES
    assert texts[0] == "LangGraph interrupt 实现", "宽泛查询要保留在最前"
    assert any("代码示例" in text or "code example" in text.lower() for text in texts)
    assert any("site:" in text.lower() for text in texts)


def test_non_how_question_keeps_queries_unchanged():
    queries = [{"query": "LangGraph 是什么", "max_results": 5}]
    assert graph._ensure_how_queries("LangGraph 是什么？", queries) == queries


def test_existing_site_and_code_queries_are_reused():
    queries = [
        {"query": "LangGraph interrupt", "max_results": 5},
        {"query": "LangGraph interrupt 代码示例", "max_results": 5},
        {"query": "LangGraph interrupt site:langchain.com", "max_results": 5},
    ]
    ensured = graph._ensure_how_queries("LangGraph 如何实现人工中断？", queries)
    texts = [item["query"] for item in ensured]
    assert len(texts) == 3
    assert "LangGraph interrupt 代码示例" in texts
    assert "LangGraph interrupt site:langchain.com" in texts


def test_tutorial_site_query_is_not_reused_as_code_query():
    """回归：模型给的“教程 site:xxx”不能被同时当成代码查询和 site 查询。"""
    queries = [
        {"query": "LangGraph implementation 中断", "max_results": 5},
        {"query": "LangGraph 人工中断 教程 site:github.com", "max_results": 5},
    ]
    texts = [item["query"] for item in graph._ensure_how_queries("LangGraph 如何实现人工中断？", queries)]
    assert len(texts) == len(set(texts)), f"查询不能重复：{texts}"
    assert any("代码示例" in text or "code example" in text.lower() for text in texts)
    assert any("site:" in text.lower() for text in texts)


def test_plan_research_adds_how_queries(tmp_path):
    state = new_state("LangGraph 如何实现人工中断？", trace_id="how-plan", log_dir=str(tmp_path / "logs"))
    updates = graph.plan_research(state)
    texts = [item["query"] for item in updates["plan"]["queries"]]
    assert any("代码示例" in text or "code example" in text.lower() for text in texts)
    assert any("site:" in text.lower() for text in texts)


def test_index_urls_are_downranked_for_how_questions():
    homepage = {"url": "https://www.langchain.com/index.html", "title": "LangChain", "snippet": ""}
    subpage = {
        "url": "https://www.langchain.com/docs/langgraph/interrupt",
        "title": "interrupt 文档",
        "snippet": "",
    }
    assert graph._is_index_url(homepage["url"])
    assert not graph._is_index_url(subpage["url"])
    assert graph._rank_bonus(subpage, ["interrupt"], how=True) > graph._rank_bonus(
        homepage, ["interrupt"], how=True
    )


def test_site_query_falls_back_when_no_results(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_call(name, payload, human_approved=False):
        query = payload["query"]
        calls.append(query)
        if "site:" in query:
            return tools.ToolResult(tool="search_web", ok=True, data=[], attempts=1, latency_ms=5)
        return tools.ToolResult(
            tool="search_web",
            ok=True,
            data=[{"url": "https://example.org/docs/a", "title": "A", "snippet": "s", "score": 0.9}],
            attempts=1,
            latency_ms=5,
        )

    monkeypatch.setattr(tools, "call_tool", fake_call)
    state = new_state("LangGraph 如何实现人工中断？", trace_id="site-fallback", log_dir=str(tmp_path / "logs"))
    state["plan"] = {
        "queries": [
            {"query": "LangGraph interrupt 实现"},
            {"query": "LangGraph interrupt 代码示例"},
            {"query": "LangGraph interrupt site:langchain.com"},
        ]
    }
    updates = graph.search_web_node(state)
    assert len(calls) == 4, "site: 查询 0 结果后应再发一次无 site: 查询"
    assert any("site:" not in query for query in calls[3:])
    assert updates["search_results"], "回退查询应拿到结果"
    assert any("已回退" in entry["output_summary"] for entry in updates["logs"])


def test_fetch_cache_kind_is_bumped_for_code_extraction():
    # 抽取逻辑升级后必须换缓存 namespace，否则旧缓存里的“无代码正文”会继续被命中
    assert tools.FETCH_CACHE_KIND == "fetch:v2"
