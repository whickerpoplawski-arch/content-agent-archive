"""Rule rubric for topic angles (spec D.6). No LLM judge."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.agent.tool_eval.case import ToolEvalCase

# Spec G4 / D.6. Do not widen this pattern.
FAKE_METRIC_RE = re.compile(r"爆款率|涨粉\d|点赞过万|阅读量\d+万|收藏率|互动率|GMV")

_ACTION_RE = re.compile(r"如何|怎么|清单|避雷|公式|步骤|天|元|分钟")

# Small function-word list. Tokens are dropped only when the whole token matches.
_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "和",
        "与",
        "或",
        "在",
        "是",
        "不",
        "也",
        "就",
        "都",
        "而",
        "及",
        "把",
        "被",
        "对",
        "为",
        "这",
        "那",
        "一个",
        "我们",
        "可以",
        "这个",
        "如果",
        "因为",
        "所以",
        "a",
        "an",
        "the",
        "of",
        "and",
        "to",
        "in",
        "for",
    }
)

_LEGAL_STATUS = frozenset({"ok", "degraded", "refuse", "clarify"})
_MAIN_BUCKETS = frozenset({"must_retrieve", "no_retrieve_ok", "tool_empty"})


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip().casefold()


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def tokens(text: str) -> set[str]:
    """Chinese character 2-grams plus ASCII/number words (spec D.6)."""
    norm = normalize_text(text)
    found: set[str] = set()
    cjk: list[str] = []
    word: list[str] = []

    def flush_cjk() -> None:
        if len(cjk) >= 2:
            found.update(a + b for a, b in zip(cjk, cjk[1:]))
        cjk.clear()

    def flush_word() -> None:
        if word:
            found.add("".join(word))
        word.clear()

    for ch in norm:
        if _is_cjk(ch):
            flush_word()
            cjk.append(ch)
        elif ch.isascii() and ch.isalnum():
            flush_cjk()
            word.append(ch)
        else:
            flush_cjk()
            flush_word()
    flush_cjk()
    flush_word()
    return {token for token in found if token not in _STOPWORDS}


def jaccard(left: str, right: str) -> float:
    a = tokens(left)
    b = tokens(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(b | a)


def fake_metric_hits(text: str) -> int:
    return len(FAKE_METRIC_RE.findall(text or ""))


def output_text(row: dict[str, Any]) -> str:
    parts: list[str] = [str(row.get("status") or ""), str(row.get("message") or "")]
    for angle in row.get("angles") or []:
        if not isinstance(angle, dict):
            parts.append(str(angle))
            continue
        for key in ("angle", "title", "why", "audience"):
            parts.append(str(angle.get(key) or ""))
        grounded = angle.get("grounded_from") or []
        parts.extend(str(item) for item in grounded)
    raw = row.get("raw")
    if isinstance(raw, dict):
        for topic in raw.get("topics") or []:
            if isinstance(topic, dict):
                parts.append(str(topic.get("title") or ""))
                parts.append(str(topic.get("reason") or ""))
    elif raw:
        parts.append(str(raw))
    return "\n".join(parts)


def _angle_blob(angle: dict[str, Any]) -> str:
    return " ".join(str(angle.get(key) or "") for key in ("angle", "title", "why"))


def _nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def score_output(case: ToolEvalCase, row: dict[str, Any]) -> dict[str, Any]:
    """Return the five D.6 bits and their sum. Refuse/clarify stay out of the mean."""
    angles = [item for item in (row.get("angles") or []) if isinstance(item, dict)]
    status = str(row.get("status") or "")
    parsed = bool(row.get("parsed"))
    degraded_case = case.expect_status.value == "degraded"
    count_ok = degraded_case or 3 <= len(angles) <= 5
    parse_point = int(parsed and status in _LEGAL_STATUS and count_ok)

    fields_point = 0
    if angles and all(
        _nonempty(angle.get("angle"))
        and _nonempty(angle.get("title"))
        and _nonempty(angle.get("why"))
        and _nonempty(angle.get("audience"))
        for angle in angles
    ):
        fields_point = 1

    if angles:
        on_theme_hits = sum(1 for angle in angles if jaccard(_angle_blob(angle), case.theme) >= 0.1)
        on_theme_point = int(on_theme_hits / len(angles) >= 0.75)
        action_hits = sum(
            1
            for angle in angles
            if _ACTION_RE.search(str(angle.get("why") or "")) or _ACTION_RE.search(str(angle.get("title") or ""))
        )
        action_point = int(action_hits / len(angles) >= 0.75)
    else:
        on_theme_point = 0
        action_point = 0

    hits = fake_metric_hits(output_text(row))
    titles = [str(angle.get("title") or "") for angle in angles]
    too_close = False
    for i, left in enumerate(titles):
        for right in titles[i + 1 :]:
            if jaccard(left, right) >= 0.9:
                too_close = True
                break
        if too_close:
            break
    honest_point = int(hits == 0 and not too_close)

    score = parse_point + fields_point + on_theme_point + action_point + honest_point
    return {
        "parse": parse_point,
        "fields": fields_point,
        "on_theme": on_theme_point,
        "actionable": action_point,
        "honest": honest_point,
        "score": score,
        "fake_metric_hits": hits,
    }


def in_main_mean(bucket: str) -> bool:
    return bucket in _MAIN_BUCKETS


def behaves(case: ToolEvalCase, row: dict[str, Any]) -> bool:
    """G4-style check for refuse/clarify: right status, no tools, no fake metrics."""
    trace = row.get("trace") or {}
    steps = trace.get("steps") if isinstance(trace, dict) else None
    return (
        str(row.get("status") or "") == case.expect_status.value
        and steps == []
        and int(row.get("tool_calls_total") or 0) == 0
        and int((row.get("rubric") or {}).get("fake_metric_hits") or 0) == 0
    )
