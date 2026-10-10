"""Lightweight observability: per-run trace with timings, tool calls, and cost.

A run log is persisted as JSON under data/agent/runs/<run_id>.json so any Agent
run can be audited and replayed without external tracing infra.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

RUNS_DIR = Path("data") / "agent" / "runs"


class AgentTracer:
    def __init__(
        self,
        run_id: str,
        event_sink: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> None:
        self.run_id = run_id
        self.started = time.time()
        self.events: list[dict[str, Any]] = []
        self.event_sink = event_sink

    @classmethod
    def new(
        cls,
        run_id: Optional[str] = None,
        event_sink: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> "AgentTracer":
        return cls(run_id or uuid.uuid4().hex[:12], event_sink)

    def event(self, kind: str, **fields: Any) -> None:
        event = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
        self.events.append(event)
        if self.event_sink:
            self.event_sink(kind, fields)

    def tool_call(self, name: str, ok: bool, elapsed_ms: float, summary: str = "") -> None:
        self.event("tool_call", name=name, ok=ok, elapsed_ms=round(elapsed_ms, 1), summary=summary)

    def _write(self, result: dict[str, Any], partial: bool) -> str:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
            "duration_ms": round((time.time() - self.started) * 1000, 1),
            "events": self.events,
            "result": result,
            "partial": partial,
        }
        path = RUNS_DIR / f"{self.run_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
        return str(path)

    def flush(self, result: Optional[dict[str, Any]] = None) -> str:
        """Persist the current trace so a crash never loses the partial run
        (optimization plan 10.4 #2). Writes a `partial: true` trace."""
        return self._write(result or {}, partial=True)

    def save(
        self,
        result: dict[str, Any],
        usage: Optional[dict[str, Any]] = None,
        stopped_reason: str = "",
    ) -> str:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        # 缺陷4: trace 落盘补 status/usage/stopped_reason,单独打开文件即可判断
        # 运行成败与用量,不再需要外部 API 事件对照。
        status = "ok"
        for e in self.events:
            if e.get("kind") == "done":
                status = e.get("status", "ok")
                break
        payload = {
            "run_id": self.run_id,
            "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
            "duration_ms": round((time.time() - self.started) * 1000, 1),
            "status": status,
            "usage": usage,
            "stopped_reason": stopped_reason,
            "events": self.events,
            "result": result,
        }
        path = RUNS_DIR / f"{self.run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
