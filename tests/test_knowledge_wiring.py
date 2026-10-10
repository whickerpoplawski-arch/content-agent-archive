"""Tests for knowledge wiring: auto-ingest + API + planner context (R3/R4)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agent.knowledge import KnowledgeStore
from app.agent.tools import CrawlParams, Tool, ToolRegistry
from app.main import app

client = TestClient(app)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from app.agent.embedder import get_embedder

    try:
        get_embedder()
    except FileNotFoundError:
        pytest.skip("embedding model not present in data/models/")
    s = KnowledgeStore(path=tmp_path / "knowledge.db")
    monkeypatch.setattr("app.agent.knowledge._store", s)
    yield s
    monkeypatch.undo()


# ---------- R3: API ----------


def test_knowledge_ingest_and_search_api(store):
    r = client.post(
        "/agent/knowledge",
        json={"text": "西电研究生秋招经验：项目经历要提前准备。", "title": "秋招", "source": "manual"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r2 = client.get("/agent/knowledge/search", params={"q": "秋招怎么准备", "top_k": 2})
    assert r2.status_code == 200
    hits = r2.json()["hits"]
    assert hits and "秋招" in hits[0]["title"]


def test_knowledge_ingest_empty_rejected(store):
    r = client.post("/agent/knowledge", json={"text": "  "})
    assert r.status_code == 422


def test_knowledge_stats(store):
    r = client.get("/agent/knowledge")
    assert r.status_code == 200
    assert "documents" in r.json()


# ---------- R3: run 产物自动入库 ----------


def test_outputs_auto_ingest(store):
    from app.agent.orchestrator import _ingest_outputs_to_knowledge

    outputs = {
        "analysis_summary": "高频关键词: 平价好物、宿舍。",
        "results": [
            {"title": "宿舍收纳清单", "body": "床底收纳箱与门后挂钩最实用。", "hashtags": ["#收纳"]}
        ],
    }
    _ingest_outputs_to_knowledge("为大学生生成收纳文案", outputs)
    assert store.count() == 2  # 1 analysis + 1 output
    _ingest_outputs_to_knowledge("为大学生生成收纳文案", outputs)  # 重放幂等
    assert store.count() == 2

    hits = store.search("宿舍收纳用什么好", top_k=2)
    assert hits
    assert hits[0]["source"] in ("analysis", "output")


def test_crawl_results_auto_ingest(store, monkeypatch):
    """crawl_notes 成功 -> 笔记入知识库 (executor 钩子)."""
    from app.agent.executor import Executor
    from app.agent.memory import AgentMemory
    from app.agent.planner import Reflector
    from app.agent.tools import ToolRegistry

    captured = {}

    async def fake_crawl(params):
        return {
            "count": 2,
            "items": [
                {"title": "宿舍收纳大法", "content": "床底收纳箱超好用", "tags": ["收纳"], "url": "u1"},
                {"title": "护肤避雷", "content": "这些雷品别买", "tags": ["护肤"], "url": "u2"},
            ],
        }

    registry = ToolRegistry()
    registry.register(
        Tool(name="crawl_notes", description="crawl", params_schema=CrawlParams, handler=fake_crawl)
    )
    ex = Executor(registry, Reflector(None), None)
    ex._current_goal = "宿舍收纳调研"

    result = {"count": 2, "items": [
        {"title": "宿舍收纳大法", "content": "床底收纳箱超好用", "tags": ["收纳"], "url": "u1"},
        {"title": "护肤避雷", "content": "这些雷品别买", "tags": ["护肤"], "url": "u2"},
    ]}
    ex._track_crawl_result(result)
    assert store.count() == 2
    hits = store.search("床底收纳", top_k=1)
    assert hits and hits[0]["metadata"].get("url") == "u1"


# ---------- R4: planner 知识上下文注入 ----------


def test_knowledge_context_injection(store):
    from app.agent.orchestrator import _knowledge_context

    store.add("西电实习内推渠道：导师推荐和学长内推最有效。", title="内推", source="manual")
    ctx = _knowledge_context("为西电学生生成实习分享文案")
    # v2 注入: 小库走 digest 模式(头行为 Knowledge base digest), 大库走检索
    # 模式(头行为 Relevant knowledge); 两种模式条目统一 K 编号。
    assert "Knowledge base digest" in ctx or "Relevant knowledge" in ctx
    assert "[K1|manual]" in ctx
    assert "内推" in ctx


def test_knowledge_context_empty_when_no_store_content(store):
    from app.agent.orchestrator import _knowledge_context

    assert _knowledge_context("任何 goal") == ""


def test_knowledge_context_survives_store_error(monkeypatch):
    from app.agent import orchestrator

    def boom(*a, **k):
        raise RuntimeError("db closed")

    monkeypatch.setattr("app.agent.knowledge.get_knowledge_store", boom)
    assert orchestrator._knowledge_context("goal") == ""
