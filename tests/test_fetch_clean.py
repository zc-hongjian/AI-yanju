"""抓取清洗验收：抓正文、去广告/导航/脚本、段落去重。"""

from __future__ import annotations

import pytest

import tools

BODY = """
<nav>首页 产品 价格 登录</nav>
<div class="ad-banner">双十一大促，立即购买，限时五折</div>
<div id="cookie-notice">本站使用 Cookie 改善体验，继续浏览即表示同意</div>
<article>
<h1>LangGraph 人工中断机制</h1>
<p>LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。</p>
<p>LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。</p>
<p>调用方通过 Command 的 resume 参数恢复执行，节点会从 interrupt 处继续。</p>
<p>如果编译图时没有提供 checkpointer，interrupt 调用会直接报错，流程无法暂停。</p>
<p>官方文档建议为每次运行指定 thread_id，并在恢复时复用同一个 thread_id。</p>
<script>trackPageView("noise")</script>
</article>
<footer>版权所有 联系我们</footer>
"""

HTML = f"<html><head><title>测试文章</title><style>.x{{}}</style></head><body>{BODY}</body></html>"


class _FakeResponse:
    status_code = 200

    def __init__(self, raw: bytes) -> None:
        self.content = raw
        self.headers = {"content-type": "text/html; charset=utf-8"}

    def iter_content(self, chunk_size: int):
        yield self.content

    def close(self) -> None:
        pass


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch):
    def _run(html: str = HTML) -> dict[str, object]:
        monkeypatch.setattr(tools, "_assert_safe_url", lambda url: None)
        monkeypatch.setattr(
            tools, "_fetch_with_safe_redirects", lambda url, **kwargs: _FakeResponse(html.encode("utf-8"))
        )
        return tools._fetch_page_impl("https://example.org/article")

    return _run


def test_fetch_strips_ads_nav_scripts_and_dedupes(fetch):
    page = fetch()
    content = str(page["content"])
    assert "checkpointer" in content and "Command" in content
    assert "大促" not in content, "广告容器要删除"
    assert "Cookie" not in content, "Cookie 提示要删除"
    assert "首页 产品 价格" not in content, "导航栏要删除"
    assert "版权所有" not in content, "页脚要删除"
    assert "trackPageView" not in content, "脚本要删除"
    # 相同段落只保留一次
    assert content.count("LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。") == 1
    assert page["chars"] >= 120


def test_fetch_rejects_page_with_only_ads(fetch):
    html = "<html><head><title>广告页</title></head><body><div class='ad'>大促</div></body></html>"
    with pytest.raises(tools.NonRetryableToolError):
        fetch(html)
