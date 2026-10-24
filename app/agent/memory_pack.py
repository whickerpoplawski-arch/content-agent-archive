"""Ephemeral memory pack for one planner turn.

The rendered text is prompt context only. Callers must not store it in a
checkpoint, trace, or transcript as if the user had said it. Pinned rows and
``veto`` rows stay even when the token budget is exhausted; situational lines
are dropped first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger("agent.memory_pack")

TOPIC_MIN_SCORE = 0.4
_SKIP_PROFILE_KEYS = {
    "lessons",
    "episodic",
    "procedural",
    "semantic",
    "user_facts",
    "user_preferences",
    "procedural_insights",
}


@dataclass
class MemoryPack:
    text: str
    general: str = ""
    situational: str = ""
    episodic: str = ""
    knowledge: str = ""
    revision: int = 0
    unavailable: bool = False
    dropped: list[str] = field(default_factory=list)
    user_id: str = "local"

    @property
    def ephemeral(self) -> bool:
        return True


def _token_count(text: str) -> int:
    from app.agent.context_builder import count_tokens

    return count_tokens(text or "")


def _overlap(query: str, blob: str) -> float:
    if not query or not blob:
        return 0.0
    try:
        from app.agent.orchestrator import _topic_overlap

        return float(_topic_overlap(query, blob))
    except Exception:  # noqa: BLE001
        return 0.0


def strip_ephemeral_pack(value: Any) -> Any:
    """Remove pack markup from values about to be persisted."""
    import re

    pattern = re.compile(r"<memory-context\b[^>]*>.*?</memory-context>", re.DOTALL)
    if isinstance(value, str):
        return pattern.sub("[ephemeral memory pack omitted]", value)
    if isinstance(value, list):
        return [strip_ephemeral_pack(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_ephemeral_pack(item) for key, item in value.items()}
    return value


def _is_protected(row: dict[str, Any]) -> bool:
    if row.get("pinned") or row.get("kind") == "veto":
        return True
    return str(row.get("id") or "").startswith("lrn_scalar_")


def _fmt_learning(row: dict[str, Any]) -> str:
    tags: list[str] = []
    if row.get("pinned"):
        tags.append("pinned")
    if row.get("kind") == "veto":
        tags.append("veto")
    elif row.get("kind") not in (None, "", "preference"):
        tags.append(str(row.get("kind")))
    prefix = f"[{','.join(tags)}] " if tags else ""
    topics = [str(topic) for topic in (row.get("topics") or []) if str(topic).strip()]
    suffix = f" (topics: {', '.join(topics)})" if topics else ""
    return f"- {prefix}{row.get('text', '')}{suffix}"


def _take(lines: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], list[str]]:
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    used = 0
    protected = [line for line in lines if line.get("protected")]
    optional = [line for line in lines if not line.get("protected")]
    for line in protected:
        kept.append(line)
        used += _token_count(line["text"])
    for line in optional:
        cost = _token_count(line["text"])
        if used + cost > budget:
            dropped.append(line["text"])
            continue
        kept.append(line)
        used += cost
    return kept, dropped


def _global_trim(
    general: list[dict[str, Any]],
    situational: list[dict[str, Any]],
    episodic: list[dict[str, Any]],
    budget: int,
) -> list[str]:
    dropped: list[str] = []

    def total() -> int:
        return sum(
            _token_count(line["text"]) for bucket in (general, situational, episodic) for line in bucket
        )

    victims: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    for bucket in (situational, episodic, general):
        for line in reversed(bucket):
            if not line.get("protected"):
                victims.append((bucket, line))
    for bucket, line in victims:
        if total() <= budget:
            break
        bucket.remove(line)
        dropped.append(line["text"])
    return dropped


def _join(lines: list[dict[str, Any]]) -> str:
    return "\n".join(line["text"] for line in lines if line.get("text"))


def _scalar_lines(profile: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    covered = "\n".join(str(row.get("text") or "") for row in rows if row.get("status") == "active")
    lines = []
    for key in ("audience", "tone"):
        value = profile.get(key)
        if not value:
            continue
        text = f"{key}: {value}"
        if text in covered:
            continue
        lines.append({"text": f"- {text}", "protected": True, "score": 1.0})
    for key, value in profile.items():
        if key in _SKIP_PROFILE_KEYS or key in ("audience", "tone"):
            continue
        if isinstance(value, (str, int, float)) and str(value).strip():
            lines.append({"text": f"- {key}: {value}", "protected": True, "score": 1.0})
    return lines


def _preference_lines(profile: dict[str, Any]) -> list[dict[str, Any]]:
    prefs = profile.get("user_preferences") or []
    if not prefs:
        return []
    agg: dict[str, dict[str, float]] = {}
    for pref in prefs:
        if not isinstance(pref, dict):
            continue
        sig = str(pref.get("goal_signature") or pref.get("goal") or "_")
        bucket = agg.setdefault(sig, {"count": 0, "liked": 0, "score": 0})
        bucket["count"] += 1
        if pref.get("liked"):
            bucket["liked"] += 1
        bucket["score"] += int(pref.get("score") or 0)
    lines = ["User feedback:"]
    for sig, bucket in sorted(agg.items(), key=lambda item: item[1]["count"], reverse=True):
        avg = bucket["score"] / bucket["count"] if bucket["count"] else 0.0
        lines.append(
            f"- domain='{sig}' feedback={int(bucket['count'])} liked={int(bucket['liked'])} "
            f"avg_score={avg:.1f}"
        )
    return [{"text": "\n".join(lines), "protected": False, "score": 0.6}]


def _fallback_facts(profile: dict[str, Any]) -> list[dict[str, Any]]:
    facts = profile.get("user_facts") or {}
    if not isinstance(facts, dict) or not facts:
        return []
    seen: set[str] = set()
    lines = []
    for entry in facts.values():
        if not isinstance(entry, dict):
            continue
        domains = tuple(entry.get("target_domains") or [])
        tone = entry.get("preferred_tone") or ""
        structure = entry.get("preferred_structure") or ""
        avoids = tuple(entry.get("avoid_topics") or [])
        key = (domains, tone, structure, avoids)
        if key in seen:
            continue
        seen.add(key)
        bits = [f"domains={list(domains)}"]
        if tone:
            bits.append(f"tone={tone}")
        if structure:
            bits.append(f"structure={structure}")
        if avoids:
            bits.append(f"avoid={list(avoids)}")
        lines.append({"text": "- " + "; ".join(bits), "protected": False, "score": 0.5})
    return lines


def _fallback_insights(profile: dict[str, Any], goal: str) -> list[dict[str, Any]]:
    rows = []
    for item in profile.get("procedural_insights") or []:
        if not isinstance(item, dict):
            continue
        if (item.get("lifecycle") or "active") in {"superseded", "merged"}:
            continue
        title = str(item.get("title") or "")
        insight = str(item.get("insight") or "")
        text = f"- {title}: {insight}".strip()
        applies = " ".join(str(topic) for topic in (item.get("applies_to") or []))
        score = _overlap(goal, f"{title} {insight} {applies}") if goal else 1.0
        if goal and applies and score < TOPIC_MIN_SCORE:
            continue
        rows.append({"text": text, "protected": False, "score": score})
    return rows


def _procedural_lines(profile: dict[str, Any], goal: str) -> list[dict[str, Any]]:
    items = [item for item in (profile.get("procedural") or []) if isinstance(item, dict)]
    scored = []
    for item in items:
        score = _overlap(goal, str(item.get("goal") or "")) if goal else 1.0
        if goal and score < TOPIC_MIN_SCORE:
            continue
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    lines = []
    for score, item in scored[:8]:
        lines.append(
            {
                "text": (
                    f"- tool chain goal='{item.get('goal', '')}' "
                    f"steps={item.get('actions', [])}"
                ),
                "protected": False,
                "score": score,
            }
        )
    return lines


def _profile_episodic(profile: dict[str, Any], goal: str, limit: int = 12) -> list[dict[str, Any]]:
    entries = profile.get("episodic") or profile.get("lessons") or []
    scored = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        score = _overlap(goal, str(entry.get("goal") or "")) if goal else 1.0
        if goal and score < TOPIC_MIN_SCORE:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:limit]]


def _fmt_episodic(row: dict[str, Any]) -> str:
    titles = row.get("titles") or []
    title = ""
    if titles:
        title = f" title='{titles[0]}'"
        if len(titles) > 1:
            title += f" +{len(titles) - 1}"
    topics = row.get("topics") or []
    topic = f" topics={topics[:4]}" if topics else ""
    snippet = row.get("snippet") or ""
    snip = f" snippet='{snippet[:80]}'" if snippet else ""
    quality = row.get("quality")
    quality_bit = ""
    if isinstance(quality, dict) and quality.get("evaluated"):
        quality_bit = f" quality={quality.get('passed', 0)}/{quality.get('evaluated')}"
    return (
        f"- run={row.get('run_id', '')} goal='{row.get('goal', '')}'"
        f"{title}{topic}{snip} results={row.get('result_count', row.get('results', ''))}"
        f" hits={row.get('hits', 1)}{quality_bit}"
    )


def _render(
    *,
    user_id: str,
    revision: int,
    unavailable: bool,
    banner: str,
    general: str,
    situational: str,
    episodic: str,
    knowledge: str,
) -> str:
    chunks = [
        (
            f'<memory-context user_id="{user_id}" ephemeral="true" '
            f'revision="{revision}" unavailable="{str(bool(unavailable)).lower()}">'
        )
    ]
    if banner:
        chunks.append(banner)
    if general:
        chunks.append("## Preferences\n" + general)
    if situational:
        chunks.append("## Situational\n" + situational)
    if episodic:
        chunks.append("## Episodic\n" + episodic)
    if not general and not situational and not episodic and not banner:
        chunks.append(
            "No learnings matched this turn. An empty pack is not a failure and "
            "does not mean the user has no saved preferences; do not claim you remember nothing."
        )
    chunks.append("</memory-context>")
    # Knowledge stays outside the learning budget and outside the pack tag so a
    # document digest cannot be replayed as something the user said.
    if knowledge.strip():
        body = knowledge if knowledge.startswith("\n") else "\n" + knowledge
        chunks.append("## Knowledge\n" + body.strip())
    return "\n".join(chunks)


def build_pack(
    goal: str,
    budget_tokens: Optional[int] = None,
    *,
    store=None,
    profile: Optional[dict[str, Any]] = None,
    knowledge_text: str = "",
    general_tokens: Optional[int] = None,
    situational_tokens: Optional[int] = None,
    episodic_tokens: Optional[int] = None,
    user_id: Optional[str] = None,
) -> MemoryPack:
    """Build a budgeted pack. ``knowledge_text`` is not charged to the learning budget."""
    profile = profile or {}
    user_id = user_id or getattr(settings, "agent_memory_user_id", None) or "local"
    total_budget = int(
        budget_tokens
        if budget_tokens is not None
        else getattr(settings, "agent_memory_pack_budget_tokens", 1200)
    )
    general_budget = int(
        general_tokens
        if general_tokens is not None
        else getattr(settings, "agent_memory_pack_general_tokens", 400)
    )
    situational_budget = int(
        situational_tokens
        if situational_tokens is not None
        else getattr(settings, "agent_memory_pack_situational_tokens", 400)
    )
    episodic_budget = int(
        episodic_tokens
        if episodic_tokens is not None
        else getattr(settings, "agent_memory_pack_episodic_tokens", 300)
    )
    unavailable = False
    revision = 0
    rows: list[dict[str, Any]] = []
    episodic_rows: list[dict[str, Any]] = []
    # Dream candidates live in SQLite but must not be re-injected from JSON.
    hide_json_insights = False
    try:
        if store is None:
            from app.agent.memory_store import get_profile_store

            store = get_profile_store()
        rows = list(store.list_for_pack())
        revision = int(store.revision())
        episodic_rows = list(store.recall_episodic(goal, limit=12))
        if not episodic_rows and store.count_episodic() == 0:
            episodic_rows = _profile_episodic(profile, goal)
        hide_json_insights = any(
            row.get("kind") == "procedure"
            for row in store.list_learnings(kind="procedure", limit=50)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory pack degraded: %s", exc)
        unavailable = True
        rows = []
        episodic_rows = _profile_episodic(profile, goal)

    active_kinds = {row.get("kind") for row in rows if row.get("status") == "active"}
    general: list[dict[str, Any]] = []
    situational: list[dict[str, Any]] = []
    general.extend(_scalar_lines(profile, rows))
    for row in rows:
        if row.get("status") not in {"active", "superseded"} and not row.get("pinned"):
            continue
        if row.get("status") == "superseded" and not row.get("pinned"):
            continue
        line = {
            "text": _fmt_learning(row),
            "protected": _is_protected(row),
            "score": 1.0,
        }
        situational_hit = False
        if row.get("lane") == "situational" and not line["protected"]:
            blob = row.get("text", "") + " " + " ".join(row.get("topics") or [])
            score = _overlap(goal, blob) if goal else 1.0
            line["score"] = score
            situational_hit = not goal or score >= TOPIC_MIN_SCORE
            if not situational_hit:
                continue
            situational.append(line)
            continue
        general.append(line)
    if "fact" not in active_kinds:
        general.extend(_fallback_facts(profile))
    if not hide_json_insights:
        general.extend(_fallback_insights(profile, goal))
    general.extend(_preference_lines(profile))
    situational.extend(_procedural_lines(profile, goal))
    situational.sort(key=lambda line: line.get("score") or 0, reverse=True)

    # Protected lines lead the section so a later trim cannot reorder them out.
    general.sort(key=lambda line: (1 if line.get("protected") else 0, line.get("score") or 0), reverse=True)

    general, dropped_g = _take(general, general_budget)
    situational, dropped_s = _take(situational, situational_budget)
    episodic_lines = [{"text": _fmt_episodic(row), "protected": False, "score": 1.0} for row in episodic_rows]
    episodic_lines, dropped_e = _take(episodic_lines, episodic_budget)
    dropped_total = _global_trim(general, situational, episodic_lines, total_budget)
    dropped = dropped_g + dropped_s + dropped_e + dropped_total

    general_text = _join(general)
    situational_text = _join(situational)
    episodic_text = _join(episodic_lines)
    banner = ""
    if unavailable:
        banner = (
            "Memory store unavailable this turn. Do not claim you remember nothing; "
            "profile scalars and any lines below still apply."
        )
    text = _render(
        user_id=user_id,
        revision=revision,
        unavailable=unavailable,
        banner=banner,
        general=general_text,
        situational=situational_text,
        episodic=episodic_text,
        knowledge=knowledge_text or "",
    )
    return MemoryPack(
        text=text,
        general=general_text,
        situational=situational_text,
        episodic=episodic_text,
        knowledge=knowledge_text or "",
        revision=revision,
        unavailable=unavailable,
        dropped=dropped,
        user_id=user_id,
    )
