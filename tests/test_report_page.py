"""报告阅读页（/report/<thread_id>）测试：路由与前端钩子。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import web

ROOT = Path(__file__).resolve().parents[1]


def _report_block(html: str) -> str:
    """截出报告页那段脚本，避免用整页字符串误判。"""
    start = html.index("//  报告阅读页：/report/<thread_id>")
    return html[start:]


def test_report_route_serves_the_reading_page():
    """报告页复用同一个 index.html，靠路径进入 report 模式。"""
    client = TestClient(web.app)
    resp = client.get("/report/web-abc123")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="report-shell"' in html
    assert 'id="report-body"' in html
    assert 'id="report-toc"' in html
    assert 'id="report-evidence"' in html
    assert 'id="report-print"' in html


def test_report_page_is_read_only():
    """报告页是只读视图：报告页脚本不调研究/恢复接口，也不写 outbox。"""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    block = _report_block(html)
    assert "mode-report" in html
    assert "loadReport(" in block
    assert "/api/research" not in block
    assert "/api/resume" not in block
    assert "/api/config" not in block


def test_report_page_wraps_citations_for_evidence_linking():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    block = _report_block(html)
    assert "function wrapCitations" in block
    assert "function makeCite" in block
    assert "data-code" in block
    assert "function bindReportInteractions" in block
    # 两种引用写法都要覆盖：[S1](url) 链接式与裸 [S1]
    assert "anchor.textContent" in block
    assert "createTreeWalker" in block


def test_report_page_has_reading_tools():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    block = _report_block(html)
    assert 'id="font-picker"' in html
    assert "agent-report-font" in block
    assert "window.print()" in block
    print_css = html[html.index("@media print"):]
    assert "report-toc" in print_css[:2000], "打印时应该隐藏目录与侧栏"


def test_workspace_links_to_report_page():
    """工作区终稿卡要有进入报告页的入口。"""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="open-report"' in html
    assert "encodeURIComponent(threadId)" in html
    tail = html[html.index('$("open-report").addEventListener'):]
    assert '"/report/"' in tail[:220]
