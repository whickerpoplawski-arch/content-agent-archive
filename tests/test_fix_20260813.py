"""Regression tests for the 2026-08-13 real-run console defects.

Covers the high/medium defects from docs/real-run-test-20260813-console-xidian.md:
- 缺陷7: run_manager 终态判定(不虚报 completed)
- 缺陷9: _coerce_params 对 record-list 字段兜底(crawl 结果 dict 内联不再崩)
- 缺陷1: 反思计数以质量门实测报告为准(on_target_passed 不再振荡)
- 缺陷8: 爬取结果落盘 data/raw(analyze 失败数据不丢)
- 缺陷1/2: 质量全过时 override 反思的 continue/replan -> stop
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.agent.run_manager import _resolve_terminal
from app.agent.tools import AnalyzeParams, _coerce_params, build_default_registry
from app.agent.executor import _quality_passed_count
from app.agent.memory import AgentMemory, LongTermMemory

SAMPLE = [
    {"title": "t1", "content": "c1", "likes": 1, "favorites": 1, "comments": 0,
     "tags": ["a"], "author": "x", "url": "u1"},
]


def test_terminal_status_resolution():
    """缺陷7: 只有计划完整才 completed;有失败步骤标 failed;用户停止标 stopped."""
    assert _resolve_terminal(False, True) == ("run_completed", "completed")
    assert _resolve_terminal(True, True) == ("run_stopped", "stopped")
    assert _resolve_terminal(False, False) == ("run_failed", "failed")
    assert _resolve_terminal(True, False) == ("run_stopped", "stopped")


def test_coerce_params_unwraps_record_list_from_crawl_wrapper():
    """缺陷9: analyze_notes.items 是 list[NoteItem],遇整个 crawl 结果 dict
    ({"target_count":..., "items":[...]})时应提取列表,而不是把 dict 透传
    给 Pydantic 导致 items Input should be a valid list."""
    crawl_wrapper = {
        "target_count": 5,
        "count": 5,
        "used_keywords": ["西电研究生"],
        "items": SAMPLE,
    }
    coerced = _coerce_params(AnalyzeParams, {"items": crawl_wrapper})
    assert isinstance(coerced["items"], list)
    assert len(coerced["items"]) == len(SAMPLE)
    # 必须能通过 Pydantic 校验(此前这里抛 ValidationError)
    AnalyzeParams(**coerced)


def test_coerce_params_passthrough_bare_list_still_works():
    """缺陷9 回归:load_notes 路径返回裸 list 时不受影响(原行为)."""
    coerced = _coerce_params(AnalyzeParams, {"items": SAMPLE})
    assert coerced["items"] == SAMPLE
    AnalyzeParams(**coerced)


def test_quality_passed_count_aggregates_reports():
    """缺陷1: on_target_passed 从质量门实测报告汇总,不靠 LLM 数数."""
    memory = AgentMemory(LongTermMemory(Path("data/agent/profile.json")))
    memory.record("generate_contents", {}, {
        "contents": [{"title": "a"}], "quality_report": {"evaluated": 1, "passed": 1},
    }, "succeeded")
    memory.record("generate_contents", {}, {
        "contents": [{"title": "b"}], "quality_report": {"evaluated": 1, "passed": 1},
    }, "succeeded")
    memory.record("generate_contents", {}, {
        "contents": [{"title": "c"}], "quality_report": {"evaluated": 1, "passed": 1},
    }, "succeeded")
    assert _quality_passed_count(memory) == 3


def test_quality_passed_count_returns_none_without_reports():
    memory = AgentMemory(LongTermMemory(Path("data/agent/profile.json")))
    memory.record("generate_contents", {}, {"contents": [{"title": "a"}]}, "succeeded")
    assert _quality_passed_count(memory) is None


def test_crawl_result_persisted_to_raw(tmp_path, monkeypatch):
    """缺陷8: crawl_notes 成功后立即落盘 data/raw/,analyze 失败也不丢数据."""
    from app.agent.executor import Executor

    monkeypatch.chdir(tmp_path)
    memory = AgentMemory(LongTermMemory(tmp_path / "profile.json"))
    executor = Executor.__new__(Executor)
    executor._crawl_empty_calls = 0
    executor._stop_requested = False
    executor.tracer = type("T", (), {"run_id": "abc123"})()
    executor._track_crawl_result(
        {"target_count": 2, "count": 2, "items": SAMPLE, "used_keywords": ["k"]}
    )
    target = tmp_path / "data" / "raw" / "crawled_notes_abc123.json"
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data["items"]) == len(SAMPLE)


def test_executor_overrides_reflector_when_quality_already_passes():
    """缺陷1/2: 质量门已全过(passed >= target)而反思仍要 continue -> 直接 stop,
    不再白跑 replan / 追加步骤(修复 3/3 全过还重规划的振荡)."""
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.schemas import Plan, PlanStep, Reflection, StepStatus
    from app.agent.tools import Tool, ToolRegistry, LoadNotesParams
    from app.agent.llm import MockLLMBackend

    async def ok_handler(params):
        return {"contents": [{"title": "x"}],
                "quality_report": {"evaluated": 1, "optimized": 0, "passed": 1}}

    registry = ToolRegistry().register(
        Tool("generate_contents", "gen", LoadNotesParams, ok_handler)
    )
    backend = MockLLMBackend()
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="continue", reason="mock continue"),
    )
    executor = Executor(
        registry, Reflector(backend), Planner(backend, registry.specs()),
        quality_gate=None, max_parallel=1,
    )
    memory = AgentMemory(LongTermMemory(Path("data/agent/profile.json")))
    # 目标 1 篇;首步成功后 quality_report passed=1 >= target=1 -> override stop
    plan = Plan(goal="生成 1 篇文案", steps=[PlanStep(action="generate_contents", params={"count": 1})])
    result = asyncio.run(executor.execute(plan.goal, "profile", plan, memory))
    assert result.steps[0].status == StepStatus.SUCCEEDED
    assert executor._stop_requested is True
    # 不应追加补足步骤
    assert len(result.steps) == 1


def test_reflector_stop_skips_pending_steps_not_failed():
    """实测缺陷(2026-08-13): 反思主动 stop 是目标达成收尾。

    crawl 5/5 + generate 全过质量门,但 analyze(依赖 crawl)还没轮到就被
    reflector stop -> 若剩余 PENDING 步骤不标 SKIPPED,plan.is_complete=False,
    终态被误报 failed「Plan incomplete」。修复后应标 SKIPPED -> completed。
    """
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.schemas import Plan, PlanStep, Reflection, StepStatus
    from app.agent.tools import Tool, ToolRegistry, LoadNotesParams
    from app.agent.llm import MockLLMBackend

    async def ok_handler(params):
        return {"contents": [{"title": "x"}],
                "quality_report": {"evaluated": 1, "optimized": 0, "passed": 1}}

    registry = ToolRegistry().register(
        Tool("generate_contents", "gen", LoadNotesParams, ok_handler)
    )
    backend = MockLLMBackend()
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=True, next_action="stop", reason="goal met"),
    )
    executor = Executor(
        registry, Reflector(backend), Planner(backend, registry.specs()),
        quality_gate=None, max_parallel=1,
    )
    memory = AgentMemory(LongTermMemory(Path("data/agent/profile.json")))
    # 首步 generate 成功;analyze(crawl 后置步骤)尚 PENDING,反射立即 stop。
    plan = Plan(goal="生成 1 篇文案", steps=[
        PlanStep(action="generate_contents", params={"count": 1}, rationale="r1"),
        PlanStep(action="analyze_notes", params={"items": []}, rationale="r2"),
    ])
    result = asyncio.run(executor.execute(plan.goal, "profile", plan, memory))
    assert result.steps[0].status == StepStatus.SUCCEEDED
    # 反思 stop -> 剩余 PENDING 标 SKIPPED 而非留 PENDING
    assert result.steps[1].status == StepStatus.SKIPPED
    assert result.is_complete is True


def test_generate_contents_waits_for_prior_analyze_even_without_ref():
    """缺陷8.2(实测 2026-08-13): planner 生成的 generate 步骤可能硬编码 topic、
    不带 $latest 引用,导致与 crawl/analyze 并行先跑、不消费真实数据。

    兜底:generate 无显式依赖但 plan 中排在其前的 analyze_notes 存在时,
    强制依赖 analyze(不并行);排在 generate 之后的步骤不构成依赖。
    """
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.schemas import Plan, PlanStep, Reflection, StepStatus
    from app.agent.tools import Tool, ToolRegistry, LoadNotesParams, AnalyzeParams
    from app.agent.llm import MockLLMBackend
    from app.agent.tracing import AgentTracer

    order: list[str] = []

    async def stub_analyze(params):
        order.append("analyze")
        return {"summary": "s", "top_keywords": [], "top_tags": [],
                "title_patterns": [], "insight_points": []}

    async def stub_generate(params):
        order.append("generate")
        return {"contents": [{"title": "x"}],
                "quality_report": {"evaluated": 1, "optimized": 0, "passed": 1}}

    registry = ToolRegistry().register(
        Tool("analyze_notes", "analyze", AnalyzeParams, stub_analyze)
    ).register(
        Tool("generate_contents", "gen", LoadNotesParams, stub_generate)
    )
    backend = MockLLMBackend()
    refs = iter([
        Reflection(should_stop=False, next_action="continue", reason="analyze done, keep going"),
        Reflection(should_stop=True, next_action="stop", reason="goal met"),
    ])
    backend.register(Reflection, lambda: next(refs))
    tracer = AgentTracer.new()
    executor = Executor(
        registry, Reflector(backend), Planner(backend, registry.specs()),
        quality_gate=None, max_parallel=4, tracer=tracer,
    )
    memory = AgentMemory(LongTermMemory(Path("data/agent/profile.json")))
    plan = Plan(goal="分析并生成", steps=[
        PlanStep(action="analyze_notes", params={"items": []}, rationale="r1"),
        PlanStep(action="generate_contents", params={"count": 1}, rationale="r2"),
    ])
    result = asyncio.run(executor.execute(plan.goal, "profile", plan, memory))
    # generate 无 $latest 引用,但 analyze 排在它前面 -> 必须等 analyze 完成
    assert order == ["analyze", "generate"], order
    assert result.steps[0].status == StepStatus.SUCCEEDED
    assert result.steps[1].status == StepStatus.SUCCEEDED


def test_replan_with_recovery_step_is_not_rejected():
    """P1-4: 新计划含失败动作的恢复步骤时,replan 必须通过(不误拒)。

    首轮 analyze_notes 注入失败 -> recovery 要求 replan -> replan 返回含
    analyze_notes 的恢复计划 -> 通过校验并重试成功。断言无 replan_rejected,
    且失败动作确实被恢复(无 FAILED 遗留)。
    """
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.schemas import Plan, PlanStep, Reflection, Recovery, StepStatus
    from app.agent.tools import Tool, ToolRegistry, AnalyzeParams
    from app.agent.llm import MockLLMBackend

    calls = {"n": 0}

    async def flaky_analyze(params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("analyze crashed once (injected)")
        return {"summary": "ok", "top_keywords": [], "top_tags": [],
                "title_patterns": [], "insight_points": []}

    registry = ToolRegistry().register(
        Tool("analyze_notes", "analyze", AnalyzeParams, flaky_analyze)
    )
    backend = MockLLMBackend()
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="continue", reason="mock"),
    )
    backend.register(
        Recovery,
        lambda: Recovery(next_action="replan", reason="mock replan"),
    )
    backend.register(
        Plan,
        lambda: Plan(goal="分析", steps=[
            PlanStep(action="analyze_notes", params={"items": []}, rationale="recover"),
        ]),
    )
    from app.agent.tracing import AgentTracer

    tracer = AgentTracer.new()
    executor = Executor(
        registry,
        Reflector(backend),
        Planner(backend, registry.specs()),
        max_retries_per_step=0,
        tracer=tracer,
    )
    memory = AgentMemory(LongTermMemory(Path("data/agent/profile.json")))
    plan = Plan(goal="分析", steps=[
        PlanStep(action="analyze_notes", params={"items": []}, rationale="r"),
    ])
    result = asyncio.run(executor.execute(plan.goal, "profile", plan, memory))
    assert not any(e["kind"] == "replan_rejected" for e in tracer.events)
    assert calls["n"] == 2  # 失败一次 + 恢复重试一次
    assert all(s.status != StepStatus.FAILED for s in result.steps)
