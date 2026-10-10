"""Dreaming-style background consolidation (IMPROVEMENT.md §2.3 阶三, Letta).

After a run completes, a low-priority background task reviews the run's trace
plus existing procedural memories and asks the LLM to distill at most one new
procedural lesson ("对美妆类选题, analyze 后直接 topics 效果更好") that generalizes
beyond the current goal. Distinct from _remember_lesson, which records the
per-goal chain verbatim: consolidation extracts transferable insight.

Failure is never fatal: any exception logs a warning and leaves memory intact.
Dedup reuses the orchestrator's goal-signature machinery on the lesson title.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("agent.consolidation")

MAX_CONSOLIDATION_ATTEMPTS = 1


class ProceduralInsight(BaseModel):
    """One distilled, transferable procedural lesson from a completed run."""

    title: str = Field(..., description="短标题,如 '美妆选题先分析后直出效果更好'")
    insight: str = Field(
        ..., description="可迁移的做法/教训,一句话,说明为什么与何时适用"
    )
    applies_to: list[str] = Field(
        default_factory=list, description="适用主题域关键词,如 ['美妆', '护肤']"
    )


def _signature(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", (text or "").lower())


def _build_prompt(goal: str, actions: list[str], outputs: dict, existing: list[dict]) -> str:
    chains = "; ".join(
        f"{p.get('goal', '')}: {p.get('actions', [])}" for p in existing[-5:]
    )
    sample = str(outputs.get("results", []))[:600]
    quality = str(outputs.get("quality_report", ""))[:300]
    return (
        "Review this completed content-generation run and distill ONE transferable "
        "procedural lesson (how to work better next time), not a repeat of the goal.\n"
        f"Goal: {goal}\n"
        f"Tool chain executed: {actions}\n"
        f"Output sample: {sample}\n"
        f"Quality report: {quality}\n"
        f"Existing procedural lessons (do NOT duplicate): {chains or '(none)'}\n"
        "If nothing new and useful can be distilled, set title to 'SKIP'."
    )


def _store_insight(long_term, insight: ProceduralInsight, goal: str) -> bool:
    """Append the insight as a procedural entry unless dedup says it exists.

    Reuses the goal-signature dedup: a stored entry with the same normalized
    title (or the same goal signature) suppresses the new one.
    """
    if _signature(insight.title) in ("SKIP", "skip") or not _signature(insight.title):
        return False
    sig = _signature(insight.title)
    existing = list(long_term.get("procedural_insights", []) or [])
    if any(_signature(e.get("title", "")) == sig for e in existing):
        return False
    existing.append(
        {
            "title": insight.title,
            "insight": insight.insight,
            "applies_to": insight.applies_to,
            "goal": goal,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "hits": 1,
        }
    )
    long_term.set("procedural_insights", existing[-20:])
    return True


def _insights_text(profile: dict[str, Any]) -> str:
    """Format stored insights for injection into the planner prompt."""
    insights = profile.get("procedural_insights") or []
    if not insights:
        return ""
    lines = ["Distilled insights from past runs (apply when relevant):"]
    for item in insights[-5:]:
        applies = ",".join(item.get("applies_to", []) or [])
        suffix = f" (applies to: {applies})" if applies else ""
        lines.append(f"- {item.get('title', '')}: {item.get('insight', '')}{suffix}")
    return "\n".join(lines)


async def consolidate(
    backend,
    long_term,
    goal: str,
    actions: list[str],
    outputs: dict[str, Any],
) -> Optional[ProceduralInsight]:
    """One dreaming pass; returns the stored insight (None = nothing stored).

    Never raises: consolidation failures are logged and swallowed so the run
    path and background pool are unaffected.
    """
    try:
        existing = list(long_term.get("procedural", []) or [])
        prompt = _build_prompt(goal, actions, outputs, existing)
        insight = await backend.generate(prompt, ProceduralInsight)
        if not _store_insight(long_term, insight, goal):
            return None
        logger.info("consolidated procedural insight: %s", insight.title)
        return insight
    except Exception as exc:  # noqa: BLE001
        logger.warning("consolidation skipped (non-fatal): %s", exc)
        return None


def submit_consolidation(
    pool,
    backend,
    long_term,
    goal: str,
    actions: list[str],
    outputs: dict[str, Any],
) -> None:
    """Fire-and-forget consolidation on a worker thread (runs its own loop)."""

    def _job() -> None:
        async def _run() -> None:
            try:
                await consolidate(backend, long_term, goal, actions, outputs)
            finally:
                # 巩固线程跑自己的 loop: 用完即关客户端, 与主 run 的客户端互不共享。
                closer = getattr(backend, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("consolidation backend close failed: %s", exc)

        try:
            asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001
            logger.warning("consolidation job failed (non-fatal): %s", exc)

    try:
        pool.submit(_job)
    except RuntimeError as exc:  # pool shut down
        logger.warning("consolidation not submitted: %s", exc)
