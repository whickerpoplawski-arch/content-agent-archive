"""Tests for the recurring Agent scheduler (optimization plan P2-a)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.agent.scheduler import Scheduler, _iso, _utcnow


client = TestClient(app)


def test_scheduler_create_list_delete(tmp_path):
    sched = Scheduler(tmp_path / "schedules.json")
    created = sched.create(name="daily", goal="analyze sample notes", interval_seconds=3600)
    assert created["interval_seconds"] == 3600
    assert created["next_run_at"] > created["created_at"]
    assert sched.list() == [created]

    deleted = sched.delete(created["id"])
    assert deleted["id"] == created["id"]
    assert sched.list() == []


def test_scheduler_requires_trigger(tmp_path):
    sched = Scheduler(tmp_path / "schedules.json")
    import pytest
    with pytest.raises(ValueError):
        sched.create(goal="g")


def test_scheduler_fires_due_schedules(tmp_path):
    sched = Scheduler(tmp_path / "schedules.json")
    item = sched.create(goal="g", interval_seconds=3600)
    item["next_run_at"] = _iso(_utcnow() - timedelta(seconds=10))
    sched._save()

    fired = []
    sched._fire_due(lambda schedule: fired.append(schedule["id"]))
    assert fired == [item["id"]]
    assert sched.due() == []  # rescheduled into the future


def test_schedule_endpoint_requires_admin_when_configured(monkeypatch):
    from app.core.config import settings
    from app.agent import scheduler as scheduler_mod

    monkeypatch.setattr(settings, "admin_token", "s3cret")
    monkeypatch.setattr(scheduler_mod, "scheduler", Scheduler())
    assert (
        client.post("/agent/schedule", json={"goal": "g", "interval_seconds": 60}).status_code
        == 403
    )


def test_schedule_endpoint_roundtrip(monkeypatch, tmp_path):
    from app.agent import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "scheduler", Scheduler(tmp_path / "schedules.json"))
    created = client.post(
        "/agent/schedule",
        json={"name": "daily", "goal": "analyze sample notes", "interval_seconds": 3600},
    )
    assert created.status_code == 200
    sid = created.json()["id"]

    listing = client.get("/agent/schedule")
    assert listing.status_code == 200
    assert any(item["id"] == sid for item in listing.json()["items"])

    deleted = client.delete(f"/agent/schedule/{sid}")
    assert deleted.status_code == 200
    assert client.delete(f"/agent/schedule/{sid}").status_code == 404