"""Web 端模型配置接口测试：Key 只存内存、不回传明文、支持测试连接与清除。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import llm_client
import web

KEY = "sk-test-1234567890"
BASE = "https://api.example.com/v1"
MODEL = "test-model"


@pytest.fixture(autouse=True)
def _clean_runtime_config():
    web._clear_runtime_config()
    llm_client.reset_llm()
    yield
    web._clear_runtime_config()
    llm_client.reset_llm()


def test_config_view_never_returns_plaintext_key():
    client = TestClient(web.app)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "api_key" not in body
    assert body["masked_key"] == "" or "****" in body["masked_key"]
    assert KEY not in resp.text


def test_save_config_returns_masked_key_and_health_updates():
    client = TestClient(web.app)
    resp = client.post(
        "/api/config",
        json={"api_key": KEY, "base_url": BASE, "model": MODEL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["mode"] == "openai"
    assert body["model"] == MODEL
    assert body["base_url"] == BASE
    assert body["masked_key"] == "sk-****7890"
    assert KEY not in resp.text

    # 再次读取也不能带明文；source 标为 runtime
    again = client.get("/api/config").json()
    assert again["source"] == "runtime"
    assert KEY not in str(again)

    health = client.get("/api/health").json()
    assert health["llm_mode"] == "openai"
    assert health["model"] == MODEL
    # 后续节点通过 get_llm() 取客户端，应已切到新配置
    active = llm_client.get_llm()
    assert active.model == MODEL
    assert active.api_key == KEY


def test_test_connection_uses_provided_config_without_saving(monkeypatch):
    monkeypatch.setattr(
        llm_client.LLMClient,
        "ping",
        lambda self: {"ok": True, "model": self.model, "latency_ms": 12, "reply": "OK"},
    )
    client = TestClient(web.app)
    resp = client.post(
        "/api/config/test",
        json={"api_key": KEY, "base_url": BASE, "model": MODEL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["latency_ms"] == 12
    assert KEY not in resp.text

    # 测试连接不应改变当前配置
    assert client.get("/api/config").json()["source"] != "runtime"


def test_invalid_base_url_rejected():
    client = TestClient(web.app)
    resp = client.post(
        "/api/config",
        json={"api_key": KEY, "base_url": "ftp://example.com", "model": MODEL},
    )
    assert resp.status_code == 400
    assert "Base URL" in resp.json()["detail"]


def test_clear_runtime_config_restores_startup_config():
    client = TestClient(web.app)
    client.post("/api/config", json={"api_key": KEY, "base_url": BASE, "model": MODEL})
    resp = client.delete("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] != "runtime"
    assert KEY not in resp.text


def test_test_connection_error_never_echoes_key(monkeypatch):
    def boom(self):
        raise RuntimeError("认证失败：" + KEY)

    monkeypatch.setattr(llm_client.LLMClient, "ping", boom)
    client = TestClient(web.app)
    resp = client.post(
        "/api/config/test",
        json={"api_key": KEY, "base_url": BASE, "model": MODEL},
    )
    assert resp.status_code == 400
    assert KEY not in resp.text
    assert "***" in resp.json()["detail"]


def test_index_contains_masked_settings_panel():
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    assert 'id="settings-modal"' in html
    assert 'id="api-key"' in html and 'type="password"' in html
    assert "后端进程内存" in html
    assert "theme-dark" in html


def test_index_progress_ui_has_segments_and_collapse_toggle():
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    assert 'id="seg-bar"' in html, "执行进度需要分段进度条容器"
    assert 'id="progress-body"' in html and 'id="progress-toggle"' in html, "执行进度需要可收纳"
    assert 'id="stage-copy"' in html and 'id="stage-percent"' in html, "需要阶段文案与百分比"
    assert 'aria-valuenow' in html, "进度条要有无障碍语义"


def test_progress_node_order_matches_graph_nodes():
    """前端进度条的 14 个节点必须与图节点顺序一致，否则百分比会算错。"""
    import re

    from graph import _node_functions

    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    block = re.search(r"const NODE_ORDER = \[(.*?)\];", html, re.S)
    assert block, "index.html 里应有 NODE_ORDER 常量"
    names = re.findall(r'"([a-z_]+)"', block.group(1))
    assert names == list(_node_functions())
