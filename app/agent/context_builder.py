"""Token-budget context assembly.

Every LLM call is built from prioritized blocks. When the soft budget is
exceeded, optional blocks are dropped (lowest priority first) and required
blocks fall back to a concise form, then a hard fit. The resulting
TokenUsageSnapshot is the only token source for the usage panel.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from app.agent.llm import _estimate_tokens
from app.core.config import settings

logger = logging.getLogger("agent.context_builder")

# parts.key -> snapshot bucket. Unknown keys land in ``other``.
_BUCKETS = {
    "system": "system",
    "tools": "tools",
    "tools_schema": "tools",
    "tool_roster": "tools",
    "goal": "messages",
    "plan_progress": "messages",
    "current_plan_progress": "messages",
    "observations": "messages",
    "recent_obs": "messages",
    "obs_digest": "messages",
    "older_obs_summary": "messages",
    "quality_report": "messages",
    "replan_note": "messages",
    "skill_index": "skills",
    "knowledge": "knowledge",
    "semantic": "semantic",
    "prefs": "semantic",
    "facts": "semantic",
    "memory_general": "semantic",
    "memory_situational": "semantic",
    "episodic": "episodic",
    "memory_episodic": "episodic",
}

_BUCKET_NAMES = (
    "system",
    "tools",
    "messages",
    "skills",
    "knowledge",
    "semantic",
    "episodic",
    "other",
)


def count_tokens(text: str) -> int:
    """Same estimator as llm usage fallback (tiktoken cl100k_base, else len//4)."""
    if not text:
        return 0
    return _estimate_tokens(text)


class ContextPart(BaseModel):
    key: str
    text: str = ""
    priority: int = 100
    required: bool = False
    concise_text: Optional[str] = None


class TokenUsageSnapshot(BaseModel):
    """One pre-call context occupancy snapshot (system / tools / messages / …)."""

    call_id: str
    run_id: str = ""
    role: str
    system: int = 0
    tools: int = 0
    messages: int = 0
    skills: int = 0
    knowledge: int = 0
    semantic: int = 0
    episodic: int = 0
    other: int = 0
    total: int = 0
    budget: int = 0
    window: int = 0
    percent_budget: float = 0.0
    percent_window: float = 0.0
    dropped: list[str] = Field(default_factory=list)
    concised: list[str] = Field(default_factory=list)
    compressed: bool = False
    before_total: Optional[int] = None
    after_total: Optional[int] = None
    ts: str = ""


class AssembleResult(BaseModel):
    text: str
    tokens: int
    included: list[str]
    dropped: list[str]
    concised: list[str]
    usage: TokenUsageSnapshot


def budgets_for(role: str) -> tuple[int, int]:
    """Return (soft, hard) token budgets for a call role."""
    table = {
        "planner": (settings.agent_context_budget_planner, settings.agent_context_hard_planner),
        "reflector": (settings.agent_context_budget_reflector, settings.agent_context_hard_reflector),
        "recover": (settings.agent_context_budget_recover, settings.agent_context_hard_recover),
        "consolidator": (
            settings.agent_context_budget_consolidator,
            settings.agent_context_hard_consolidator,
        ),
    }
    return table.get(role, (settings.agent_context_budget_planner, settings.agent_context_hard_planner))


def _fit_text(text: str, token_budget: int) -> str:
    """Shorten ``text`` so count_tokens(result) <= token_budget. Empty if budget is 0."""
    if token_budget <= 0 or not text:
        return ""
    if count_tokens(text) <= token_budget:
        return text
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        chunk = text[:mid]
        if count_tokens(chunk) <= token_budget:
            best = chunk
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _bucket(key: str) -> str:
    return _BUCKETS.get(key, "other")


def _snapshot(
    included: list[tuple[str, str]],
    *,
    role: str,
    budget: int,
    window: int,
    run_id: str,
    dropped: list[str],
    concised: list[str],
    compressed: bool,
    before_total: Optional[int],
    after_total: Optional[int],
    stamp_before: bool,
) -> TokenUsageSnapshot:
    buckets = {name: 0 for name in _BUCKET_NAMES}
    for key, text in included:
        buckets[_bucket(key)] += count_tokens(text)
    total = sum(buckets.values())
    if stamp_before and before_total is None:
        before_total = total
    if compressed and after_total is None:
        after_total = total
    percent_budget = (total / budget * 100.0) if budget else 0.0
    percent_window = (total / window * 100.0) if window else 0.0
    return TokenUsageSnapshot(
        call_id=uuid.uuid4().hex[:12],
        run_id=run_id or "",
        role=role,
        total=total,
        budget=budget,
        window=window,
        percent_budget=percent_budget,
        percent_window=percent_window,
        dropped=list(dropped),
        concised=list(concised),
        compressed=compressed,
        before_total=before_total,
        after_total=after_total,
        ts=datetime.now(timezone.utc).isoformat(),
        **buckets,
    )


def assemble(
    parts: list[ContextPart],
    *,
    role: str,
    budget: int,
    hard: Optional[int] = None,
    window: Optional[int] = None,
    run_id: str = "",
    tracer=None,
    compressed: bool = False,
    before_total: Optional[int] = None,
    after_total: Optional[int] = None,
    stamp_before: bool = False,
) -> AssembleResult:
    """Fill ``budget`` by ascending priority. Required blocks are never dropped.

    ``hard`` is an absolute ceiling (defaults to ``budget``). The snapshot
    recounts tokens on the text that was actually included.
    """
    limit = budget if hard is None else min(budget, hard)
    if limit < 0:
        limit = 0
    window = settings.agent_context_window_tokens if window is None else window

    ordered = sorted(parts, key=lambda p: (p.priority, p.key))
    included: list[tuple[str, str]] = []
    dropped: list[str] = []
    concised: list[str] = []
    used = 0

    for part in ordered:
        full = part.text or ""
        concise = part.concise_text if part.concise_text is not None else None
        if not full and not concise:
            continue
        full_tokens = count_tokens(full)
        if used + full_tokens <= limit:
            included.append((part.key, full))
            used += full_tokens
            continue
        if concise is not None:
            concise_tokens = count_tokens(concise)
            if concise and used + concise_tokens <= limit:
                included.append((part.key, concise))
                used += concise_tokens
                concised.append(part.key)
                continue
        if part.required:
            remaining = limit - used
            source = concise if concise else full
            fitted = _fit_text(source, remaining)
            if fitted:
                included.append((part.key, fitted))
                used += count_tokens(fitted)
            if part.key not in concised:
                concised.append(part.key)
            logger.warning(
                "context required block %s hard-fit for role=%s remaining=%s",
                part.key,
                role,
                remaining,
            )
            continue
        dropped.append(part.key)

    usage = _snapshot(
        included,
        role=role,
        budget=budget,
        window=window,
        run_id=run_id,
        dropped=dropped,
        concised=concised,
        compressed=compressed,
        before_total=before_total,
        after_total=after_total,
        stamp_before=stamp_before,
    )
    # Recount is authoritative; ``used`` can drift by a token on fit boundaries.
    text = "\n\n".join(chunk for _, chunk in included if chunk)
    if tracer is not None:
        tracer.event("context_usage", **usage.model_dump(mode="json"))
    return AssembleResult(
        text=text,
        tokens=usage.total,
        included=[key for key, _ in included],
        dropped=dropped,
        concised=concised,
        usage=usage,
    )
