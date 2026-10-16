"""OpenRouter wiring stays env-only and skips live calls without a key."""

from __future__ import annotations

import os

from app.agent.llm import _loads_json_object, openrouter_config, openrouter_model_name


def test_openrouter_config_empty_without_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    cfg = openrouter_config()
    assert cfg["api_key"] == ""
    assert cfg["base_url"] == "https://openrouter.ai/api/v1"
    assert cfg["model"] == "openrouter/free"
    assert openrouter_model_name() == ""
    assert "sk-" not in json_dump(cfg)


def test_openrouter_config_reads_env_only(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
    cfg = openrouter_config()
    assert cfg["api_key"] == "test-key-not-real"
    assert cfg["model"] == "openrouter/free"
    assert os.environ["OPENROUTER_API_KEY"] == "test-key-not-real"


def test_loads_json_object_strips_fence():
    data = _loads_json_object('```json\n{"ok": true, "echo": "pong"}\n```')
    assert data == {"ok": True, "echo": "pong"}


def json_dump(cfg: dict) -> str:
    return " ".join(cfg.values())
