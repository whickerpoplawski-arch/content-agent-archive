"""Tests for dreaming-style background consolidation (IMPROVEMENT.md §2.3 阶三).

Covers: insight distillation + store, dedup (SKIP / duplicate title), prompt
injection via _profile_text, and non-fatal failure on backend error.
"""

from __future__ import annotations

import asyncio

from app.agent.consolidation import ProceduralInsight, consolidate
from app.agent.llm import MockLLMBackend
from app.agent.memory import LongTermMemory


def _backend(title="美妆选题先分析后直出效果更好") -> MockLLMBackend:
    backend = MockLLMBackend()
    backend.register(
        ProceduralInsight,
        lambda: ProceduralInsight(
            title=title,
            insight="对美妆类选题，analyze 后直接 topics 效果更好，少一轮重规划。",
            applies_to=["美妆", "护肤"],
        ),
    )
    return backend


def test_consolidate_stores_new_insight(tmp_path):
    memory = LongTermMemory(tmp_path / "profile.json")
    stored = asyncio.run(
        consolidate(_backend(), memory, "为美妆学生生成文案", ["analyze_notes"], {"results": []})
    )
    assert stored is not None
    items = memory.get("procedural_insights")
    assert len(items) == 1
    assert items[0]["title"] == "美妆选题先分析后直出效果更好"
    assert items[0]["applies_to"] == ["美妆", "护肤"]


def test_consolidate_dedups_same_title(tmp_path):
    memory = LongTermMemory(tmp_path / "profile.json")
    for _ in range(2):
        asyncio.run(
            consolidate(_backend(), memory, "为美妆学生生成文案", ["analyze_notes"], {})
        )
    assert len(memory.get("procedural_insights")) == 1


def test_consolidate_skips_placeholder(tmp_path):
    memory = LongTermMemory(tmp_path / "profile.json")
    stored = asyncio.run(
        consolidate(_backend("SKIP"), memory, "goal", [], {})
    )
    assert stored is None
    assert memory.get("procedural_insights") is None


def test_consolidate_survives_backend_error(tmp_path):
    class ExplodingBackend:
        async def generate(self, *a, **k):
            raise RuntimeError("boom")

    memory = LongTermMemory(tmp_path / "profile.json")
    stored = asyncio.run(
        consolidate(ExplodingBackend(), memory, "goal", [], {})
    )
    assert stored is None  # 非致命:返回 None 不抛


def test_profile_text_injects_insights():
    from app.agent.orchestrator import _profile_text

    profile = {
        "procedural_insights": [
            {"title": "美妆先分析后直出", "insight": "少一轮重规划", "applies_to": ["美妆"]}
        ]
    }
    txt = _profile_text(profile, goal="为美妆学生生成文案")
    assert "Distilled insights" in txt
    assert "美妆先分析后直出" in txt


def test_run_appends_insight_after_mock_run(tmp_path, monkeypatch):
    """端到端: mock agent.run 后 profile.json 出现提炼条目。"""
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.mock_factories import build_mock_backend, build_mock_registry
    from app.agent.orchestrator import XHSAgent

    monkeypatch.chdir(tmp_path)
    long_term = LongTermMemory(tmp_path / "profile.json")
    agent = XHSAgent(
        backend=build_mock_backend(), registry=build_mock_registry()
    )
    asyncio.run(
        agent.run(
            "为大学生女生生成3篇小红书种草文案",
            memory=AgentMemory(long_term),
        )
    )
    insights = long_term.get("procedural_insights")
    assert insights and insights[0]["title"] == "样本数据链路直出即可"
