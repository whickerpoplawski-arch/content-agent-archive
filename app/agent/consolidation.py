"""Dreaming-style background consolidation (IMPROVEMENT.md §2.3, mem0 Dream).

After a run completes, a low-priority background task reviews the trace and
emits up to three memory operations: ADD, UPDATE, SUPERSEDE, or NOOP. History
is never physically deleted. Near-duplicate insights are merged by exact title
signature and by token Jaccard. High-confidence active insights can be promoted
to SKILL.md when ``agent_skills_enabled`` is on.

Failure is never fatal: any exception logs a warning and leaves the run result
alone. A fresh OpenAI/OpenRouter client is required when this runs on another
event loop (see orchestrator).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger("agent.consolidation")

MAX_CONSOLIDATION_ATTEMPTS = 1
_INACTIVE = {"superseded", "merged"}


class ProceduralInsight(BaseModel):
    """Legacy single-insight schema. Still accepted when a backend has no DreamResult factory."""

    title: str = Field(..., description="短标题,如 '美妆选题先分析后直出效果更好'")
    insight: str = Field(
        ..., description="可迁移的做法/教训,一句话,说明为什么与何时适用"
    )
    applies_to: list[str] = Field(
        default_factory=list, description="适用主题域关键词,如 ['美妆', '护肤']"
    )


class DreamOp(BaseModel):
    op: Literal["ADD", "UPDATE", "SUPERSEDE", "NOOP"] = "ADD"
    target_id: Optional[str] = None
    title: str = ""
    insight: str = ""
    applies_to: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    when_to_use: str = ""


class DreamResult(BaseModel):
    """One dreaming pass: at most three operations. NOOP when nothing new is worth storing."""

    ops: list[DreamOp] = Field(default_factory=list)


def _signature(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", (text or "").lower())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return "pins_" + uuid.uuid4().hex[:10]


def normalize_insight(entry: dict[str, Any]) -> dict[str, Any]:
    """Fill id/lifecycle on legacy rows. Does not delete fields."""
    item = dict(entry or {})
    title = str(item.get("title") or "")
    if not item.get("id"):
        sig = _signature(title) or uuid.uuid4().hex[:8]
        item["id"] = "pins_" + sig[:10]
    lifecycle = str(item.get("lifecycle") or "active").lower()
    if lifecycle not in {"active", "superseded", "merged"}:
        lifecycle = "active"
    item["lifecycle"] = lifecycle
    item.setdefault("insight", "")
    item.setdefault("applies_to", [])
    item.setdefault("when_to_use", "")
    item.setdefault("confidence", 0.5)
    item.setdefault("hits", 1)
    item.setdefault("superseded_by", None)
    item.setdefault("merged_into", None)
    item.setdefault("created_at", item.get("updated_at") or _now())
    item.setdefault("updated_at", item.get("created_at") or _now())
    return item


def migrate_insights(long_term) -> list[dict[str, Any]]:
    """Read procedural_insights and persist lifecycle/id when legacy rows lack them."""
    raw = list(long_term.get("procedural_insights", []) or [])
    if not raw and long_term.get("procedural_insights") is None:
        return []
    normalized = [normalize_insight(item) for item in raw]
    if normalized != raw:
        long_term.set("procedural_insights", normalized)
    return normalized


def list_active(long_term) -> list[dict[str, Any]]:
    """Active insights only. Reading migrates legacy rows in place."""
    items = migrate_insights(long_term)
    return [item for item in items if item.get("lifecycle", "active") not in _INACTIVE]


def _norm_units(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", (text or "").lower())
    words = set(re.findall(r"[a-z0-9]{2,}", compact))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    grams: set[str] = set()
    if len(cjk) == 1:
        grams.add(cjk)
    elif cjk:
        grams.update(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return words | grams


def insight_jaccard(a: str, b: str) -> float:
    left, right = _norm_units(a), _norm_units(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_skip(title: str) -> bool:
    return _signature(title) in {"", "skip"}


def _find(items: list[dict[str, Any]], target_id: Optional[str], title: str) -> Optional[dict[str, Any]]:
    if target_id:
        for item in items:
            if item.get("id") == target_id:
                return item
    sig = _signature(title)
    if not sig:
        return None
    for item in items:
        if item.get("lifecycle") in _INACTIVE:
            continue
        if _signature(item.get("title", "")) == sig:
            return item
    return None


def _fuzzy_active(items: list[dict[str, Any]], insight: str) -> Optional[dict[str, Any]]:
    threshold = float(settings.agent_dream_fuzzy_jaccard)
    best: Optional[dict[str, Any]] = None
    best_score = 0.0
    for item in items:
        if item.get("lifecycle") in _INACTIVE:
            continue
        score = insight_jaccard(insight, str(item.get("insight") or ""))
        if score >= threshold and score > best_score:
            best = item
            best_score = score
    return best


def _make_entry(op: DreamOp, goal: str) -> dict[str, Any]:
    now = _now()
    return {
        "id": _new_id(),
        "title": op.title,
        "insight": op.insight,
        "applies_to": list(op.applies_to),
        "when_to_use": op.when_to_use or op.insight,
        "confidence": float(op.confidence),
        "hits": 1,
        "source_goal": goal,
        "goal": goal,
        "created_at": now,
        "updated_at": now,
        "lifecycle": "active",
        "superseded_by": None,
        "merged_into": None,
    }


def _touch(item: dict[str, Any], op: DreamOp) -> None:
    if op.insight:
        item["insight"] = op.insight
    if op.applies_to:
        item["applies_to"] = list(op.applies_to)
    if op.when_to_use:
        item["when_to_use"] = op.when_to_use
    if op.title and not _is_skip(op.title):
        item["title"] = op.title
    item["hits"] = int(item.get("hits") or 1) + 1
    item["confidence"] = max(float(item.get("confidence") or 0), float(op.confidence))
    item["updated_at"] = _now()
    item["lifecycle"] = "active"


def _maybe_promote(item: dict[str, Any]) -> None:
    try:
        from app.agent.skills_loader import promote_insight

        path = promote_insight(item)
        if path is not None:
            item["skill_path"] = str(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill promotion skipped (non-fatal): %s", exc)


def apply_dream_ops(long_term, ops: list[DreamOp], goal: str) -> Optional[dict[str, Any]]:
    """Apply dreaming ops. Superseded rows stay on disk. Returns the last write, or None."""
    had_key = long_term.get("procedural_insights") is not None
    items = [normalize_insight(item) for item in list(long_term.get("procedural_insights", []) or [])]
    stored: Optional[dict[str, Any]] = None
    changed = False
    for op in list(ops or [])[:3]:
        if op.op == "NOOP" or _is_skip(op.title) and op.op == "ADD":
            continue
        if op.op == "UPDATE":
            target = _find(items, op.target_id, op.title)
            if target is None or target.get("lifecycle") in _INACTIVE:
                continue
            _touch(target, op)
            _maybe_promote(target)
            stored = target
            changed = True
            continue
        if op.op == "SUPERSEDE":
            target = _find(items, op.target_id, op.title)
            new_item = _make_entry(op, goal) if op.insight or op.title else None
            if target is not None:
                target["lifecycle"] = "superseded"
                target["updated_at"] = _now()
                if new_item is not None:
                    target["superseded_by"] = new_item["id"]
                changed = True
            if new_item is not None and not _is_skip(new_item["title"]):
                items.append(new_item)
                _maybe_promote(new_item)
                stored = new_item
                changed = True
            continue
        # ADD
        if _is_skip(op.title):
            continue
        exact = _find(items, None, op.title)
        if exact is not None and exact.get("lifecycle") not in _INACTIVE:
            _touch(exact, op)
            _maybe_promote(exact)
            stored = exact
            changed = True
            continue
        fuzzy = _fuzzy_active(items, op.insight) if op.insight else None
        if fuzzy is not None:
            if float(op.confidence) >= float(fuzzy.get("confidence") or 0):
                new_item = _make_entry(op, goal)
                fuzzy["lifecycle"] = "superseded"
                fuzzy["superseded_by"] = new_item["id"]
                fuzzy["updated_at"] = _now()
                items.append(new_item)
                _maybe_promote(new_item)
                stored = new_item
                changed = True
            continue
        new_item = _make_entry(op, goal)
        items.append(new_item)
        _maybe_promote(new_item)
        stored = new_item
        changed = True
    if not changed and not had_key:
        return None
    if changed or (had_key and items != list(long_term.get("procedural_insights", []) or [])):
        long_term.set("procedural_insights", items)
    return stored


def _supports(backend, schema: type) -> bool:
    factories = getattr(backend, "_factories", None)
    if factories is None:
        return True
    return schema in factories


def _build_prompt(goal: str, actions: list[str], outputs: dict, existing: list[dict]) -> str:
    catalog = []
    for item in existing:
        if item.get("lifecycle") in _INACTIVE:
            continue
        catalog.append(
            f"- id={item.get('id')} title={item.get('title')} "
            f"confidence={item.get('confidence')} hits={item.get('hits')} "
            f"insight={item.get('insight')}"
        )
    sample = str(outputs.get("results", []))[:600]
    quality = str(outputs.get("quality_report", ""))[:300]
    return (
        "Review this completed content-generation run and emit 1 to 3 memory operations. "
        "Use ADD for a new transferable procedural lesson, UPDATE when target_id should "
        "be refreshed, SUPERSEDE when a new lesson replaces target_id, or a single NOOP "
        "when nothing new is worth storing. Do not repeat the goal verbatim.\n"
        f"Goal: {goal}\n"
        f"Tool chain executed: {actions}\n"
        f"Output sample: {sample}\n"
        f"Quality report: {quality}\n"
        f"Active procedural insights (do NOT duplicate; reference id for UPDATE/SUPERSEDE):\n"
        + ("\n".join(catalog) if catalog else "(none)")
    )


def _assemble_consolidator_prompt(prompt: str, *, goal: str, tracer, run_id: str) -> str:
    from app.agent.context_builder import ContextPart, assemble, budgets_for

    soft, hard = budgets_for("consolidator")
    parts = [
        ContextPart(
            key="system",
            text=(
                "You distill transferable procedural lessons for a Xiaohongshu content agent. "
                "Return only the DreamResult schema."
            ),
            priority=1,
            required=True,
        ),
        ContextPart(key="goal", text=f"Goal:\n{goal}", priority=2, required=True),
        ContextPart(key="observations", text=prompt, priority=5),
    ]
    assembled = assemble(
        parts,
        role="consolidator",
        budget=soft,
        hard=hard,
        window=settings.agent_context_window_tokens,
        run_id=run_id or getattr(tracer, "run_id", "") or "",
        tracer=tracer,
    )
    return assembled.text


async def _generate_ops(backend, prompt: str) -> list[DreamOp]:
    if _supports(backend, DreamResult):
        result = await backend.generate(prompt, DreamResult)
        return list(result.ops)[:3]
    insight = await backend.generate(prompt, ProceduralInsight)
    if _is_skip(insight.title):
        return [DreamOp(op="NOOP", title="SKIP")]
    return [
        DreamOp(
            op="ADD",
            title=insight.title,
            insight=insight.insight,
            applies_to=list(insight.applies_to),
            confidence=0.55,
            when_to_use=insight.insight[:240],
        )
    ]


def _insights_text(profile: dict[str, Any]) -> str:
    """Format active insights for the legacy profile string. Superseded rows are hidden."""
    insights = [
        item
        for item in (profile.get("procedural_insights") or [])
        if (item.get("lifecycle") or "active") not in _INACTIVE
    ]
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
    tracer=None,
) -> Optional[dict[str, Any]]:
    """One dreaming pass. Returns the stored insight dict, or None.

    Never raises: consolidation failures are logged and swallowed so the run
    path and background pool are unaffected. Skills written here are visible
    on the next run, not mid-run.
    """
    try:
        existing = list_active(long_term)
        prompt = _build_prompt(goal, actions, outputs, existing)
        if settings.agent_context_builder_enabled:
            prompt = _assemble_consolidator_prompt(
                prompt,
                goal=goal,
                tracer=tracer,
                run_id=getattr(tracer, "run_id", "") or "",
            )
        ops = await _generate_ops(backend, prompt)
        stored = apply_dream_ops(long_term, ops, goal)
        _record_learning_cursor(long_term)
        if stored:
            logger.info("consolidated procedural insight: %s", stored.get("title"))
        return stored
    except Exception as exc:  # noqa: BLE001
        logger.warning("consolidation skipped (non-fatal): %s", exc)
        return None


def _record_learning_cursor(long_term) -> None:
    """Fold near-duplicate learnings and advance the consolidate cursor.

    Local SQLite only — no extra model call, so it does not hold the turn.
    """
    try:
        long_term.store.consolidate_learnings()
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning consolidate skipped (non-fatal): %s", exc)


def submit_consolidation(
    pool,
    backend,
    long_term,
    goal: str,
    actions: list[str],
    outputs: dict[str, Any],
    tracer=None,
) -> None:
    """Fire-and-forget consolidation on a worker thread (runs its own loop)."""

    def _job() -> None:
        async def _run() -> None:
            try:
                await consolidate(backend, long_term, goal, actions, outputs, tracer)
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
