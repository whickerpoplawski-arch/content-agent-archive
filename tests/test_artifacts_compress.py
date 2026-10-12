"""Artifact offload and async observation compression."""

from __future__ import annotations

import asyncio
import json

from app.agent.artifacts import is_artifact_handle, load_artifact, maybe_offload, read_artifact_view
from app.agent.compress import ObsDigest, compress_observations
from app.agent.context_builder import ContextPart, assemble, count_tokens
from app.agent.executor import resolve_params
from app.agent.memory import AgentMemory, LongTermMemory
from app.core.config import settings


def test_large_result_offloaded_and_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_artifact_offload_enabled", True)
    monkeypatch.setattr(settings, "agent_artifacts_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_artifact_chars", 8000)
    payload = {"items": [{"content": "x" * 200} for _ in range(80)], "count": 80}
    assert len(json.dumps(payload)) > 8000
    memory = AgentMemory(LongTermMemory(tmp_path / "profile.json"))
    handle = maybe_offload("run1", 3, "crawl_notes", payload)
    memory.record("crawl_notes", {}, handle, "succeeded")
    assert is_artifact_handle(memory.latest("crawl_notes"))
    assert (tmp_path / "run1").exists()
    obs = memory.observations()
    assert "x" * 200 not in obs
    view = read_artifact_view(handle["artifact"], fields=["count"], limit=5)
    assert view["data"]["count"] == 80
    loaded = load_artifact(handle["artifact"])
    assert loaded["count"] == 80


def test_read_artifact_refuses_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_artifacts_dir", str(tmp_path))
    outside = tmp_path.parent / "secret.json"
    outside.write_text('{"ok": false}', encoding="utf-8")
    try:
        load_artifact(str(outside))
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_offload_flag_off_keeps_inline_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_artifact_offload_enabled", False)
    monkeypatch.setattr(settings, "agent_artifacts_dir", str(tmp_path))
    payload = {"blob": "y" * 9000}
    stored = maybe_offload("run2", 0, "analyze_notes", payload)
    assert stored is payload
    assert list(tmp_path.glob("**/*.json")) == []


def test_resolve_params_does_not_expand_whole_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_artifact_offload_enabled", True)
    monkeypatch.setattr(settings, "agent_artifacts_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_artifact_chars", 100)
    payload = {"items": ["n" * 80 for _ in range(10)], "count": 10}
    memory = AgentMemory(LongTermMemory(tmp_path / "profile.json"))
    handle = maybe_offload("run3", 1, "crawl_notes", payload)
    memory.record("crawl_notes", {}, handle, "succeeded")
    whole = asyncio.run(resolve_params({"notes": "$latest.crawl_notes"}, memory))
    assert whole["notes"].get("_artifact_handle") is True
    field = asyncio.run(resolve_params({"n": "$latest.crawl_notes.count"}, memory))
    assert field["n"] == 10
    huge = asyncio.run(resolve_params({"items": "$latest.crawl_notes.items"}, memory))
    assert huge["items"].get("refused_expand") is True


def test_compress_lowers_builder_tokens_and_uses_async_generate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_obs_compress_enabled", True)
    monkeypatch.setattr(settings, "agent_obs_keep_raw", 4)
    monkeypatch.setattr(settings, "agent_obs_compress_every_n", 12)
    memory = AgentMemory(LongTermMemory(tmp_path / "profile.json"))
    for index in range(30):
        memory.record("analyze_notes", {}, {"summary": f"step-{index}-" + ("detail " * 40)}, "succeeded")
    before_text = memory.observations(full=True)
    before = count_tokens(before_text)

    class Backend:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, schema, **kwargs):
            self.calls += 1
            assert schema is ObsDigest
            return ObsDigest(
                goal_restated="demo",
                completed_actions=["analyze_notes"],
                key_numbers={"passed": 3},
                failures=[],
                artifact_paths=[],
                next_focus="generate",
            )

    class Tracer:
        def __init__(self):
            self.events = []

        def event(self, kind, **fields):
            self.events.append((kind, fields))

    backend = Backend()
    tracer = Tracer()
    info = asyncio.run(compress_observations(memory, backend, goal="demo goal", tracer=tracer))
    assert backend.calls == 1
    assert info["compressed"] is True
    assert memory.meta["obs_digest"]["goal_restated"] == "demo"
    after_text = memory.observations()
    after = count_tokens(after_text)
    assert after < before
    assert tracer.events[0][0] == "obs_compress"
    pre = assemble(
        [ContextPart(key="recent_obs", text=before_text, priority=5, required=True)],
        role="reflector",
        budget=100000,
        hard=100000,
        window=128000,
    )
    # full=True still sees raw steps; the reflector prompt after digest is shorter.
    post = assemble(
        [ContextPart(key="recent_obs", text=after_text, priority=5, required=True)],
        role="reflector",
        budget=100000,
        hard=100000,
        window=128000,
        compressed=True,
        before_total=pre.tokens,
    )
    assert post.tokens < pre.tokens
    assert post.usage.compressed is True
    assert post.usage.after_total == post.tokens
    assert post.usage.before_total == pre.tokens
