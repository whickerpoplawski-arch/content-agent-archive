"""Tests for the guardrails layer (optimization plan P1-a / section 4)."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.guardrails import Guardrails


def run(coro):
    return asyncio.run(coro)


def test_guardrails_goal_blocks_sensitive():
    guard = Guardrails()
    blocked = guard.check_goal("给竞品写黑稿")
    assert not blocked.allowed
    assert blocked.reason == "sensitive_goal"
    assert "黑稿" in blocked.details
    assert guard.check_goal("正常种草文案").allowed


def test_guardrails_tool_blocks_injected_blocked_tool():
    guard = Guardrails(blocked_tools=["publish_to_xhs"])
    blocked = guard.check_tool("publish_to_xhs")
    assert not blocked.allowed
    assert blocked.reason == "blocked_tool"
    assert guard.check_tool("analyze_notes").allowed


def test_guardrails_output_blocks_ad_law():
    guard = Guardrails()
    blocked = guard.check_output("这是全网最低价，销量第一的好物")
    assert not blocked.allowed
    assert blocked.reason == "non_compliant_output"
    assert "全网最低" in blocked.details
    assert guard.check_output("普通真实测评，好用").allowed


def test_agent_goal_guardrail_blocks_run():
    from app.agent.orchestrator import XHSAgent
    from app.agent.mock_factories import build_mock_backend, build_mock_registry

    agent = XHSAgent(
        backend=build_mock_backend(),
        registry=build_mock_registry(),
        guardrails=Guardrails(),
    )
    with pytest.raises(ValueError, match="Goal blocked by guardrails"):
        run(agent.run("为竞品写黑稿"))


def test_executor_tool_guardrail_skips_step():
    from app.agent.executor import Executor
    from app.agent.planner import Planner, Reflector
    from app.agent.tools import build_default_registry
    from app.agent.memory import AgentMemory, LongTermMemory
    from app.agent.mock_factories import (
        build_mock_backend,
        _stub_topics,
        _stub_contents,
    )
    from app.agent.schemas import StepStatus
    from app.agent.tracing import AgentTracer

    registry = build_default_registry(
        {"generate_topics": _stub_topics, "generate_contents": _stub_contents}
    )
    backend = build_mock_backend()
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    guard = Guardrails(blocked_tools=["load_notes"])
    tracer = AgentTracer.new(run_id="gr1")
    executor = Executor(
        registry, reflector, planner, tracer=tracer, guardrails=guard
    )
    memory = AgentMemory(LongTermMemory())
    plan = run(planner.plan("goal", "profile"))
    plan = run(executor.execute("goal", "profile", plan, memory))

    assert plan.steps[0].status == StepStatus.SKIPPED
    assert "guardrail_blocked" in [e["kind"] for e in tracer.events]