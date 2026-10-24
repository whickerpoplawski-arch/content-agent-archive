"""Build prioritized ContextPart lists for planner calls.

Kept separate from orchestrator/executor so those modules can share the
builder without an import cycle.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.agent.context_builder import ContextPart, count_tokens
from app.core.config import settings

logger = logging.getLogger("agent.context_parts")


def build_planner_parts(
    profile: dict[str, Any],
    goal: str,
    *,
    tools_full: str,
    tools_concise: str,
    system_text: str,
    replan_note: str = "",
    include_skills: Optional[bool] = None,
    store=None,
) -> list[ContextPart]:
    from app.agent.orchestrator import _profile_sections, knowledge_for_tokens

    sections = _profile_sections(profile or {}, goal)
    pack = _maybe_pack(profile or {}, goal, store)
    use_pack = pack is not None
    semantic = "\n".join(
        chunk for chunk in (sections.get("kv"), sections.get("prefs"), sections.get("facts")) if chunk
    )
    skills_on = settings.agent_skills_enabled if include_skills is None else include_skills
    system = system_text
    if skills_on:
        system += (
            "\n- A skill index may list name + description only. When one matches "
            "the goal, the first step MUST be load_skill(name). Do not invent the "
            "skill body; it is not in this prompt.\n"
        )
    parts: list[ContextPart] = [
        ContextPart(key="system", text=system.strip(), priority=1, required=True),
        ContextPart(key="goal", text=f"Goal:\n{goal}", priority=2, required=True),
        ContextPart(
            key="tools",
            text=f"Available tools (name: params schema - description):\n{tools_full}",
            concise_text=f"Available tools (name: description):\n{tools_concise}",
            priority=3,
            required=True,
        ),
    ]
    if replan_note.strip():
        parts.append(ContextPart(key="plan_progress", text=replan_note.strip(), priority=5, required=True))
    if use_pack:
        # Pack sections outrank knowledge so a document digest cannot evict a veto.
        # Legacy insight blocks (priority 10) are folded into the pack.
        _append_pack_parts(parts, pack)
    else:
        if semantic.strip():
            parts.append(ContextPart(key="semantic", text=f"User profile:\n{semantic}", priority=4))
        if sections.get("episodic"):
            parts.append(ContextPart(key="episodic", text=sections["episodic"], priority=6))
    if skills_on:
        from app.agent.skills_loader import skill_index_text

        index = skill_index_text(goal)
        if index:
            parts.append(ContextPart(key="skill_index", text=index, priority=7))
    if sections.get("procedural") and not use_pack:
        parts.append(ContextPart(key="procedural_chains", text=sections["procedural"], priority=8))

    used = sum(count_tokens(part.text) for part in parts)
    remaining = max(0, int(settings.agent_context_budget_planner) - used)
    knowledge = knowledge_for_tokens(goal, remaining) if remaining >= 40 else ""
    if knowledge.strip():
        parts.append(ContextPart(key="knowledge", text=knowledge.strip(), priority=9))
    if sections.get("insights") and not use_pack:
        parts.append(ContextPart(key="insights_raw", text=sections["insights"], priority=10))
    return parts


def _maybe_pack(profile: dict[str, Any], goal: str, store):
    if not settings.agent_memory_pack_enabled:
        return None
    try:
        from app.agent.memory_pack import build_pack
        from app.agent.memory_store import get_profile_store

        return build_pack(
            goal,
            store=store if store is not None else get_profile_store(),
            profile=profile,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory pack skipped: %s", exc)
        return None


def _append_pack_parts(parts: list[ContextPart], pack) -> None:
    header = f"User memory (ephemeral, user_id={pack.user_id}; not persona):"
    if pack.unavailable or not (pack.general or pack.situational or pack.episodic):
        parts.append(ContextPart(key="memory_general", text=pack.text, priority=4))
        return
    if pack.general.strip():
        parts.append(
            ContextPart(key="memory_general", text=f"{header}\n{pack.general}", priority=4)
        )
    if pack.situational.strip():
        parts.append(ContextPart(key="memory_situational", text=pack.situational, priority=5))
    if pack.episodic.strip():
        parts.append(ContextPart(key="memory_episodic", text=pack.episodic, priority=6))
