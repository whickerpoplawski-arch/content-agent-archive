"""Lightweight, dependency-free scheduler for recurring Agent runs (plan P2-a).

Persists schedules to data/agent/schedules.json and runs a background loop that
fires due schedules through AgentRunManager.create (reusing the existing approval
flow and SSE events). Supports two trigger types: an interval (seconds) and a
daily wall-clock run_at (HH:MM, UTC). No third-party cron dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("agent.scheduler")

SCHEDULES_FILE = Path("data") / "agent" / "schedules.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Scheduler:
    """JSON-backed store of recurring Agent run schedules with a poll loop."""

    def __init__(self, path: Path = SCHEDULES_FILE) -> None:
        self.path = Path(path)
        self._schedules: dict[str, dict[str, Any]] = {}
        self._load()

    def create(
        self,
        *,
        name: str = "",
        goal: str,
        audience: str = "",
        tone: str = "",
        backend: str = "openai",
        enabled_tools: Optional[list[str]] = None,
        interval_seconds: Optional[int] = None,
        run_at: Optional[str] = None,
    ) -> dict[str, Any]:
        if not goal or not goal.strip():
            raise ValueError("goal is required")
        if not interval_seconds and not run_at:
            raise ValueError("provide interval_seconds or run_at")
        now = _utcnow()
        sid = uuid.uuid4().hex[:12]
        schedule = {
            "id": sid,
            "name": name or goal.strip()[:20],
            "goal": goal.strip(),
            "audience": audience,
            "tone": tone,
            "backend": backend,
            "enabled_tools": list(enabled_tools) if enabled_tools else None,
            "interval_seconds": int(interval_seconds) if interval_seconds else None,
            "run_at": run_at,
            "next_run_at": _iso(self._next(now, interval_seconds, run_at)),
            "created_at": _iso(now),
        }
        self._schedules[sid] = schedule
        self._save()
        return schedule

    def _next(self, now: datetime, interval_seconds, run_at) -> datetime:
        if interval_seconds:
            return now + timedelta(seconds=max(1, int(interval_seconds)))
        hours, minutes = (int(part) for part in run_at.split(":"))
        candidate = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._schedules.values(), key=lambda s: s["created_at"])

    def get(self, sid: str) -> dict[str, Any]:
        if sid not in self._schedules:
            raise KeyError(sid)
        return self._schedules[sid]

    def delete(self, sid: str) -> dict[str, Any]:
        schedule = self.get(sid)
        del self._schedules[sid]
        self._save()
        return schedule

    def due(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        now = now or _utcnow()
        return [
            s for s in self._schedules.values()
            if _parse(s["next_run_at"]) <= now
        ]

    async def run_loop(
        self,
        submit: Callable[[dict[str, Any]], Any],
        stop_event: Optional[threading.Event] = None,
        poll_seconds: float = 2.0,
    ) -> None:
        stop = stop_event or threading.Event()
        while not stop.is_set():
            try:
                self._fire_due(submit)
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduler fire failed: %s", exc)
            steps = max(1, int(poll_seconds * 10))
            for _ in range(steps):
                if stop.is_set():
                    return
                await asyncio.sleep(0.1)

    def _fire_due(self, submit: Callable[[dict[str, Any]], Any]) -> None:
        now = _utcnow()
        fired = self.due(now)
        if not fired:
            return
        for schedule in fired:
            submit(schedule)
            schedule["next_run_at"] = _iso(
                self._next(
                    now,
                    schedule.get("interval_seconds"),
                    schedule.get("run_at"),
                )
            )
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schedules": self._schedules}
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._schedules = data.get("schedules", {}) or {}
        except (OSError, ValueError) as exc:
            logger.warning("failed to load schedules: %s", exc)
            self._schedules = {}


scheduler = Scheduler()