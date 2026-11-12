"""一键启用启动页测试：web.py 路由、页面里的深链、启动脚本。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import web

ROOT = Path(__file__).resolve().parents[1]


def test_home_serves_launch_page():
    """启动 web.py 后打开根路径就应该看到“一键启用”，否则用户以为没生效。"""
    client = TestClient(web.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "一键启用" in resp.text
    assert 'id="launch-btn"' in resp.text


def test_launch_route_serves_one_click_page():
    client = TestClient(web.app)
    resp = client.get("/launch")
    assert resp.status_code == 200
    assert "一键启用" in resp.text
    # 一键启用 = 打开主界面并自动弹出模型设置
    assert "./index.html?settings=1" in resp.text
    # 页面要探测后端状态，未启动时给出启动方式
    assert "api/health" in resp.text
    assert "python web.py" in resp.text or "start.bat" in resp.text


def test_launch_page_is_standalone_and_links_to_main_ui():
    html = (ROOT / "launch.html").read_text(encoding="utf-8")
    assert "./index.html?settings=1" in html
    assert 'href="./index.html"' in html
    assert "?history=1" in html
    # 双击（file://）打开时没有后端，必须降级为静态演示而不是死链
    assert 'location.protocol === "file:"' in html
    assert "DEMO_URL" in html


def test_main_ui_at_index_html_and_app():
    client = TestClient(web.app)
    for path in ("/index.html", "/app"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert 'id="progress-card"' in resp.text, path


def test_start_script_launches_web_and_opens_launch_page():
    bat = (ROOT / "start.bat").read_text(encoding="utf-8", errors="ignore")
    assert "web.py" in bat
    assert "AGENT_WEB_PORT" in bat
    assert "/index.html" in bat, "脚本要告诉用户主界面在哪"
    assert ".venv" in bat, "优先用项目虚拟环境里的 Python"
