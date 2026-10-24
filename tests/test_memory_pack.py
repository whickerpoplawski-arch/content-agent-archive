"""Ephemeral pack trimming, topic recall, tools, and dream candidates."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.agent.checkpoint import StepCheckpointer
from app.agent.memory import AgentMemory, LongTermMemory
from app.agent.memory_pack import build_pack
from app.agent.memory_store import ProfileStore, bind_store, reset_store
from app.agent.schemas import Plan, PlanStep
from app.core.config import Settings, settings

client = TestClient(__import__("app.main", fromlist=["app"]).app)


def _store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "memory.db")


def test_pack_flag_defaults_on():
    assert Settings.model_fields["agent_memory_pack_enabled"].default is True
    assert Settings.model_fields["agent_memory_pack_budget_tokens"].default == 1200


def test_pinned_veto_survives_tiny_budget(tmp_path):
    store = _store(tmp_path)
    store.learn("NEVER use exaggerated marketing claims", kind="veto", pinned=True)
    for index in range(12):
        store.learn(
            f"situational padding {index} " + ("word " * 40),
            kind="preference",
            lane="situational",
            topics=["宿舍好物"],
        )
    pack = build_pack(
        "宿舍好物推荐",
        store=store,
        budget_tokens=80,
        general_tokens=40,
        situational_tokens=30,
        episodic_tokens=20,
        profile={"audience": "宿舍党", "tone": "克制"},
    )
    assert "NEVER use exaggerated marketing claims" in pack.text
    assert "宿舍党" in pack.text
    assert pack.dropped
    assert "<memory-context" in pack.text
    assert 'ephemeral="true"' in pack.text
    assert "situational padding 11" not in pack.text or any(
        "situational padding" in item for item in pack.dropped
    )


def test_knowledge_budget_does_not_evict_veto(tmp_path):
    store = _store(tmp_path)
    store.learn("NEVER use exaggerated marketing claims", kind="veto", pinned=True)
    knowledge = "K-doc " * 200
    pack = build_pack(
        "宿舍好物",
        store=store,
        knowledge_text=knowledge,
        budget_tokens=40,
        general_tokens=30,
        situational_tokens=10,
        episodic_tokens=10,
    )
    assert "NEVER use exaggerated marketing claims" in pack.text
    assert "K-doc" in pack.text
    # Knowledge sits outside the memory-context tag.
    memory, _, docs = pack.text.partition("</memory-context>")
    assert "K-doc" not in memory
    assert "K-doc" in docs


def test_situational_recall_is_topic_aware(tmp_path):
    store = _store(tmp_path)
    store.learn("美妆文案先写成分再写感受", lane="situational", topics=["美妆"], kind="procedure")
    store.learn("宿舍好物开头要写具体场景", lane="situational", topics=["宿舍好物"], kind="procedure")
    pack = build_pack("写一篇宿舍好物清单", store=store, budget_tokens=400)
    assert "宿舍好物开头" in pack.text
    assert "美妆文案" not in pack.text


def test_forget_drops_the_line_from_the_pack(tmp_path):
    store = _store(tmp_path)
    row = store.learn("口吻保持克制", kind="preference")
    assert "口吻保持克制" in build_pack("任意目标", store=store).text
    store.forget(row["id"])
    assert "口吻保持克制" not in build_pack("任意目标", store=store).text


def test_pack_shows_migrated_audience_tone_and_veto(tmp_path):
    store = _store(tmp_path)
    store.migrate_profile(
        {
            "audience": "西电学生",
            "tone": "真实克制",
            "user_facts": {
                "a": {
                    "target_domains": ["宿舍好物"],
                    "preferred_tone": "真实克制",
                    "preferred_structure": "",
                    "avoid_topics": ["夸张营销"],
                },
                "b": {
                    "target_domains": ["宿舍好物"],
                    "preferred_tone": "真实克制",
                    "preferred_structure": "",
                    "avoid_topics": ["夸张营销"],
                },
            },
        }
    )
    pack = build_pack("宿舍好物", store=store)
    assert "西电学生" in pack.text
    assert "真实克制" in pack.text
    assert "夸张营销" in pack.text
    assert pack.user_id == "local"


def test_checkpoint_strips_ephemeral_pack(tmp_path):
    pack = build_pack("goal", store=_store(tmp_path), profile={"audience": "学生"})
    checkpointer = StepCheckpointer(tmp_path / "checkpoints")
    plan = Plan(goal="g", steps=[PlanStep(action="load_notes")])
    checkpointer.save(
        "run1",
        plan,
        [
            {
                "action": "load_notes",
                "params": {"note": pack.text},
                "result": pack.text,
                "status": "ok",
            }
        ],
        {},
    )
    text = (tmp_path / "checkpoints" / "run1.json").read_text(encoding="utf-8")
    assert "<memory-context" not in text
    assert "ephemeral memory pack omitted" in text


def test_tools_learn_forget_recall_pin(tmp_path):
    from app.agent.tools import build_default_registry

    store = _store(tmp_path)
    token = bind_store(store)
    try:
        registry = build_default_registry()
        learned = asyncio.run(
            registry.call(
                "memory_learn",
                {"text": "不要写夸张营销", "kind": "veto", "pinned": True},
            )
        )
        assert learned["ok"] is True
        learning_id = learned["learning"]["id"]
        recalled = asyncio.run(registry.call("memory_recall", {"query": "夸张营销", "limit": 5}))
        assert recalled["ok"] is True
        assert any(row["id"] == learning_id for row in recalled["learnings"])
        unpinned = asyncio.run(
            registry.call("memory_pin", {"learning_id": learning_id, "pinned": False})
        )
        assert unpinned["learning"]["pinned"] is False
        refused = asyncio.run(
            registry.call(
                "memory_learn",
                {"text": "api_key=sk-abcdefghijklmnopqrstuvwxyz"},
            )
        )
        assert refused["ok"] is False
        assert refused["refused"] is True
        assert store.get(learning_id)["status"] == "active"
        forgotten = asyncio.run(registry.call("memory_forget", {"learning_id": learning_id}))
        assert forgotten["ok"] is True
        assert store.get(learning_id)["status"] == "forgotten"
        assert "不要写夸张营销" not in build_pack("营销文案", store=store).text
    finally:
        reset_store(token)


def test_memory_http_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.agent.memory.LongTermMemory",
        lambda *a, **k: LongTermMemory(tmp_path / "profile.json"),
    )
    created = client.post(
        "/agent/memory/learn",
        json={"text": "品牌红线：不写绝对化用语", "kind": "veto", "pinned": True},
    )
    assert created.status_code == 200, created.text
    learning_id = created.json()["learning"]["id"]
    listed = client.get("/agent/memory", params={"status": "active"})
    assert listed.status_code == 200
    assert any(item["id"] == learning_id for item in listed.json()["items"])
    recalled = client.get("/agent/memory/recall", params={"q": "绝对化"})
    assert recalled.status_code == 200
    assert recalled.json()["learnings"]
    refused = client.post(
        "/agent/memory/learn",
        json={"text": "password=super-secret-value"},
    )
    assert refused.status_code == 400
    forgotten = client.post("/agent/memory/forget", json={"learning_id": learning_id})
    assert forgotten.status_code == 200
    listed_again = client.get("/agent/memory", params={"status": "active"})
    assert all(item["id"] != learning_id for item in listed_again.json()["items"])


def test_dream_writes_candidate_not_injected(tmp_path):
    import asyncio as aio

    from app.agent.consolidation import ProceduralInsight, consolidate
    from app.agent.llm import MockLLMBackend

    backend = MockLLMBackend()
    backend.register(
        ProceduralInsight,
        lambda: ProceduralInsight(
            title="美妆选题先分析后直出",
            insight="对美妆类选题，analyze 后直接 topics 效果更好。",
            applies_to=["美妆"],
        ),
    )
    memory = LongTermMemory(tmp_path / "profile.json")
    stored = aio.run(
        consolidate(backend, memory, "为美妆学生生成文案", ["analyze_notes"], {"results": []})
    )
    assert stored is not None
    rows = memory.store.list_learnings(kind="procedure")
    dream_rows = [row for row in rows if row["source"] == "dream"]
    assert dream_rows
    assert dream_rows[0]["status"] == "candidate"
    pack = build_pack("为美妆学生生成文案", store=memory.store, profile=memory.data)
    assert "analyze 后直接 topics" not in pack.text
    assert memory.store.cursor()["revision"] >= 1


def test_run_pack_is_not_checkpoint_or_trace_speech(tmp_path, monkeypatch):
    from app.agent.mock_factories import build_mock_backend, build_mock_registry
    from app.agent.orchestrator import XHSAgent

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "agent_memory_pack_enabled", True)
    long_term = LongTermMemory(tmp_path / "profile.json")
    long_term.store.learn("UNIQUE_PACK_MARKER_9f3a 保持克制", kind="veto", pinned=True)
    memory = AgentMemory(long_term)

    class RecordingCheckpoint:
        def __init__(self):
            self.blobs: list[str] = []

        def load(self, run_id):
            return None

        def save(self, run_id, plan, memory_steps, usage, memory_meta=None):
            self.blobs.append(
                json.dumps(
                    {"steps": memory_steps, "meta": memory_meta or {}},
                    ensure_ascii=False,
                    default=str,
                )
            )

        def clear(self, run_id):
            return None

    agent = XHSAgent(backend=build_mock_backend(), registry=build_mock_registry())
    captured: list[str] = []
    original = agent.planner.plan

    async def _capture(goal, profile, **kwargs):
        captured.append(profile)
        return await original(goal, profile, **kwargs)

    agent.planner.plan = _capture
    checkpoint = RecordingCheckpoint()
    asyncio.run(
        agent.run(
            "为大学生生成3篇宿舍好物文案",
            memory=memory,
            checkpointer=checkpoint,
        )
    )
    assert captured
    assert "UNIQUE_PACK_MARKER_9f3a" in captured[0]
    assert "<memory-context" in captured[0]
    assert all("UNIQUE_PACK_MARKER_9f3a" not in blob for blob in checkpoint.blobs)
    assert all("<memory-context" not in blob for blob in checkpoint.blobs)
    trace_files = list((tmp_path / "data" / "agent" / "runs").glob("*.json"))
    assert trace_files
    for path in trace_files:
        text = path.read_text(encoding="utf-8")
        assert "UNIQUE_PACK_MARKER_9f3a" not in text
        assert "<memory-context" not in text
