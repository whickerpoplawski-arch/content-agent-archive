"""ContextBuilder budgets, snapshots, and the flag-off planner path."""

from __future__ import annotations

import asyncio

from app.agent.context_builder import ContextPart, assemble, count_tokens
from app.agent.llm import MockLLMBackend
from app.agent.schemas import Plan, PlanStep
from app.core.config import Settings, settings


def test_over_budget_drops_low_priority_keeps_goal():
    goal = "Goal: ship three notes"
    filler = "insight " * 4000
    parts = [
        ContextPart(key="goal", text=goal, priority=2, required=True),
        ContextPart(key="insights_raw", text=filler, priority=10),
        ContextPart(key="knowledge", text="K " * 2000, priority=9),
    ]
    result = assemble(parts, role="planner", budget=80, hard=120, window=1000, run_id="run")
    assert result.tokens <= 120
    assert result.tokens == result.usage.total
    assert "goal" not in result.dropped
    assert "goal" in result.included
    assert result.dropped
    assert "insights_raw" in result.dropped or "knowledge" in result.dropped


def test_required_block_over_budget_uses_concise():
    huge = "token " * 3000
    concise = "name: load_notes"
    parts = [
        ContextPart(
            key="tools",
            text=huge,
            concise_text=concise,
            priority=3,
            required=True,
        )
    ]
    budget = count_tokens(concise) + 2
    result = assemble(parts, role="planner", budget=budget, hard=budget + 10, window=1000)
    assert "tools" in result.concised
    assert concise in result.text
    assert result.tokens <= budget + 10


def test_knowledge_dropped_when_higher_priority_fills_budget():
    skill = "skill-index " * 80
    parts = [
        ContextPart(key="skill_index", text=skill, priority=7, required=True),
        ContextPart(key="knowledge", text="K1 " * 500, priority=9),
    ]
    budget = count_tokens(skill)
    result = assemble(parts, role="planner", budget=budget, hard=budget, window=8000)
    assert "knowledge" in result.dropped
    assert "skill_index" in result.included
    assert "[K1]" not in result.text


def test_usage_buckets_sum_and_percents():
    parts = [
        ContextPart(key="system", text="system rules", priority=1, required=True),
        ContextPart(key="tools", text="tool load_notes", priority=3, required=True),
        ContextPart(key="goal", text="goal text", priority=2, required=True),
        ContextPart(key="semantic", text="audience students", priority=4),
    ]
    result = assemble(parts, role="planner", budget=500, hard=600, window=1000, run_id="abc")
    usage = result.usage
    total = (
        usage.system
        + usage.tools
        + usage.messages
        + usage.skills
        + usage.knowledge
        + usage.semantic
        + usage.episodic
        + usage.other
    )
    assert usage.total == total
    assert usage.system > 0 and usage.tools > 0 and usage.messages > 0
    assert usage.percent_budget == usage.total / 500 * 100
    assert usage.percent_window == usage.total / 1000 * 100


def test_assemble_emits_context_usage_event():
    class Tracer:
        def __init__(self):
            self.events = []

        def event(self, kind, **fields):
            self.events.append((kind, fields))

    tracer = Tracer()
    parts = [
        ContextPart(key="system", text="sys", priority=1, required=True),
        ContextPart(key="tools", text="tools", priority=3, required=True),
        ContextPart(key="goal", text="goal", priority=2, required=True),
    ]
    result = assemble(parts, role="reflector", budget=200, hard=250, window=1000, tracer=tracer, run_id="r1")
    kinds = [kind for kind, _ in tracer.events]
    assert kinds == ["context_usage"]
    fields = tracer.events[0][1]
    assert fields["total"] == fields["system"] + fields["tools"] + fields["messages"] + fields["skills"] + fields["knowledge"] + fields["semantic"] + fields["episodic"] + fields["other"]
    assert fields["total"] == result.usage.total
    assert "system" in fields and "tools" in fields and "messages" in fields


def test_flag_off_planner_keeps_legacy_template(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_builder_enabled", False)
    captured = {}

    async def fake(prompt, schema, **kwargs):
        captured["prompt"] = prompt
        return Plan(goal="g", steps=[PlanStep(action="load_notes")])

    monkeypatch.setattr("app.agent.planner.generate_structured", fake)
    from app.agent.planner import Planner

    planner = Planner(MockLLMBackend(), [])
    asyncio.run(planner.plan("my goal", "profile-text-marker"))
    assert "User profile:" in captured["prompt"]
    assert "profile-text-marker" in captured["prompt"]
    assert "my goal" in captured["prompt"]


def test_flag_on_planner_emits_usage(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_builder_enabled", True)
    captured = {}

    async def fake(prompt, schema, **kwargs):
        captured["prompt"] = prompt
        return Plan(goal="g", steps=[PlanStep(action="load_notes")])

    monkeypatch.setattr("app.agent.planner.generate_structured", fake)
    from app.agent.planner import Planner

    class Tracer:
        def __init__(self):
            self.events = []
            self.run_id = "run-flag"

        def event(self, kind, **fields):
            self.events.append((kind, fields))

    tracer = Tracer()
    planner = Planner(MockLLMBackend(), [])
    asyncio.run(planner.plan("goal text", "audience: students", tracer=tracer, run_id="run-flag"))
    assert tracer.events[0][0] == "context_usage"
    assert tracer.events[0][1]["role"] == "planner"
    assert "goal text" in captured["prompt"]
    assert "audience: students" in captured["prompt"]


def test_flag_defaults():
    assert Settings.model_fields["agent_context_builder_enabled"].default is False
    assert Settings.model_fields["agent_obs_compress_enabled"].default is False
    assert Settings.model_fields["agent_skills_enabled"].default is False
    assert Settings.model_fields["agent_artifact_offload_enabled"].default is True
