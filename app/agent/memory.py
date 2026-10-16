"""Agent memory.

Short-term memory tracks the results of each executed step within a run so the
planner/reflector can reason over prior observations. Long-term memory persists
a lightweight user profile (audience, tone, preferences) to disk so the Agent
"learns" the target user across runs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger("agent.memory")

MEMORY_DIR = Path("data") / "agent"
PROFILE_FILE = MEMORY_DIR / "profile.json"


class LongTermMemory:
    """JSON-backed store for user profile / long-lived preferences."""

    def __init__(self, path: Path = PROFILE_FILE) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load long-term memory %s: %s", self.path, exc)
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to save long-term memory: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    def set_many(self, mapping: dict[str, Any]) -> None:
        """Batch set with a single disk write (每 run 多键回写时避免多次全量重写)。"""
        self._data.update(mapping)
        self.save()

    def update(self, mapping: dict[str, Any]) -> None:
        self._data.update(mapping)
        self.save()

    @property
    def data(self) -> dict[str, Any]:
        return self._data


class AgentMemory:
    """Combined short- and long-term memory exposed to the Agent loop."""

    def __init__(self, long_term: Optional[LongTermMemory] = None) -> None:
        self.long_term = long_term or LongTermMemory()
        self.steps: list[dict[str, Any]] = []  # {action, params, result, status}
        self._by_action: dict[str, list[dict[str, Any]]] = {}
        self.meta: dict[str, Any] = {}

    def record(self, action: str, params: dict[str, Any], result: Any, status: str) -> None:
        entry = {"action": action, "params": params, "result": result, "status": status}
        self.steps.append(entry)
        self._by_action.setdefault(action, []).append(entry)

    def results_for(self, action: str) -> list[Any]:
        return [e["result"] for e in self._by_action.get(action, [])]

    def latest(self, action: str) -> Optional[Any]:
        entries = self._by_action.get(action, [])
        return entries[-1]["result"] if entries else None

    def count_results(self, action: str, predicate=None) -> int:
        """Count generated items across all rounds of ``action`` (12.5 objective
        progress). ``predicate`` filters items (e.g. on-target & passing)."""
        from app.agent.artifacts import materialize_result

        n = 0
        for result in self.results_for(action):
            result = materialize_result(result)
            if isinstance(result, dict):
                for item in result.get("contents", []):
                    if predicate is None or predicate(item):
                        n += 1
        return n

    def restore(self, steps: list[dict[str, Any]]) -> None:
        """Rebuild short-term memory from a persisted checkpoint."""
        self.steps = list(steps)
        self._by_action = {}
        for entry in self.steps:
            self._by_action.setdefault(entry.get("action"), []).append(entry)
    def observations(self, limit: int = 5, *, full: bool = False) -> str:
        """Compact summary of recent step observations for the planner/reflector.

        With observation compression enabled and a digest present, returns the
        digest plus the last ``agent_obs_keep_raw`` raw steps. ``full=True``
        expands every step (used to measure the pre-compression prompt). The
        default path (flag off) stays the recent ``limit`` steps.
        """
        digest = None
        if settings.agent_obs_compress_enabled:
            digest = (self.meta or {}).get("obs_digest")
        if digest:
            from app.agent.compress import format_obs_digest

            keep = max(1, int(settings.agent_obs_keep_raw))
            recent = self._format_steps(self.steps[-keep:])
            text = format_obs_digest(digest)
            if recent:
                text += "\nRecent steps:\n" + recent
            return text
        use_limit = len(self.steps) if full else limit
        recent = self.steps[-use_limit:] if use_limit else []
        return self._format_steps(recent) or "(no observations yet)"

    def _format_steps(self, entries: list[dict[str, Any]]) -> str:
        lines = []
        for entry in entries:
            summary = _summarize(entry["result"])
            lines.append(f"- {entry['action']} -> {entry['status']}: {summary}")
        return "\n".join(lines)


def _summarize(value: Any, max_len: int = 120) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text
