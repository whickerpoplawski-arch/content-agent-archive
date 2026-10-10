"""Tests for the Agent core (planner/executor/memory/tools/llm).

These run fully offline via the deterministic Mock backend, so they need no API
key and no external services.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent.executor import resolve_params
from app.agent.llm import MockLLMBackend, generate_structured
from app.agent.memory import AgentMemory, LongTermMemory
from app.agent.orchestrator import XHSAgent
from app.agent.mock_factories import build_mock_backend, build_mock_registry
from app.agent.schemas import Plan, PlanStep, Recovery, Reflection, StepStatus
from app.agent.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_mock_agent_loop_completes(tmp_path):
    agent = XHSAgent(backend=build_mock_backend(), registry=build_mock_registry())
    result = run(
        agent.run(
            "为大学生女生生成小红书种草文案",
            audience="大学生女生",
            tone="真实分享",
        )
    )

    actions = [s.action for s in result.plan.steps]
    assert actions == ["load_notes", "analyze_notes", "generate_topics", "generate_contents"]
    assert all(s.status == StepStatus.SUCCEEDED for s in result.plan.steps)
    assert result.outputs.get("analysis_summary")
    assert len(result.outputs.get("results", [])) >= 1
    # tool call accounting + trace persisted
    assert result.tool_calls["analyze_notes"] == 1
    assert Path(result.trace_path).exists()


def test_resolve_params_references():
    memory = AgentMemory(LongTermMemory(Path("data/agent/profile.json")))
    memory.record("analyze_notes", {}, {"summary": "S", "top_keywords": ["a", "b"]}, "succeeded")

    resolved = run(
        resolve_params(
            {"summary": "$latest.analyze_notes.summary", "kw": "$latest.analyze_notes.top_keywords"},
            memory,
        )
    )
    assert resolved == {"summary": "S", "kw": ["a", "b"]}

    with pytest.raises(KeyError):
        run(resolve_params({"bad": "$latest.unknown.field"}, memory))


def test_mock_backend_structured_output():
    backend = MockLLMBackend()
    backend.register(Reflection, lambda: Reflection(should_stop=True, next_action="stop", reason="done"))
    ref = run(generate_structured("do it", Reflection, backend=backend, max_retries=1))
    assert ref.should_stop is True and ref.next_action == "stop"


def test_generate_structured_retries_then_raises():
    class Flaky(MockLLMBackend):
        def __init__(self):
            super().__init__()
            self.attempts = 0
        async def generate(self, prompt, schema, *, temperature=None):
            self.attempts += 1
            raise RuntimeError("boom")

    backend = Flaky()
    with pytest.raises(RuntimeError):
        run(generate_structured("x", Reflection, backend=backend, max_retries=2))
    assert backend.attempts == 2


def test_tool_registry_validates_params():
    registry = build_mock_registry()
    # analyze_notes with a list of notes should succeed (coerced to NoteItem)
    notes = [{"title": "T", "content": "C", "likes": 1}]
    out = run(registry.call("analyze_notes", {"items": notes}))
    assert out["total_count"] == 1

    # missing required params -> validation error
    with pytest.raises(Exception):
        run(registry.call("analyze_notes", {}))


def test_planner_returns_plan_and_tools_visible():
    backend = build_mock_backend()
    registry = build_mock_registry()
    specs = registry.specs()
    names = [s.name for s in specs]
    assert "analyze_notes" in names and "publish_to_xhs" in names



def test_autonomous_endpoint_mock_backend():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/agent/autonomous",
        json={
            "goal": "为大学生女生生成小红书文案",
            "audience": "大学生女生",
            "tone": "真实分享",
            "backend": "mock",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert all(s["status"] == "succeeded" for s in data["plan"])
    assert data["trace_path"]
    assert len(data["outputs"].get("results", [])) >= 1



def test_evaluator_optimizer_loop_revises_then_passes():
    from app.agent.evaluator import Evaluator, Optimizer, QualityGate
    from app.agent.schemas import ContentDraft, ContentEvaluation

    backend = MockLLMBackend()
    state = {"n": 0}
    def eval_factory():
        state["n"] += 1
        if state["n"] == 1:
            return ContentEvaluation(score=4, verdict="revise", issues=["标题不够吸引"])
        return ContentEvaluation(score=9, verdict="pass", issues=[])
    backend.register(ContentEvaluation, eval_factory)
    backend.register(
        ContentDraft,
        lambda: ContentDraft(title="优化后标题", body="优化正文", hashtags=["#a"],
                             cta="c", image_suggestion="s", content_type="分享"),
    )

    gate = QualityGate(Evaluator(backend), Optimizer(backend), min_score=7, max_rounds=3)
    contents = [{"title": "t", "body": "b", "hashtags": ["#x"], "cta": "c",
                 "image_suggestion": "s", "content_type": "分享"}]
    revised, report = run(gate.run(contents))

    assert report["optimized"] == 1
    assert report["evaluated"] == 2
    assert revised[0]["title"] == "优化后标题"



class _ScriptedBackend(MockLLMBackend):
    """Backend with scripted responses for Plan/Reflection/Recovery."""

    def __init__(self, recovery_action="retry", adjusted=None):
        super().__init__()
        self.recovery_action = recovery_action
        self.adjusted = adjusted or {}
        self.recovery_calls = 0

    async def generate(self, prompt, schema, *, temperature=None):
        if schema is Plan:
            from app.agent.mock_factories import _mock_plan
            return _mock_plan()
        if schema is Reflection:
            return Reflection(should_stop=False, next_action="continue", reason="continue")
        if schema is Recovery:
            self.recovery_calls += 1
            return Recovery(next_action=self.recovery_action, adjusted_params=self.adjusted, reason="r")
        raise ValueError(f"unexpected schema {schema}")


def test_executor_retries_failed_tool():
    """Fault injection: a tool fails once, the executor retries it and succeeds."""
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry, LoadNotesParams
    from app.agent.memory import AgentMemory, LongTermMemory

    state = {"calls": 0}
    async def flaky_load(params: LoadNotesParams) -> list:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("network glitch")
        return [{"title": "T", "content": "C", "likes": 1}]

    from app.agent.mock_factories import _stub_topics, _stub_contents
    registry = build_default_registry(
        {"load_notes": flaky_load, "generate_topics": _stub_topics, "generate_contents": _stub_contents}
    )
    backend = _ScriptedBackend(recovery_action="retry")
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    executor = Executor(registry, reflector, planner, max_retries_per_step=3)
    memory = AgentMemory(LongTermMemory())

    plan = run(planner.plan("goal", "profile"))
    plan = run(executor.execute("goal", "profile", plan, memory))

    assert state["calls"] == 2  # failed once, retried once
    assert plan.steps[0].status == StepStatus.SUCCEEDED
    assert plan.is_complete


def test_executor_stops_on_unrecoverable_failure():
    """Fault injection: recovery says stop -> loop halts, plan stays incomplete."""
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry
    from app.agent.memory import AgentMemory, LongTermMemory

    async def always_fail(params):
        raise RuntimeError("hard failure")

    registry = build_default_registry({"load_notes": always_fail})
    backend = _ScriptedBackend(recovery_action="stop")
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    executor = Executor(registry, reflector, planner, max_retries_per_step=2)
    memory = AgentMemory(LongTermMemory())

    plan = run(planner.plan("goal", "profile"))
    plan = run(executor.execute("goal", "profile", plan, memory))

    assert plan.steps[0].status == StepStatus.FAILED
    assert not plan.is_complete
    assert plan.failed_step is not None



def test_resolve_ref_best_effort_fallback():
    """Planner may over-index references (e.g. .items); we degrade gracefully."""
    from app.agent.executor import resolve_params
    from app.agent.memory import AgentMemory, LongTermMemory

    memory = AgentMemory(LongTermMemory())
    memory.record("load_notes", {}, [{"title": "A", "content": "c"}, {"title": "B", "content": "d"}], "succeeded")

    # .items doesn't exist on a list -> fall back to the full list
    resolved = run(resolve_params({"items": "$latest.load_notes.items"}, memory))
    assert resolved["items"] == [{"title": "A", "content": "c"}, {"title": "B", "content": "d"}]

    # deep missing field -> closest ancestor (the dict)
    resolved2 = run(resolve_params({"x": "$latest.load_notes.0.nope"}, memory))
    assert resolved2["x"] == {"title": "A", "content": "c"}



def test_tool_registry_coerces_dict_to_str():
    """Planner may pass a whole topic object where a title string is expected."""
    from app.agent.tools import build_default_registry, ContentsParams

    received = {}
    async def stub_contents(params: ContentsParams) -> dict:
        received["topic"] = params.topic
        return {"contents": []}

    registry = build_default_registry({"generate_contents": stub_contents})
    run(registry.call("generate_contents", {
        "topic": {"title": "大学生好物", "reason": "高收藏"},
        "reason": "r", "audience": "大学生女生", "tone": "真实分享", "count": 1,
    }))
    assert received["topic"] == "大学生好物"


def test_coerce_unwraps_dict_wrapping_list_to_str():
    """Real planner passes ``$latest.generate_topics`` which resolves to a dict
    wrapping a list (``{"topics": [{"title": ...}]}``); the topic title must be
    extracted instead of failing ContentsParams.topic validation."""
    from app.agent.tools import ContentsParams, _coerce_params

    out = _coerce_params(
        ContentsParams,
        {"topic": {"topics": [{"title": "宿舍收纳大法", "reason": "高频词"}]}},
    )
    assert out["topic"] == "宿舍收纳大法"


def test_coerce_unwraps_bare_list_to_str():
    """A bare list of records (``[{"title": ...}]``) should also reduce to the
    first record's scalar."""
    from app.agent.tools import ContentsParams, _coerce_params

    out = _coerce_params(ContentsParams, {"topic": [{"title": "避雷指南", "reason": "r"}]})
    assert out["topic"] == "避雷指南"


def test_coerce_extracts_scalar_list_field():
    """ImagesParams.content_hashtags (list[str]) must be pulled out of a dict
    wrapping the contents list; this is the field that failed 6/6 in the real
    LLM run before the fix."""
    from app.agent.tools import ImagesParams, _coerce_params

    out = _coerce_params(
        ImagesParams,
        {
            "topic_title": {"topics": [{"title": "宿舍好物", "reason": "r"}]},
            "content_title": {"contents": [{"title": "好物推荐", "body": "正文"}]},
            "content_hashtags": {"contents": [{"hashtags": ["#宿舍", "#平价"]}]},
        },
    )
    assert out["topic_title"] == "宿舍好物"
    assert out["content_title"] == "好物推荐"
    assert out["content_hashtags"] == ["#宿舍", "#平价"]


def test_coerce_maps_content_prefix_to_record_keys():
    """ImagesParams fields are prefixed (content_title/content_body/content_cta/
    content_image_suggestion); they must map back to the record's own keys
    (title/body/cta/image_suggestion)."""
    from app.agent.tools import ImagesParams, _coerce_params

    record = {"contents": [{
        "title": "T", "body": "B", "cta": "C",
        "image_suggestion": "IMG", "content_type": "清单",
    }]}
    out = _coerce_params(ImagesParams, {
        "topic_title": {"topics": [{"title": "TT"}]},
        "content_title": record,
        "content_body": record,
        "content_cta": record,
        "content_image_suggestion": record,
        "content_type": record,
    })
    assert out["content_title"] == "T"
    assert out["content_body"] == "B"
    assert out["content_cta"] == "C"
    assert out["content_image_suggestion"] == "IMG"
    assert out["content_type"] == "清单"


def test_coerce_preserves_record_list_field():
    """list[NoteItem] (AnalyzeParams.items) must NOT be unwrapped to the first
    record -- the full list of notes must reach the handler."""
    from app.agent.tools import AnalyzeParams, _coerce_params
    from app.models.schemas import NoteItem

    notes = [
        {"title": "n1", "content": "c1", "likes": 1, "favorites": 1, "comments": 1, "tags": []},
        {"title": "n2", "content": "c2", "likes": 2, "favorites": 2, "comments": 2, "tags": []},
    ]
    out = _coerce_params(AnalyzeParams, {"items": notes})
    assert isinstance(out["items"], list)
    assert len(out["items"]) == 2  # not collapsed to first record
    # and still validates into NoteItem objects
    validated = AnalyzeParams(**out)
    assert all(isinstance(n, NoteItem) for n in validated.items)


def test_coerce_leaves_correct_values_untouched():
    """Already-correct values pass through unchanged (backward compat)."""
    from app.agent.tools import ContentsParams, _coerce_params

    out = _coerce_params(ContentsParams, {"topic": "已是字符串", "count": 2})
    assert out["topic"] == "已是字符串"



def test_budget_logic():
    from app.agent.budget import Budget

    b = Budget(max_steps=2)
    assert b.exceeded({"calls": 5, "est_tokens": 0}, 3) is True
    assert b.reason({"calls": 5}, 3) == "steps 3 > 2"
    assert b.exceeded({"calls": 5}, 1) is False

    calls = Budget(max_llm_calls=4)
    assert calls.exceeded({"calls": 5}, 1) is True
    tokens = Budget(max_est_tokens=1000)
    assert tokens.exceeded({"calls": 1, "est_tokens": 1500}, 1) is True


def test_executor_stops_on_budget():
    """A tiny step/usage budget halts the loop and leaves the plan incomplete."""
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.budget import Budget
    from app.agent.mock_factories import _mock_plan, _stub_topics, _stub_contents

    registry = build_default_registry(
        {"generate_topics": _stub_topics, "generate_contents": _stub_contents}
    )
    backend = _ScriptedBackend(recovery_action="retry")
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    executor = Executor(
        registry, reflector, planner,
        budget=Budget(max_steps=1),
        get_usage=lambda: {"calls": 0, "est_tokens": 0},
    )
    memory = AgentMemory(LongTermMemory())
    plan = run(planner.plan("goal", "profile"))
    plan = run(executor.execute("goal", "profile", plan, memory))

    # budget.max_steps now caps the TOTAL executed steps (plan 10.2): with
    # max_steps=1 only the first step runs; the rest stay pending.
    assert plan.steps[0].status == StepStatus.SUCCEEDED
    assert plan.steps[1].status == StepStatus.PENDING
    assert plan.steps[2].status == StepStatus.PENDING  # halted before finishing
    assert not plan.is_complete



def test_profile_text_renders_lessons_actionably():
    from app.agent.orchestrator import _profile_text

    profile = {
        "audience": "?????",
        "lessons": [{"goal": "????", "steps_ok": True, "results": 2,
                     "quality": [{"passed": 2}]}],
    }
    txt = _profile_text(profile)
    assert "Relevant past experiences (reuse what worked, avoid what failed)" in txt
    assert "????" in txt and "results=2" in txt

def test_budget_from_settings_reads_max_steps():
    """Config-drift fix (plan 10.2): Budget.from_settings must honor agent_max_steps."""
    from app.agent.budget import Budget

    class _Stub:
        agent_max_llm_calls = 0
        agent_max_est_tokens = 0
        agent_max_steps = 7
        agent_max_replans = 0

    budget = Budget.from_settings(_Stub())
    assert budget.max_steps == 7


def test_executor_uses_budget_max_steps_as_total_cap():
    """plan 10.2: max_steps is now a hard total-step cap, not a post-batch threshold."""
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.budget import Budget
    from app.agent.mock_factories import _mock_plan, _stub_topics, _stub_contents

    registry = build_default_registry(
        {"generate_topics": _stub_topics, "generate_contents": _stub_contents}
    )
    backend = _ScriptedBackend(recovery_action="retry")
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    executor = Executor(
        registry, reflector, planner,
        budget=Budget(max_steps=2),
        get_usage=lambda: {"calls": 0, "est_tokens": 0},
    )
    memory = AgentMemory(LongTermMemory())
    plan = run(planner.plan("goal", "profile"))
    plan = run(executor.execute("goal", "profile", plan, memory))

    assert plan.steps[0].status == StepStatus.SUCCEEDED
    assert plan.steps[1].status == StepStatus.SUCCEEDED
    assert plan.steps[2].status == StepStatus.PENDING
    assert not plan.is_complete



def test_remember_lesson_dedups_by_goal(tmp_path):
    """plan 12.1: lessons with the same normalized goal are merged, not stacked."""
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.orchestrator import _remember_lesson

    memory = AgentMemory(LongTermMemory(tmp_path / "profile.json"))
    goal = "create xhs content for college girls"
    plan = Plan(
        goal=goal,
        steps=[
            PlanStep(action="load_notes", status=StepStatus.SUCCEEDED),
            PlanStep(action="analyze_notes", status=StepStatus.SUCCEEDED),
        ],
    )
    _remember_lesson(memory, goal, plan, {"results": ["a"]})
    _remember_lesson(memory, goal, plan, {"results": ["b"]})

    episodic = memory.long_term.get("episodic")
    assert len(episodic) == 1  # deduped
    assert episodic[0]["results"] == 1  # newest wins
    assert len(memory.long_term.get("procedural")) == 1


def test_profile_text_recalls_by_keyword(tmp_path):
    """plan 12.1: _profile_text recalls lessons matching the current goal's keywords."""
    from app.agent.orchestrator import _profile_text

    profile = {
        "audience": "college girls",
        "episodic": [
            {"goal": "create xhs content for college girls", "steps_ok": True, "results": 2},
            {"goal": "create foodie exploration notes", "steps_ok": True, "results": 1},
        ],
        "procedural": [
            {"goal": "create xhs content for college girls", "actions": ["analyze_notes"]},
            {"goal": "create foodie exploration notes", "actions": ["load_notes"]},
        ],
    }
    txt = _profile_text(profile, goal="create xhs content for college girls")
    assert "college girls" in txt
    assert "foodie" not in txt
    assert "Successful tool chains to follow" in txt
    assert "analyze_notes" in txt

    all_txt = _profile_text(profile)  # no goal -> fall back to recent/all
    assert "foodie" in all_txt

def test_checkpointer_roundtrip(tmp_path):
    """StepCheckpointer persists plan/memory/usage and can be cleared (plan P0)."""
    from app.agent.checkpoint import StepCheckpointer

    cp = StepCheckpointer(tmp_path / "checkpoints")
    plan = Plan(
        goal="g",
        steps=[PlanStep(action="load_notes", status=StepStatus.SUCCEEDED)],
    )
    cp.save(
        "abc",
        plan,
        [{"action": "load_notes", "result": "x", "status": "succeeded"}],
        {"calls": 1, "est_tokens": 10},
    )
    saved = cp.load("abc")
    assert saved is not None
    restored = Plan.model_validate(saved["plan"])
    assert restored.steps[0].status == StepStatus.SUCCEEDED
    assert saved["memory"]["steps"][0]["action"] == "load_notes"
    assert saved["usage"]["calls"] == 1
    cp.clear("abc")
    assert cp.load("abc") is None


def test_executor_resume_skips_succeeded_steps(tmp_path):
    """plan P0 / 12.2: resuming from a checkpoint skips already-succeeded steps."""
    from app.agent.executor import Executor
    from app.agent.checkpoint import StepCheckpointer
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry
    from app.agent.budget import Budget
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.mock_factories import _mock_plan, _stub_topics, _stub_contents
    from app.agent.tracing import AgentTracer

    registry = build_default_registry(
        {"generate_topics": _stub_topics, "generate_contents": _stub_contents}
    )
    backend = _ScriptedBackend(recovery_action="retry")
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    cp = StepCheckpointer(tmp_path / "checkpoints")
    run_id = "resume1"

    # First pass: budget caps total steps at 1, so only step 0 executes.
    memory = AgentMemory(LongTermMemory())
    executor1 = Executor(
        registry, reflector, planner,
        tracer=AgentTracer.new(run_id=run_id),
        budget=Budget(max_steps=1),
        get_usage=lambda: {"calls": 0, "est_tokens": 0},
        checkpointer=cp,
    )
    plan = run(planner.plan("goal", "profile"))
    plan = run(executor1.execute("goal", "profile", plan, memory))
    assert plan.steps[0].status == StepStatus.SUCCEEDED
    assert plan.steps[1].status == StepStatus.PENDING
    assert cp.load(run_id) is not None

    # Second pass: restore from checkpoint; step 0 must NOT run again.
    saved = cp.load(run_id)
    restored_plan = Plan.model_validate(saved["plan"])
    memory2 = AgentMemory(LongTermMemory())
    memory2.restore(saved["memory"]["steps"])
    executor2 = Executor(
        registry, reflector, planner,
        tracer=AgentTracer.new(run_id=run_id),
        budget=Budget(max_steps=20),
        get_usage=lambda: {"calls": 0, "est_tokens": 0},
        checkpointer=cp,
    )
    executor2.base_usage = saved.get("usage", {})
    plan2 = run(executor2.execute("goal", "profile", restored_plan, memory2))
    assert plan2.is_complete
    assert plan2.steps[0].status == StepStatus.SUCCEEDED
    assert plan2.steps[1].status == StepStatus.SUCCEEDED
    # step 0 recorded exactly once (restored, not re-executed)
    assert len(memory2.results_for("load_notes")) == 1

def test_tracer_flush_persists_partial(tmp_path, monkeypatch):
    """plan 10.4 #2: intermediate flush writes a durable partial trace."""
    import json
    from app.agent.tracing import AgentTracer

    monkeypatch.setattr("app.agent.tracing.RUNS_DIR", tmp_path)
    tracer = AgentTracer.new(run_id="flush1")
    tracer.event("goal", goal="g")
    path = tracer.flush()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["partial"] is True
    assert data["events"][0]["kind"] == "goal"


# ============================================================================
# Optimization plan 12.1 - 12.5 (M0 quality fix)
# ============================================================================

def test_focus_keywords_injected():
    """plan 12.1: planner injects target-domain keywords into topic/content steps."""
    from app.agent.planner import Planner
    from app.agent.mock_factories import build_mock_backend, build_mock_registry

    backend = build_mock_backend()
    registry = build_mock_registry()
    planner = Planner(backend, registry.specs())
    goal = "为西电学生生成 3 篇暑期实习文案"
    plan = run(planner.plan(goal, "profile"))
    gen = [s for s in plan.steps if s.action == "generate_contents"][0]
    focus = gen.params.get("focus_keywords") or []
    assert focus, "focus_keywords must be injected into generate_contents"
    assert any("实习" in kw for kw in focus), "focus must include the goal domain"
    # topics step should carry it too
    top = [s for s in plan.steps if s.action == "generate_topics"][0]
    assert top.params.get("focus_keywords") == focus


def test_relevance_gate_blocks_off_topic():
    """plan 12.2: quality gate flags drafts that miss focus_keywords."""
    from app.agent.evaluator import Evaluator, Optimizer, QualityGate
    from app.agent.schemas import ContentDraft, ContentEvaluation

    backend = MockLLMBackend()
    backend.register(
        ContentEvaluation,
        lambda: ContentEvaluation(score=3, verdict="revise", issues=["跑题：未命中目标关键词"]),
    )
    backend.register(
        ContentDraft,
        lambda: ContentDraft(title="优化标题", body="优化正文", hashtags=["#a"],
                             cta="c", image_suggestion="s", content_type="分享"),
    )
    gate = QualityGate(Evaluator(backend), Optimizer(backend), min_score=7, max_rounds=1)
    items = [{"title": "宿舍收纳好物", "body": "宿舍收纳好物推荐，空间翻倍",
              "hashtags": ["#宿舍"], "cta": "c", "image_suggestion": "s", "content_type": "分享"}]
    revised, report = run(gate.run(items, focus_keywords=["实习"]))
    assert report["passed"] == 0
    assert revised[0]["_verdict"] == "revise"


def test_collect_outputs_dedupes_and_ranks():
    """plan 12.3: results dedupe by title and rank by focus relevance, <= top_n."""
    from app.agent.orchestrator import _collect_outputs
    from app.agent.memory import AgentMemory, LongTermMemory

    memory = AgentMemory(LongTermMemory())
    items = [
        {"title": "暑期实习避坑指南", "body": "实习真实体验", "_score": 8, "_verdict": "pass"},
        {"title": "暑期实习避坑指南", "body": "实习真实体验", "_score": 6, "_verdict": "revise"},
        {"title": "实习简历怎么写", "body": "简历经验", "_score": 9, "_verdict": "pass"},
        {"title": "宿舍收纳大法", "body": "收纳技巧", "_score": 9, "_verdict": "pass"},
    ]
    memory.record("generate_contents", {}, {"contents": items, "quality_report": {"passed": 2}}, "succeeded")

    outputs = _collect_outputs(memory, focus_keywords=["实习"], strategy="best_n", top_n=2)
    results = outputs["results"]
    assert len(results) == 2
    assert [r["title"] for r in results] == ["实习简历怎么写", "暑期实习避坑指南"]
    assert all("_score" not in r and "_verdict" not in r for r in results)

    # strategy="all" dedupes but does not truncate; temp fields still stripped
    all_out = _collect_outputs(memory, focus_keywords=["实习"], strategy="all")
    assert len(all_out["results"]) == 3  # dup merged (2 unique + 1 off-topic)
    assert all("_score" not in r for r in all_out["results"])


def test_oscillation_break():
    """plan 12.4: 3 consecutive same-reason replans force focus injection."""
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.budget import Budget
    from app.agent.tracing import AgentTracer
    from app.agent.evaluator import Evaluator, Optimizer, QualityGate
    from app.agent.schemas import ContentDraft, ContentEvaluation
    from app.agent.mock_factories import _mock_plan, _stub_topics, _stub_contents

    registry = build_default_registry(
        {"generate_topics": _stub_topics, "generate_contents": _stub_contents}
    )
    backend = MockLLMBackend()
    backend.register(Plan, _mock_plan)
    backend.register(Recovery, lambda: Recovery(next_action="replan", reason="r"))
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="replan", reason="same off-topic reason"),
    )
    backend.register(ContentEvaluation, lambda: ContentEvaluation(score=9, verdict="pass", issues=[]))
    backend.register(
        ContentDraft,
        lambda: ContentDraft(title="优化标题", body="优化正文", hashtags=["#a"],
                             cta="c", image_suggestion="s", content_type="分享"),
    )

    class _SpyPlanner(Planner):
        def __init__(self, backend, tools):
            super().__init__(backend, tools)
            self.replan_focus = []

        async def replan(self, goal, profile, failed_step=None, focus_keywords=None):
            self.replan_focus.append(list(focus_keywords) if focus_keywords else None)
            return await super().replan(goal, profile, failed_step, focus_keywords=focus_keywords)

    planner = _SpyPlanner(backend, registry.specs())
    reflector = Reflector(backend)
    tracer = AgentTracer.new(run_id="osc1")
    executor = Executor(
        registry, reflector, planner,
        tracer=tracer,
        quality_gate=QualityGate(Evaluator(backend), Optimizer(backend)),
        budget=Budget(max_steps=12),
        get_usage=lambda: {"calls": 0, "est_tokens": 0},
    )
    memory = AgentMemory(LongTermMemory())
    goal = "为西电学生生成 3 篇暑期实习文案"
    plan = run(planner.plan(goal, "profile"))
    run(executor.execute(goal, "profile", plan, memory))

    assert any(e["kind"] == "oscillation_break" for e in tracer.events)
    assert any(f is not None and len(f) > 0 for f in planner.replan_focus)


def test_reflector_uses_objective_count():
    """plan 12.5: judge prompt carries objective progress counts."""
    from app.agent.planner import Reflector
    from app.agent.schemas import Reflection

    class _CaptureBackend(MockLLMBackend):
        def __init__(self):
            super().__init__()
            self.last_prompt = ""

        async def generate(self, prompt, schema, *, temperature=None):
            self.last_prompt = prompt
            return Reflection(should_stop=True, next_action="stop", reason="goal met")

    backend = _CaptureBackend()
    reflector = Reflector(backend)
    memory = AgentMemory(LongTermMemory())
    plan = Plan(goal="g", steps=[])
    run(
        reflector.judge(
            "为西电学生生成 3 篇暑期实习文案",
            plan,
            memory,
            progress={"total": 3, "on_target_passed": 3, "target": 3},
            quality_report={"passed": 3},
        )
    )
    assert "produced copy count: 3" in backend.last_prompt
    assert "on-target & passing count: 3" in backend.last_prompt
    assert "target count: 3" in backend.last_prompt


def test_append_steps_when_continue_and_full():
    """plan 12.5: reflector says continue with an exhausted plan -> append step."""
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry, ContentsParams
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.budget import Budget
    from app.agent.evaluator import Evaluator, Optimizer, QualityGate
    from app.agent.schemas import ContentDraft, ContentEvaluation
    from app.agent.mock_factories import _mock_plan, _stub_topics

    async def _target_contents(params: ContentsParams) -> dict:
        focus = params.focus_keywords or ["实习"]
        kw = focus[0]
        n = max(1, params.count)
        return {
            "contents": [
                {"title": f"{kw}实战{i}", "body": f"关于{kw}的真实体验{i}",
                 "hashtags": ["#实习"], "cta": "c", "image_suggestion": "s",
                 "content_type": "分享"}
                for i in range(n)
            ]
        }

    registry = build_default_registry(
        {"generate_topics": _stub_topics, "generate_contents": _target_contents}
    )
    backend = MockLLMBackend()
    backend.register(Plan, _mock_plan)
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="continue", reason="need more"),
    )
    backend.register(ContentEvaluation, lambda: ContentEvaluation(score=9, verdict="pass", issues=[]))
    backend.register(
        ContentDraft,
        lambda: ContentDraft(title="优化标题", body="优化正文", hashtags=["#a"],
                             cta="c", image_suggestion="s", content_type="分享"),
    )
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    executor = Executor(
        registry, reflector, planner,
        quality_gate=QualityGate(Evaluator(backend), Optimizer(backend)),
        budget=Budget(max_steps=20),
        get_usage=lambda: {"calls": 0, "est_tokens": 0},
    )
    memory = AgentMemory(LongTermMemory())
    goal = "为西电学生生成 3 篇暑期实习文案"
    plan = run(planner.plan(goal, "profile"))
    plan = run(executor.execute(goal, "profile", plan, memory))

    assert plan.is_complete
    assert len(plan.steps) > 4  # a 补足 generate_contents step was appended
    assert memory.count_results("generate_contents") >= 3
