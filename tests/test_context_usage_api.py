"""SSE/REST context usage snapshots."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _wait(run_id: str, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/agent/runs/{run_id}").json()
        if payload["status"] in {"completed", "failed", "stopped"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_context_usage_missing_run_is_404():
    response = client.get("/agent/runs/does-not-exist-zz/context_usage")
    assert response.status_code == 404


def test_context_usage_empty_snapshot_is_null():
    created = client.post(
        "/agent/runs",
        json={"goal": "context usage empty marker", "backend": "mock"},
    ).json()
    _wait(created["run_id"])
    response = client.get(f"/agent/runs/{created['run_id']}/context_usage")
    assert response.status_code == 200
    body = response.json()
    assert body["latest"] is None
    assert body["history"] == []


def test_context_usage_history_keeps_before_and_after(tmp_path):
    from app.agent.run_manager import AgentRunManager

    mgr = AgentRunManager(tmp_path / "runs")
    run_id = "ctx1"
    with mgr._lock:
        mgr._runs[run_id] = {
            "run_id": run_id,
            "goal": "g",
            "status": "running",
            "created_at": "t",
            "updated_at": "t",
            "events": [],
            "plan": [],
            "outputs": {},
            "usage": {},
            "tool_calls": {},
            "error": None,
            "stop_requested": False,
            "backend": "mock",
            "enabled_tools": [],
        }
        mgr._approval_conditions[run_id] = threading.Condition(mgr._lock)
    before = {
        "call_id": "a",
        "role": "reflector",
        "system": 10,
        "tools": 0,
        "messages": 90,
        "skills": 0,
        "knowledge": 0,
        "semantic": 0,
        "episodic": 0,
        "other": 0,
        "total": 100,
        "budget": 4000,
        "window": 128000,
        "compressed": False,
        "before_total": 100,
        "after_total": None,
    }
    after = {**before, "call_id": "b", "messages": 30, "total": 40, "compressed": True, "after_total": 40}
    mgr._from_agent_event(run_id, "context_usage", before)
    mgr._from_agent_event(run_id, "obs_compress", {"before_tokens": 100, "after_tokens": 40, "kept_raw": 4})
    mgr._from_agent_event(run_id, "context_usage", after)
    snap = mgr.context_usage(run_id)
    assert snap["latest"]["compressed"] is True
    assert snap["latest"]["total"] == snap["latest"]["system"] + snap["latest"]["tools"] + snap["latest"]["messages"]
    assert [item["call_id"] for item in snap["history"]] == ["a", "b"]
    types = [event["type"] for event in mgr.get(run_id)["events"]]
    assert types == ["context_usage", "obs_compress", "context_usage"]
