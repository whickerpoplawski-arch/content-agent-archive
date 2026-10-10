"""Rebuild data/agent/profile.json from real console-run history.

Why: during the memory-layer upgrade a test-suite run wrote through the real
profile (audience/tone overwritten, episodic merged with test goals). This script
reconstructs the profile from the authoritative source of truth - the completed
real console runs in data/agent/console_runs/*.json - using the upgraded
_dedup_by_goal (主题指纹去重) so the rebuilt lessons are consistent with the new
memory behavior.

Usage: python scripts/rebuild_profile.py [--dry-run]
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.agent.memory import LongTermMemory
from app.agent.orchestrator import _dedup_by_goal, _goal_signature

CONSOLE_RUNS = Path("data/agent/console_runs")
PROFILE = Path("data/agent/profile.json")

REAL_MARKERS = ("西电", "研究生", "实习", "西安电子科技大学")


def _is_real_run(r: dict) -> bool:
    goal = r.get("goal") or ""
    audience = r.get("audience") or ""
    return any(m in goal or m in audience for m in REAL_MARKERS)


def main(dry_run: bool) -> None:
    runs: list[dict] = []
    for path in sorted(CONSOLE_RUNS.glob("*.json")):
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("status") == "completed" and _is_real_run(r):
            runs.append(r)

    if not runs:
        print("no real completed runs found; aborting")
        return

    audience = Counter(r.get("audience") or "" for r in runs).most_common(1)[0][0]
    tone = Counter(r.get("tone") or "" for r in runs).most_common(1)[0][0]

    episodic: list[dict] = []
    procedural: list[dict] = []
    for r in runs:
        goal = r.get("goal") or ""
        ts = r.get("updated_at") or r.get("created_at") or datetime.now(timezone.utc).isoformat()
        results = len((r.get("outputs") or {}).get("results", []) or [])
        lesson = {
            "goal": goal,
            "signature": _goal_signature(goal),
            "steps_ok": True,
            "results": results,
            "updated_at": ts,
            "hits": 1,
        }
        episodic.append(lesson)
        proc = {
            "goal": goal,
            "signature": _goal_signature(goal),
            "actions": ["crawl_notes", "analyze_notes", "generate_topics", "generate_contents"],
            "results": results,
            "updated_at": ts,
            "hits": 1,
        }
        procedural.append(proc)

    episodic = _dedup_by_goal(episodic)
    procedural = _dedup_by_goal(procedural)[-20:]

    profile = {
        "audience": audience,
        "tone": tone,
        "episodic": episodic,
        "procedural": procedural,
        # 派生键镜像 episodic(与 _remember_lesson 行为一致)。
        "lessons": episodic,
        # 派生/学习键:无真实来源(测试污染过),置空,后续真实 run 再生成。
        "user_facts": {},
        "user_preferences": [],
    }
    if dry_run:
        print(json.dumps(_summary(profile), ensure_ascii=False, indent=2))
        return

    memory = LongTermMemory(PROFILE)
    for k, v in profile.items():
        memory.set(k, v)
    print("rebuilt:", json.dumps(_summary(profile), ensure_ascii=False, indent=2))


def _summary(profile: dict) -> dict:
    return {
        "audience": profile["audience"],
        "tone": profile["tone"],
        "episodic_count": len(profile["episodic"]),
        "episodic": [
            {"goal": e["goal"][:40], "hits": e.get("hits", 1)}
            for e in profile["episodic"]
        ],
        "procedural_count": len(profile["procedural"]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(args.dry_run)
