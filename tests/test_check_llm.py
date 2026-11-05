"""接入真实大模型的配置自检（不发起真实网络请求）。"""

from __future__ import annotations

import check_llm


def test_mask_secret_hides_key():
    assert check_llm.mask_secret("") == "（空）"
    assert check_llm.mask_secret("sk-1234567890abcdef").startswith("sk-123")
    assert "1234567890abcdef" not in check_llm.mask_secret("sk-1234567890abcdef")


def test_resolve_config_is_offline_without_key(monkeypatch, tmp_path):
    monkeypatch.setattr(check_llm, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("AGENT_LLM_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = check_llm.resolve_config()
    assert config["mode"] == "offline"
    assert config["model"]


def test_resolve_config_falls_back_offline_when_forced_openai_but_no_key(monkeypatch, tmp_path):
    monkeypatch.setattr(check_llm, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("AGENT_LLM_MODE", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert check_llm.resolve_config()["mode"] == "offline"


def test_check_llm_returns_2_in_offline_mode(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(check_llm, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setattr(check_llm, "EXAMPLE_FILE", tmp_path / "missing.example")
    monkeypatch.setenv("AGENT_LLM_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert check_llm.main(["--question", "测试问题"]) == 2
    output = capsys.readouterr().out
    assert "离线模式" in output
    assert "OPENAI_API_KEY" in output


def test_write_env_values_updates_file_and_preserves_comments(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# 注释\nOPENAI_API_KEY=\nOPENAI_MODEL=old\n", encoding="utf-8")
    monkeypatch.setattr(check_llm, "ENV_FILE", env_file)
    monkeypatch.setattr(check_llm, "EXAMPLE_FILE", tmp_path / "missing.example")
    check_llm._write_env_values({"OPENAI_API_KEY": "sk-test-123456", "OPENAI_MODEL": "deepseek-chat"})
    text = env_file.read_text(encoding="utf-8")
    assert "# 注释" in text
    assert "OPENAI_API_KEY=sk-test-123456" in text
    assert "OPENAI_MODEL=deepseek-chat" in text
    assert "OPENAI_MODEL=old" not in text


def test_setup_cli_writes_base_url_and_model(monkeypatch, capsys, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(check_llm, "ENV_FILE", env_file)
    monkeypatch.setattr(check_llm, "EXAMPLE_FILE", tmp_path / "missing.example")
    assert check_llm.main(["--base-url", "https://api.deepseek.com/v1", "--model", "deepseek-chat"]) == 0
    text = env_file.read_text(encoding="utf-8")
    assert "OPENAI_BASE_URL=https://api.deepseek.com/v1" in text
    assert "OPENAI_MODEL=deepseek-chat" in text
    assert "已写入" in capsys.readouterr().out
