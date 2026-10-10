"""Goal-to-focus helpers (optimization plan 12.4 / 12.5).

Best-effort extraction of target-domain keywords and target counts from a user
goal, plus repeated-reason streak detection used by the oscillation guard.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_STRIP_GOAL_RE = re.compile(
    r"为.+?(?:学生|用户|人群|粉丝|读者|宝妈|上班族|打工人).*?(?:生成|写|创作|制作|产出)\s*\d*\s*篇?"
)
_QUOTED_OPEN = "".join(chr(c) for c in (0x300c, 0x201c, 0x0022, 0x0027))  # 「 “ " '
_QUOTED_CLOSE = "".join(chr(c) for c in (0x300d, 0x201d, 0x0022, 0x0027))  # 」 ” " '
_QUOTED_RE = re.compile("[" + _QUOTED_OPEN + "]([^" + _QUOTED_CLOSE + "]{2,16})[" + _QUOTED_CLOSE + "]")
_ABOUT_RE = re.compile(r"关于(.{2,16}?)(?:的|，|,|。|；|;|$)")
_TRAILING_RE = re.compile(r"(文案|文章|笔记|分享|内容|种草|短视频|图文)$")
_TARGET_COUNT_RE = re.compile(r"(\d+)\s*篇")


def _extract_focus(goal: str) -> list[str]:
    """Best-effort extraction of target-domain keywords from a goal text."""
    if not goal:
        return []
    goal = goal.strip()
    focus: list[str] = []
    for m in _QUOTED_RE.finditer(goal):
        kw = m.group(1).strip()
        if kw and kw not in focus:
            focus.append(kw)
    if not focus:
        m = _ABOUT_RE.search(goal)
        if m:
            kw = m.group(1).strip()
            if kw and kw not in focus:
                focus.append(kw)
    if not focus:
        stripped = _STRIP_GOAL_RE.sub("", goal)
        stripped = _TRAILING_RE.sub("", stripped).strip(" ，,。；;：:、")
        if 2 <= len(stripped) <= 20 and stripped not in focus:
            focus.append(stripped)
    return focus


def _extract_target_count(goal: str) -> int:
    m = _TARGET_COUNT_RE.search(goal or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def _same_reason_streak(history: list[str]) -> int:
    """Number of consecutive identical (normalized) reasons from the end."""
    if not history:
        return 0
    last = _normalize_reason(history[-1])
    streak = 0
    for reason in reversed(history):
        if _normalize_reason(reason) == last:
            streak += 1
        else:
            break
    return streak


def _normalize_reason(reason: str) -> str:
    return re.sub(r"\s+", " ", (reason or "").strip()).lower()
