"""Observation compaction.

Large tool payloads are offloaded separately (see artifacts.py). This module
summarizes older step observations when a reflector call is near its token
budget or the step count crosses the configured interval. The LLM call is
``await backend.generate`` — never a synchronous LangChain invoke.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.agent.context_builder import count_tokens
from app.core.config import settings

logger = logging.getLogger("agent.compress")


class ObsDigest(BaseModel):
    goal_restated: str = ""
    completed_actions: list[str] = Field(default_factory=list)
    key_numbers: dict[str, Any] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    next_focus: str = ""


def format_obs_digest(digest: Any) -> str:
    if isinstance(digest, str):
        return digest
    if not isinstance(digest, dict):
        return str(digest)
    lines = ["Observation digest (older steps compressed; raw steps remain on disk):"]
    if digest.get("goal_restated"):
        lines.append(f"- goal: {digest['goal_restated']}")
    actions = digest.get("completed_actions") or []
    if actions:
        lines.append("- completed: " + ", ".join(str(a) for a in actions))
    numbers = digest.get("key_numbers") or {}
    if numbers:
        lines.append("- key_numbers: " + json.dumps(numbers, ensure_ascii=False, default=str)[:400])
    failures = digest.get("failures") or []
    if failures:
        lines.append("- failures: " + "; ".join(str(f) for f in failures)[:400])
    paths = digest.get("artifact_paths") or []
    if paths:
        lines.append("- artifacts: " + ", ".join(str(p) for p in paths)[:500])
    if digest.get("next_focus"):
        lines.append(f"- next_focus: {digest['next_focus']}")
    return "\n".join(lines)


def _step_blob(step: dict[str, Any]) -> str:
    try:
        return json.dumps(
            {"action": step.get("action"), "status": step.get("status"), "result": step.get("result")},
            ensure_ascii=False,
            default=str,
        )
    except Exception:  # noqa: BLE001
        return str(step)


def compression_needed(memory, budget: int) -> bool:
    """True when step count, observation chars, or a full-history token estimate trips."""
    if not settings.agent_obs_compress_enabled:
        return False
    steps = list(getattr(memory, "steps", []) or [])
    keep = max(1, int(settings.agent_obs_keep_raw))
    if len(steps) <= keep:
        return False
    covered = int((getattr(memory, "meta", None) or {}).get("obs_digest_covered") or 0)
    new_steps = len(steps) - covered
    every_n = max(1, int(settings.agent_obs_compress_every_n))
    if memory.meta.get("obs_digest") and new_steps < every_n:
        est = count_tokens(memory.observations(full=True))
        return est >= float(settings.agent_obs_compress_ratio) * max(1, budget)
    if len(steps) >= every_n:
        return True
    chars = sum(len(_step_blob(step)) for step in steps)
    if chars >= int(settings.agent_obs_compress_chars):
        return True
    est = count_tokens(memory.observations(full=True))
    return est >= float(settings.agent_obs_compress_ratio) * max(1, budget)


def _artifact_paths(steps: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for step in steps:
        result = step.get("result")
        if isinstance(result, dict) and result.get("_artifact_handle") and result.get("artifact"):
            paths.append(str(result["artifact"]))
    return paths


def _digest_prompt(goal: str, old_steps: list[dict[str, Any]]) -> str:
    lines = []
    for step in old_steps:
        blob = _step_blob(step)
        if len(blob) > 800:
            blob = blob[:800] + "…"
        lines.append(f"- {step.get('action')} [{step.get('status')}]: {blob}")
    history = "\n".join(lines)[:6000]
    return (
        "Summarize OLDER agent observations into one ObsDigest. Keep quality-gate "
        "numbers, focus keywords, failures, and artifact paths. Drop raw note bodies.\n"
        f"Goal: {goal}\n"
        f"Known artifact paths: {_artifact_paths(old_steps)}\n"
        f"Older steps:\n{history}\n"
    )


async def compress_observations(memory, backend, *, goal: str, tracer=None) -> dict[str, Any]:
    """Replace older observations with an ObsDigest. Failures are non-fatal.

    Uses ``await backend.generate`` so the async run loop is not blocked by a
    synchronous client.
    """
    keep = max(1, int(settings.agent_obs_keep_raw))
    steps = list(memory.steps)
    if len(steps) <= keep:
        return {"compressed": False}
    old = steps[:-keep]
    before_tokens = count_tokens(memory.observations(full=True))
    try:
        digest = await backend.generate(_digest_prompt(goal, old), ObsDigest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("obs compress failed (non-fatal): %s", exc)
        return {"compressed": False, "before_tokens": before_tokens}
    payload = digest.model_dump()
    if not payload.get("artifact_paths"):
        payload["artifact_paths"] = _artifact_paths(old)
    memory.meta["obs_digest"] = payload
    memory.meta["obs_digest_covered"] = len(steps)
    after_text = memory.observations()
    after_tokens = count_tokens(after_text)
    info = {
        "compressed": True,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "kept_raw": keep,
        "digest_chars": len(after_text),
    }
    if tracer is not None:
        tracer.event(
            "obs_compress",
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            kept_raw=keep,
            digest_chars=info["digest_chars"],
        )
    return info
