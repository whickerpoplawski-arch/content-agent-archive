"""Dreaming ops, fuzzy dedup, and SKILL.md promotion."""

from __future__ import annotations

import asyncio
import json

from app.agent.consolidation import (
    DreamOp,
    apply_dream_ops,
    consolidate,
    list_active,
)
from app.agent.llm import MockLLMBackend
from app.agent.memory import LongTermMemory
from app.agent.skills_loader import load_skill, promote_insight, score_skill, skill_index_text, validate_skill_text
from app.agent.skills_loader import discover
from app.core.config import settings


def test_near_duplicate_supersedes_and_keeps_one_active(tmp_path):
    memory = LongTermMemory(tmp_path / "profile.json")
    first = DreamOp(
        op="ADD",
        title="先分析后直出",
        insight="对美妆类选题，分析后直接生成效果更好。",
        applies_to=["美妆"],
        confidence=0.6,
    )
    second = DreamOp(
        op="ADD",
        title="分析后直出更稳",
        insight="对美妆类选题，分析后直接生成效果更好。",
        applies_to=["美妆"],
        confidence=0.92,
    )
    apply_dream_ops(memory, [first], "美妆目标")
    apply_dream_ops(memory, [second], "美妆目标")
    active = list_active(memory)
    assert len(active) == 1
    assert active[0]["title"] == "分析后直出更稳"
    raw = memory.get("procedural_insights")
    assert len(raw) == 2
    assert any(item.get("lifecycle") == "superseded" for item in raw)


def test_exact_title_updates_hits_without_a_second_row(tmp_path):
    memory = LongTermMemory(tmp_path / "profile.json")
    op = DreamOp(op="ADD", title="同一标题", insight="做法 A", confidence=0.4)
    apply_dream_ops(memory, [op], "g")
    apply_dream_ops(memory, [op], "g")
    items = memory.get("procedural_insights")
    assert len(items) == 1
    assert items[0]["hits"] == 2


def test_legacy_insight_migrates_on_read(tmp_path):
    memory = LongTermMemory(tmp_path / "profile.json")
    memory.set("procedural_insights", [{"title": "旧洞察", "insight": "做法", "applies_to": ["美妆"]}])
    active = list_active(memory)
    assert active[0]["lifecycle"] == "active"
    assert active[0]["id"].startswith("pins_")
    raw = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert raw["procedural_insights"][0]["lifecycle"] == "active"


def test_consolidation_failure_does_not_raise(tmp_path):
    class Exploding:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("boom")

    memory = LongTermMemory(tmp_path / "profile.json")
    stored = asyncio.run(consolidate(Exploding(), memory, "goal", [], {}))
    assert stored is None


def test_promote_writes_valid_skill_and_rejects_bad_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_skills_enabled", True)
    monkeypatch.setattr(settings, "agent_skills_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_skill_promote_confidence", 0.8)
    monkeypatch.setattr(settings, "agent_skill_promote_hits", 2)
    insight = {
        "id": "pins_abc",
        "title": "offline sample",
        "insight": "Use load_notes when the corpus is already local.",
        "when_to_use": "Use when the goal says 离线 or 样本.",
        "applies_to": ["离线"],
        "confidence": 0.91,
        "hits": 2,
        "lifecycle": "active",
    }
    path = promote_insight(insight)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert validate_skill_text(text, directory_name=path.parent.name) == []
    assert "name: offline-sample" in text

    rejected = promote_insight({**insight, "skill_name": "Bad Name", "hits": 5})
    assert rejected is None
    assert not (tmp_path / "Bad Name").exists()


def test_skill_index_hides_body_until_load(monkeypatch):
    monkeypatch.setattr(settings, "agent_skills_enabled", True)
    monkeypatch.setattr(settings, "agent_skills_dir", "skills")
    text = skill_index_text("用离线样本做演示，不要爬取")
    assert "offline-sample-pipeline" in text
    assert "不要默认 crawl_notes" not in text
    loaded = load_skill("offline-sample-pipeline")
    assert "不要默认 crawl_notes" in loaded["body"]
    docs = discover()
    ranked = {doc.name: score_skill("离线样本演示", doc) for doc in docs}
    assert ranked["offline-sample-pipeline"] > score_skill("量子场论论文", docs[0])


def test_mock_backend_still_stores_sample_insight(tmp_path):
    from app.agent.consolidation import DreamResult
    from app.agent.mock_factories import build_mock_backend

    backend = build_mock_backend()
    assert DreamResult in backend._factories
    memory = LongTermMemory(tmp_path / "profile.json")
    stored = asyncio.run(
        consolidate(backend, memory, "为大学生生成文案", ["load_notes"], {"results": []})
    )
    assert stored is not None
    assert stored["title"] == "样本数据链路直出即可"
    assert stored["lifecycle"] == "active"
