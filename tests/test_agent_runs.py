from __future__ import annotations

import time
import asyncio

from fastapi.testclient import TestClient

from app.main import app
from app.agent.executor import Executor
from app.agent.memory import AgentMemory, LongTermMemory
from app.agent.planner import Planner, Reflector
from app.agent.schemas import Plan, PlanStep, Reflection, StepStatus
from app.agent.tools import LoadNotesParams, Tool, ToolRegistry
from app.agent.llm import MockLLMBackend


client = TestClient(app)


def _wait_for_terminal(run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/agent/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed", "stopped"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def test_create_mock_run_streams_ordered_events_and_completes():
    response = client.post(
        "/agent/runs",
        json={
            "goal": "Analyze sample notes and create two posts without publishing",
            "audience": "college students",
            "tone": "practical",
            "backend": "mock",
            "enabled_tools": [
                "load_notes",
                "analyze_notes",
                "generate_topics",
                "generate_contents",
            ],
        },
    )

    assert response.status_code == 202
    created = response.json()
    assert created["run_id"]
    assert created["status"] in {"queued", "running"}

    run = _wait_for_terminal(created["run_id"])
    assert run["status"] == "completed"
    assert run["outputs"]["results"]

    event_ids = [event["id"] for event in run["events"]]
    event_types = [event["type"] for event in run["events"]]
    assert event_ids == list(range(1, len(event_ids) + 1))
    assert event_types[0] == "run_started"
    assert "plan_created" in event_types
    assert "tool_started" in event_types
    assert "tool_completed" in event_types
    assert event_types[-1] == "run_completed"


def test_completed_run_events_can_be_replayed_as_sse():
    created = client.post(
        "/agent/runs",
        json={"goal": "Create sample content", "backend": "mock"},
    ).json()
    run = _wait_for_terminal(created["run_id"])

    response = client.get(
        f"/agent/runs/{created['run_id']}/events",
        headers={"Last-Event-ID": "1"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {run['events'][1]['id']}" in response.text
    assert "event: run_completed" in response.text
    assert "id: 1\n" not in response.text


def test_run_history_contains_newest_run():
    created = client.post(
        "/agent/runs",
        json={"goal": "History marker goal", "backend": "mock"},
    ).json()
    _wait_for_terminal(created["run_id"])

    response = client.get("/agent/runs?limit=50")

    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["run_id"] == created["run_id"] for item in items)
    assert items[0]["created_at"] >= items[-1]["created_at"]


def test_create_run_rejects_unknown_tools():
    response = client.post(
        "/agent/runs",
        json={
            "goal": "Do something",
            "backend": "mock",
            "enabled_tools": ["not_a_real_tool"],
        },
    )

    assert response.status_code == 422
    assert "not_a_real_tool" in response.text


def test_executor_skips_side_effect_tool_when_approval_is_rejected():
    calls = {"publish": 0}

    async def publish_handler(params: LoadNotesParams):
        calls["publish"] += 1
        return {"success": True}

    registry = ToolRegistry().register(
        Tool("publish_to_xhs", "publish", LoadNotesParams, publish_handler)
    )
    backend = MockLLMBackend()
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="continue", reason="continue"),
    )
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    plan = Plan(goal="publish", steps=[PlanStep(action="publish_to_xhs", params={})])

    async def reject(_action, _params):
        return False

    executor = Executor(registry, reflector, planner, approval_handler=reject)
    result = asyncio.run(
        executor.execute("publish", "profile", plan, AgentMemory(LongTermMemory()))
    )

    assert calls["publish"] == 0
    assert result.steps[0].status == StepStatus.SKIPPED
    assert "rejected" in result.steps[0].summary.lower()


def test_executor_stop_check_prevents_next_tool_from_starting():
    calls: list[str] = []
    stopped = {"value": False}

    async def first_handler(params: LoadNotesParams):
        calls.append("first")
        stopped["value"] = True
        return [{"title": "done"}]

    async def second_handler(params: LoadNotesParams):
        calls.append("second")
        return []

    registry = ToolRegistry()
    registry.register(Tool("first", "first", LoadNotesParams, first_handler))
    registry.register(Tool("second", "second", LoadNotesParams, second_handler))
    backend = MockLLMBackend()
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="continue", reason="continue"),
    )
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    plan = Plan(
        goal="stop",
        steps=[
            PlanStep(action="first", params={}),
            PlanStep(action="second", params={"items": "$latest.first"}),
        ],
    )

    executor = Executor(
        registry,
        reflector,
        planner,
        stop_check=lambda: stopped["value"],
        max_parallel=1,
    )
    result = asyncio.run(
        executor.execute("stop", "profile", plan, AgentMemory(LongTermMemory()))
    )

    assert calls == ["first"]
    assert result.steps[0].status == StepStatus.SUCCEEDED
    assert result.steps[1].status == StepStatus.PENDING


def test_stop_endpoint_is_idempotent_for_terminal_run():
    created = client.post(
        "/agent/runs",
        json={"goal": "Complete before stop", "backend": "mock"},
    ).json()
    _wait_for_terminal(created["run_id"])

    first = client.post(f"/agent/runs/{created['run_id']}/stop")
    second = client.post(f"/agent/runs/{created['run_id']}/stop")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "completed"
    assert second.json()["status"] == "completed"


def test_stop_unknown_run_returns_404():
    response = client.post("/agent/runs/does-not-exist/stop")

    assert response.status_code == 404


def test_confirm_without_pending_approval_returns_409():
    created = client.post(
        "/agent/runs",
        json={"goal": "No publish", "backend": "mock"},
    ).json()
    _wait_for_terminal(created["run_id"])

    response = client.post(
        f"/agent/runs/{created['run_id']}/confirm",
        json={"approval_id": "stale", "approved": True},
    )

    assert response.status_code == 409


def test_agent_console_page_exposes_live_run_controls():
    response = client.get("/static/agent_console.html")

    assert response.status_code == 200
    html = response.text
    assert 'id="goalInput"' in html
    assert 'id="stopRunBtn"' in html
    assert 'id="historyPanel"' in html
    assert 'id="approvalDialog"' in html
    assert "new EventSource" in html
    assert "/agent/runs" in html
    assert 'id="contextChip"' in html
    assert 'id="contextPopover"' in html
    assert "context_usage" in html
    assert "/context_usage" in html


def test_home_page_links_to_agent_console():
    response = client.get("/static/index.html")

    assert response.status_code == 200
    assert "/static/agent_console.html" in response.text


def test_resume_unknown_run_404():
    response = client.post("/agent/runs/doesnotexist/resume")
    assert response.status_code == 404


def test_resume_completed_run_without_checkpoint_409():
    created = client.post(
        "/agent/runs",
        json={
            "goal": "Analyze sample notes and create two posts without publishing",
            "audience": "college students",
            "tone": "practical",
            "backend": "mock",
            "enabled_tools": [
                "load_notes",
                "analyze_notes",
                "generate_topics",
                "generate_contents",
            ],
        },
    )
    assert created.status_code == 202
    run = _wait_for_terminal(created.json()["run_id"])
    assert run["status"] == "completed"
    response = client.post(f"/agent/runs/{run['run_id']}/resume")
    assert response.status_code == 409  # completed run cleared its checkpoint