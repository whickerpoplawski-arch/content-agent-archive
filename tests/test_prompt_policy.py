"""Acceptance locks for planner / reflector / consolidator prompt policy."""

from __future__ import annotations

import asyncio

from app.agent.context_builder import assemble, budgets_for, count_tokens
from app.agent.llm import MockLLMBackend
from app.agent.memory import AgentMemory
from app.agent.schemas import Plan, PlanStep, Reflection
from app.core.config import settings


TOOL_CUES = {
    "knowledge_search": ("use first", "source", "do not write copy"),
    "load_notes": ("offline/sample", "fresh"),
    "analyze_notes": ("note list",),
    "generate_topics": ("focus_keywords",),
    "generate_contents": ("count", "don't split"),
    "generate_images": ("existing draft",),
    "publish_to_xhs": ("approval", "just try"),
    "sync_to_feishu": ("Bitable",),
    "crawl_notes": ("logged-in", "fresh"),
    "read_artifact": ("$latest",),
    "load_skill": ("skill body",),
}


def test_planner_system_short_but_substantive():
    from app.agent.planner import PLANNER_SYSTEM, PLANNER_TEMPLATE

    assert len(PLANNER_SYSTEM) >= 150
    assert count_tokens(PLANNER_SYSTEM) > 50
    lowered = PLANNER_SYSTEM.lower()
    assert "approval" in lowered
    assert "prefer knowledge_search" in lowered
    # Flag-off template stays the long legacy prompt.
    assert "Available tools" in PLANNER_TEMPLATE
    assert "Adaptive retrieval" in PLANNER_TEMPLATE
    assert "params:" in PLANNER_TEMPLATE
    assert len(PLANNER_TEMPLATE) > len(PLANNER_SYSTEM)


def test_tool_descriptions_aci():
    from app.agent.tools import build_default_registry

    registry = build_default_registry()
    missing = [name for name in TOOL_CUES if name not in registry.names()]
    assert missing == []
    for name, cues in TOOL_CUES.items():
        description = registry.get(name).description.lower()
        for cue in cues:
            assert cue.lower() in description, f"{name} missing cue {cue!r}: {description}"


def test_context_builder_planner_tools_nonzero(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_builder_enabled", True)
    monkeypatch.setattr(settings, "agent_skills_enabled", False)
    from app.agent.context_parts import build_planner_parts
    from app.agent.planner import PLANNER_SYSTEM, Planner
    from app.agent.tools import build_default_registry

    specs = build_default_registry().specs()
    planner = Planner(MockLLMBackend(), specs)
    parts = build_planner_parts(
        {},
        "为西电学生生成 3 篇实习笔记，不要发布",
        tools_full=planner._tools_text(),
        tools_concise=planner._tools_concise(),
        system_text=PLANNER_SYSTEM,
        include_skills=False,
    )
    soft, hard = budgets_for("planner")
    result = assemble(parts, role="planner", budget=soft, hard=hard, window=128000, run_id="planner-aci")
    tools_text = next(part.text for part in parts if part.key == "tools")
    assert '"properties"' in tools_text
    assert "tools" not in result.concised
    assert result.usage.system > 50
    assert result.usage.tools > 200
    assert result.usage.role == "planner"


def test_reflector_readonly_roster_no_schemas(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_builder_enabled", True)
    monkeypatch.setattr(settings, "agent_obs_compress_enabled", False)
    from app.agent.planner import REFLECTOR_TOOL_ROSTER, Reflector

    class RecordingBackend(MockLLMBackend):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def generate(self, prompt, schema, **kwargs):
            self.calls.append({"prompt": prompt, "schema": schema, "kwargs": dict(kwargs)})
            return Reflection(should_stop=False, next_action="continue", reason="ok")

    reflector = Reflector(RecordingBackend())
    plan = Plan(goal="g", steps=[PlanStep(action="load_notes")])
    parts = reflector._reflect_parts(
        "goal",
        plan,
        AgentMemory(),
        {"total": 0, "on_target_passed": 0, "target": 1},
        "(no quality gate run yet)",
        full_obs=False,
    )
    roster = next(part.text for part in parts if part.key == "tool_roster")
    assert roster == REFLECTOR_TOOL_ROSTER
    assert "knowledge_search" in roster and "load_skill" in roster and "generate_images" in roster
    assert '"properties"' not in roster
    assert "properties" not in roster

    soft, hard = budgets_for("reflector")
    assembled = assemble(parts, role="reflector", budget=soft, hard=hard, window=128000, run_id="reflect-aci")
    assert assembled.usage.system > 20
    assert assembled.usage.tools > 0
    assert '"properties"' not in assembled.text
    assert "properties" not in assembled.text

    result = asyncio.run(
        reflector.judge(
            "goal",
            plan,
            AgentMemory(),
            progress={"total": 0, "on_target_passed": 0, "target": 1},
            run_id="reflect-aci",
        )
    )
    assert result.next_action == "continue"
    assert len(reflector._backend.calls) == 1
    call = reflector._backend.calls[0]
    assert not call["kwargs"].get("tools")
    assert "tools" not in call["kwargs"]
    assert '"properties"' not in call["prompt"]
    assert "READ-ONLY tool roster" in call["prompt"]
    assert "do NOT call tools" in call["prompt"]


def test_consolidator_zero_tools():
    from app.agent.consolidation import _assemble_consolidator_prompt

    class Tracer:
        def __init__(self):
            self.events = []
            self.run_id = "dream-aci"

        def event(self, kind, **fields):
            self.events.append((kind, fields))

    tracer = Tracer()
    text = _assemble_consolidator_prompt(
        "Goal notes for dreaming.",
        goal="为大学生生成文案",
        tracer=tracer,
        run_id="dream-aci",
    )
    assert tracer.events
    kind, fields = tracer.events[0]
    assert kind == "context_usage"
    assert fields["role"] == "consolidator"
    assert fields["system"] > 0
    assert fields["tools"] == 0
    assert "DreamResult" in text or "procedural" in text
    assert '"properties"' not in text
    assert "READ-ONLY tool roster" not in text
