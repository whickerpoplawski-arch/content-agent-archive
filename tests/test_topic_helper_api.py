"""Smoke tests for the topic-helper demo page and POST /topics/helper."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agent.tools import DEMO_DENYLIST
from app.main import app


def test_demo_page_is_served():
    client = TestClient(app)
    response = client.get("/demo/topic-helper")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "选题工具调用演示" in body
    assert ">生成<" in body
    assert 'value="mock" selected' in body
    assert "/topics/helper" in body
    assert "publish_to_xhs" not in body
    assert "crawl_notes" not in body


def test_helper_api_mock_returns_angles_and_trace(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.topic_helper.TRACE_DIR", tmp_path)
    client = TestClient(app)
    response = client.post(
        "/topics/helper",
        json={"theme": "大学生平价通勤穿搭", "mode": "mock", "count": 4},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert 3 <= len(data["angles"]) <= 5
    steps = data["trace"]["steps"]
    assert steps
    tools = [step["tool"] for step in steps]
    assert "generate_topics" in tools
    assert set(tools).isdisjoint(DEMO_DENYLIST)
    assert data["meta"]["mode"] == "mock"


def test_helper_api_empty_theme_refuses_without_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.topic_helper.TRACE_DIR", tmp_path)
    client = TestClient(app)
    response = client.post("/topics/helper", json={"theme": "  ", "mode": "mock"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "refuse"
    assert data["reason"] == "empty_theme"
    assert data["angles"] == []
    assert data["trace"]["steps"] == []


def test_helper_api_vague_theme_clarifies(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.topic_helper.TRACE_DIR", tmp_path)
    client = TestClient(app)
    response = client.post("/topics/helper", json={"theme": "爆款", "mode": "mock"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "clarify"
    assert data["angles"] == []
    assert data["trace"]["steps"] == []
    assert 1 <= len(data["questions"]) <= 2


def test_helper_api_drops_blacklist_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.topic_helper.TRACE_DIR", tmp_path)
    client = TestClient(app)
    response = client.post(
        "/topics/helper",
        json={
            "theme": "宿舍护肤",
            "mode": "mock",
            "allow_tools": ["generate_topics", "crawl_notes", "publish_to_xhs"],
        },
    )
    assert response.status_code == 200
    tools = [step["tool"] for step in response.json()["trace"]["steps"]]
    assert tools == ["generate_topics"]
