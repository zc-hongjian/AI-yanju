"""搜索提供方测试：360 结果解析、中文宽泛问题优先使用 360。"""

from __future__ import annotations

import tools


def test_provider_order_prefers_360_for_broad_chinese(monkeypatch):
    monkeypatch.setenv("AGENT_SEARCH_PROVIDERS", "bing,so360,duckduckgo,wikipedia")
    assert tools._provider_order("2025 年 AI Agent 在企业落地的主要挑战是什么")[0].__name__ == "_search_so360"
    # 具体框架/技术词仍优先 Bing（官方文档更全）
    assert tools._provider_order("LangGraph 如何实现人工中断")[0].__name__ == "_search_bing"
    assert tools._provider_order("BM25 vs vector search")[0].__name__ == "_search_bing"


def test_search_so360_parses_data_mdurl(monkeypatch):
    html = (
        "<html><body><ul>"
        '<li class="res-list"><h3><a data-mdurl="https://example.org/a" '
        'href="https://www.so.com/link?m=x">AI Agent 挑战</a></h3>'
        '<div class="res-desc">关于挑战的摘要</div></li>'
        '<li class="res-list"><h3><a data-mdurl="https://example.org/b" '
        'href="https://www.so.com/link?m=y">第二篇</a></h3>'
        "<p>第二段摘要</p></li>"
        "</ul></body></html>"
    )

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = html.encode("utf-8")

    monkeypatch.setattr(tools.requests, "get", lambda *args, **kwargs: _Resp())
    hits = tools._search_so360("AI Agent 挑战", 5)
    assert [hit["url"] for hit in hits] == ["https://example.org/a", "https://example.org/b"]
    assert hits[0]["title"] == "AI Agent 挑战"
    assert "挑战的摘要" in hits[0]["snippet"]
    assert "so360" in tools.SEARCH_PROVIDERS
