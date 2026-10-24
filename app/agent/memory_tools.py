"""Agent tools for explicit memory: learn, forget, recall, pin, edit.

Handlers talk to the bound ProfileStore (the run's store) and never raise on a
scrub refusal — the caller hears that memory was left unchanged.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agent.memory_scrub import ScrubRejected
from app.agent.memory_store import get_profile_store

logger = logging.getLogger("agent.memory_tools")


class MemoryLearnParams(BaseModel):
    text: str = Field(..., description="What to remember, in the user's words")
    kind: str = Field(
        "preference",
        description="preference | fact | procedure | correction | veto",
    )
    lane: str = Field("general", description="general (every turn) | situational (topic match)")
    topics: list[str] = Field(default_factory=list, description="Topic tags for situational recall")
    pinned: bool = Field(False, description="Keep this line even when the pack is tight")


class MemoryForgetParams(BaseModel):
    learning_id: str = Field(..., description="Id returned by memory_learn or memory_recall")


class MemoryRecallParams(BaseModel):
    query: str = Field("", description="Goal or topic to recall. Empty lists recent active rows.")
    limit: int = Field(8, ge=1, le=20)


class MemoryPinParams(BaseModel):
    learning_id: str
    pinned: bool = True


class MemoryEditParams(BaseModel):
    learning_id: str
    text: str = ""
    kind: str = ""
    lane: str = ""
    topics: list[str] = Field(default_factory=list)


def _refusal(exc: Exception) -> dict:
    reason = getattr(exc, "reason", None) or str(exc)
    return {
        "ok": False,
        "refused": True,
        "reason": reason,
        "memory": "unchanged",
    }


async def memory_learn(params: MemoryLearnParams, store=None) -> dict:
    try:
        row = (store or get_profile_store()).learn(
            params.text,
            kind=params.kind,
            lane=params.lane,
            topics=list(params.topics),
            pinned=params.pinned,
            source="user",
            status="active",
        )
    except (ScrubRejected, ValueError) as exc:
        return _refusal(exc)
    return {"ok": True, "learning": row}


async def memory_forget(params: MemoryForgetParams, store=None) -> dict:
    found = (store or get_profile_store()).forget(params.learning_id)
    if not found:
        return {"ok": False, "error": "learning not found", "learning_id": params.learning_id}
    return {"ok": True, "forgotten": params.learning_id, "memory": "kept other rows"}


async def memory_recall(params: MemoryRecallParams, store=None) -> dict:
    try:
        payload = (store or get_profile_store()).recall(params.query, limit=params.limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory recall failed: %s", exc)
        return {
            "ok": False,
            "unavailable": True,
            "reason": "memory store unavailable",
            "note": "Do not claim you remember nothing; recall failed this turn.",
            "learnings": [],
            "episodic": [],
        }
    return {"ok": True, **payload}


async def memory_pin(params: MemoryPinParams, store=None) -> dict:
    row = (store or get_profile_store()).pin(params.learning_id, params.pinned)
    if row is None:
        return {"ok": False, "error": "learning not found", "learning_id": params.learning_id}
    return {"ok": True, "learning": row}


async def memory_edit(params: MemoryEditParams, store=None) -> dict:
    try:
        row = (store or get_profile_store()).edit(
            params.learning_id,
            text=params.text or None,
            kind=params.kind or None,
            lane=params.lane or None,
            topics=list(params.topics) if params.topics else None,
        )
    except (ScrubRejected, ValueError) as exc:
        return _refusal(exc)
    if row is None:
        return {"ok": False, "error": "learning not found", "learning_id": params.learning_id}
    return {"ok": True, "learning": row}


def iter_memory_tools():
    """(name, description, params schema, handler) for the tool registry."""
    return (
        (
            "memory_learn",
            "Remember a durable user preference, fact, correction, procedure, or veto. Scrubs secrets.",
            MemoryLearnParams,
            memory_learn,
        ),
        (
            "memory_forget",
            "Forget one learning by id. Other memories stay; this does not wipe the profile.",
            MemoryForgetParams,
            memory_forget,
        ),
        (
            "memory_recall",
            "Recall learnings and past runs for a topic. An empty result is not 'I remember nothing'.",
            MemoryRecallParams,
            memory_recall,
        ),
        (
            "memory_pin",
            "Pin or unpin a learning so the pack keeps it ahead of situational notes.",
            MemoryPinParams,
            memory_pin,
        ),
        (
            "memory_edit",
            "Edit a learning's text, kind, lane, or topics. Secrets are refused.",
            MemoryEditParams,
            memory_edit,
        ),
    )
