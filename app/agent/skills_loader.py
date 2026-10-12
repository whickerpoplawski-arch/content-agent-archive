"""Agent Skills (agentskills.io) loader and insight promotion.

Planning sees only name + description. The body is loaded on demand via the
``load_skill`` tool. Matching uses description tokens plus ``metadata.applies_to``
and does not reuse ``_topic_overlap`` as the only score.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger("agent.skills")

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME = 64
_MAX_DESCRIPTION = 1024


@dataclass
class SkillDoc:
    name: str
    description: str
    body: str
    path: Path
    applies_to: list[str] = field(default_factory=list)


def skills_root() -> Path:
    return Path(settings.agent_skills_dir)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        raise ValueError("SKILL.md missing opening frontmatter")
    end = stripped.find("\n---", 3)
    if end < 0:
        raise ValueError("SKILL.md frontmatter not terminated")
    raw = stripped[3:end]
    body = stripped[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    return _parse_simple_yaml(raw), body


def _parse_simple_yaml(block: str) -> dict[str, Any]:
    """Subset parser: scalars, folded/literal blocks, and one nested map."""
    data: dict[str, Any] = {}
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.strip().startswith("#"):
            index += 1
            continue
        if line[0] in " \t":
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest in {">", "|", ""}:
            index += 1
            nested: list[str] = []
            while index < len(lines) and (lines[index].startswith(" ") or lines[index].startswith("\t")):
                nested.append(lines[index].strip())
                index += 1
            if rest in {">", "|"}:
                joiner = " " if rest == ">" else "\n"
                data[key] = joiner.join(item for item in nested if item)
            else:
                sub: dict[str, str] = {}
                for item in nested:
                    if ":" not in item:
                        continue
                    sub_key, _, sub_val = item.partition(":")
                    sub[sub_key.strip()] = sub_val.strip().strip("\"'")
                data[key] = sub
            continue
        data[key] = rest.strip("\"'")
        index += 1
    return data


def _applies_to(meta: Any) -> list[str]:
    if not isinstance(meta, dict):
        return []
    raw = meta.get("applies_to") or meta.get("applies-to") or ""
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).replace("，", ",").split(",") if part.strip()]


def validate_skill_text(text: str, *, directory_name: str) -> list[str]:
    """Return validation errors. Empty list means the file matches the spec subset."""
    errors: list[str] = []
    try:
        front, body = _parse_frontmatter(text)
    except ValueError as exc:
        return [str(exc)]
    name = str(front.get("name") or "").strip()
    description = str(front.get("description") or "").strip()
    if not name:
        errors.append("name is required")
    elif len(name) > _MAX_NAME:
        errors.append("name longer than 64 characters")
    elif not _NAME_RE.match(name):
        errors.append("name must be lowercase letters, digits, and single hyphens")
    elif name != directory_name:
        errors.append(f"name {name!r} must match directory {directory_name!r}")
    if not description:
        errors.append("description is required")
    elif len(description) > _MAX_DESCRIPTION:
        errors.append("description longer than 1024 characters")
    if not body.strip():
        errors.append("skill body is empty")
    return errors


def _doc_from_text(text: str, path: Path) -> SkillDoc:
    front, body = _parse_frontmatter(text)
    name = str(front.get("name") or "").strip()
    description = " ".join(str(front.get("description") or "").split())
    return SkillDoc(
        name=name,
        description=description,
        body=body,
        path=path,
        applies_to=_applies_to(front.get("metadata")),
    )


def discover(skills_dir: Optional[Path] = None) -> list[SkillDoc]:
    root = Path(skills_dir) if skills_dir is not None else skills_root()
    if not root.exists():
        return []
    docs: list[SkillDoc] = []
    for path in sorted(root.glob("*/SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8")
            errors = validate_skill_text(text, directory_name=path.parent.name)
            if errors:
                logger.warning("skip invalid skill %s: %s", path, "; ".join(errors))
                continue
            docs.append(_doc_from_text(text, path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip unreadable skill %s: %s", path, exc)
    return docs


def load_skill(name: str, skills_dir: Optional[Path] = None) -> dict[str, Any]:
    wanted = (name or "").strip()
    for doc in discover(skills_dir):
        if doc.name == wanted:
            return {
                "name": doc.name,
                "description": doc.description,
                "body": doc.body,
                "path": str(doc.path),
                "applies_to": list(doc.applies_to),
            }
    raise KeyError(f"Unknown skill: {wanted}")


def _units(text: str) -> set[str]:
    text = (text or "").lower()
    words = set(re.findall(r"[a-z0-9]{2,}", text))
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            grams.add(run)
        else:
            grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return words | grams


def score_skill(goal: str, doc: SkillDoc) -> float:
    """Description Jaccard plus an applies_to keyword hit. Not _topic_overlap."""
    goal_units = _units(goal)
    desc_units = _units(doc.description)
    if goal_units and desc_units:
        desc_score = len(goal_units & desc_units) / len(goal_units | desc_units)
    else:
        desc_score = 0.0
    applies_hit = 0.0
    for keyword in doc.applies_to:
        if keyword and keyword in (goal or ""):
            applies_hit = 1.0
            break
    if applies_hit == 0.0:
        for keyword in doc.applies_to:
            if keyword and keyword in doc.description and keyword in (goal or ""):
                applies_hit = 1.0
                break
    return 0.7 * desc_score + 0.3 * applies_hit


def rank_skills(goal: str, top_m: Optional[int] = None, skills_dir: Optional[Path] = None) -> list[SkillDoc]:
    limit = settings.agent_skill_index_top_m if top_m is None else top_m
    scored = [(score_skill(goal, doc), doc) for doc in discover(skills_dir)]
    scored = [(score, doc) for score, doc in scored if score > 0]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [doc for _, doc in scored[: max(0, limit)]]


def skill_index_text(goal: str, top_m: Optional[int] = None) -> str:
    ranked = rank_skills(goal, top_m=top_m)
    if not ranked:
        return ""
    lines = [
        "Skill index (name + description only). Call load_skill(name) before relying on a skill; bodies are not inlined."
    ]
    for doc in ranked:
        lines.append(f"- {doc.name}: {doc.description}")
    return "\n".join(lines)


def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= _MAX_NAME and bool(_NAME_RE.match(name))


def skill_name_for(insight: dict[str, Any]) -> str:
    explicit = str(insight.get("skill_name") or "").strip()
    if explicit:
        return explicit
    title = str(insight.get("title") or "insight")
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    ascii_slug = re.sub(r"-{2,}", "-", ascii_slug)
    if _valid_name(ascii_slug):
        return ascii_slug
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"insight-{digest}"


def _description_for(insight: dict[str, Any], name: str) -> str:
    when = str(insight.get("when_to_use") or "").strip()
    insight_text = str(insight.get("insight") or "").strip()
    applies = insight.get("applies_to") or []
    applies_text = ", ".join(str(item) for item in applies if item)
    base = when or insight_text or name
    if applies_text and applies_text not in base:
        base = f"{base} Applies to: {applies_text}."
    base = " ".join(base.split())
    if len(base) > _MAX_DESCRIPTION:
        base = base[: _MAX_DESCRIPTION - 1].rstrip() + "…"
    return base


def _render_skill(name: str, description: str, body: str, applies: list[str]) -> str:
    applies_line = ", ".join(applies) if applies else ""
    meta = f"metadata:\n  applies_to: {applies_line}\n" if applies_line else ""
    # Quote description so colons and CJK stay a single scalar.
    safe_description = description.replace('"', "'")
    return (
        f"---\n"
        f"name: {name}\n"
        f'description: "{safe_description}"\n'
        f"{meta}"
        f"---\n\n"
        f"{body.strip()}\n"
    )


def _body_for(insight: dict[str, Any], name: str) -> str:
    applies = insight.get("applies_to") or []
    bullets = "\n".join(f"- {item}" for item in applies) or "- (unspecified)"
    return (
        f"# {name}\n\n"
        f"## When to use\n{insight.get('when_to_use') or insight.get('insight') or ''}\n\n"
        f"## Insight\n{insight.get('insight') or ''}\n\n"
        f"## Applies to\n{bullets}\n\n"
        f"## Avoid\n- Do not paste this body into the planner prompt; it is loaded via load_skill.\n"
    )


def promote_insight(insight: dict[str, Any], *, skills_dir: Optional[Path] = None) -> Optional[Path]:
    """Write ``skills/<name>/SKILL.md`` when the insight clears the promotion bar.

    Invalid frontmatter is rejected and nothing is written. Returns the path
    or None when the flag is off, the insight is inactive, or validation fails.
    """
    if not settings.agent_skills_enabled:
        return None
    lifecycle = (insight.get("lifecycle") or "active").lower()
    if lifecycle != "active":
        return None
    forced = bool(insight.get("immutable"))
    confidence = float(insight.get("confidence") or 0)
    hits = int(insight.get("hits") or 0)
    if not forced:
        if confidence < float(settings.agent_skill_promote_confidence):
            return None
        if hits < int(settings.agent_skill_promote_hits):
            return None
    name = skill_name_for(insight)
    if not _valid_name(name):
        logger.warning("skill promotion rejected: invalid name %r", name)
        return None
    description = _description_for(insight, name)
    applies = [str(item) for item in (insight.get("applies_to") or []) if str(item).strip()]
    body = _body_for(insight, name)
    text = _render_skill(name, description, body, applies)
    errors = validate_skill_text(text, directory_name=name)
    if errors:
        logger.warning("skill promotion rejected: %s", "; ".join(errors))
        return None
    root = Path(skills_dir) if skills_dir is not None else skills_root()
    dest = root / name / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(".md.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, dest)
    logger.info("promoted insight %s to %s", insight.get("id") or insight.get("title"), dest)
    return dest
