"""Build prioritized ContextPart lists for planner calls.

Kept separate from orchestrator/executor so those modules can share the
builder without an import cycle.
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.context_builder import ContextPart, count_tokens
from app.core.config import settings


def build_planner_parts(
    profile: dict[str, Any],
    goal: str,
    *,
    tools_full: str,
    tools_concise: str,
    system_text: str,
    replan_note: str = "",
    include_skills: Optional[bool] = None,
) -> list[ContextPart]:
    from app.agent.orchestrator import _profile_sections, knowledge_for_tokens

    sections = _profile_sections(profile or {}, goal)
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
    if semantic.strip():
        parts.append(ContextPart(key="semantic", text=f"User profile:\n{semantic}", priority=4))
    if sections.get("episodic"):
        parts.append(ContextPart(key="episodic", text=sections["episodic"], priority=6))
    if skills_on:
        from app.agent.skills_loader import skill_index_text

        index = skill_index_text(goal)
        if index:
            parts.append(ContextPart(key="skill_index", text=index, priority=7))
    if sections.get("procedural"):
        parts.append(ContextPart(key="procedural_chains", text=sections["procedural"], priority=8))

    used = sum(count_tokens(part.text) for part in parts)
    remaining = max(0, int(settings.agent_context_budget_planner) - used)
    knowledge = knowledge_for_tokens(goal, remaining) if remaining >= 40 else ""
    if knowledge.strip():
        parts.append(ContextPart(key="knowledge", text=knowledge.strip(), priority=9))
    if sections.get("insights"):
        parts.append(ContextPart(key="insights_raw", text=sections["insights"], priority=10))
    return parts
