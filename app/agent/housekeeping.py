"""Housekeeping: purge stale trace / console-run / checkpoint files.

Long-running autonomous agents accumulate run artifacts indefinitely; without a
TTL the data/agent directory grows without bound (optimization plan 10.1 / D7).
This module deletes run files older than a retention window and optionally keeps
a small summary of each purged run so audit history is not fully lost.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent.housekeeping")

TRACE_DIR = Path("data") / "agent" / "runs"
CONSOLE_RUNS_DIR = Path("data") / "agent" / "console_runs"
CHECKPOINT_DIR = Path("data") / "agent" / "checkpoints"
RAW_DIR = Path("data") / "raw"
IMAGES_DIR = Path("data") / "output" / "images"

# 阶段5: 产物 TTL —— 只清理可再生的派生文件,绝不动 xhs_state.json / sample_notes.json
CRAWLED_GLOB = "crawled_notes_*.json"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _age_days(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 86400.0
    except OSError:
        return float("inf")


def _extract_summary(data: dict) -> dict:
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    goal = data.get("goal") or result.get("goal", "")
    status = data.get("status") or ("ok" if data.get("result") else "unknown")
    created = data.get("created_at") or data.get("started")
    artifacts = len(result.get("results", [])) if isinstance(result, dict) else 0
    return {
        "run_id": data.get("run_id"),
        "goal": goal,
        "status": status,
        "created_at": created,
        "artifact_count": artifacts,
    }


def _write_summary(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    summary = _extract_summary(data)
    try:
        summary_path = path.with_name(path.stem + ".summary.json")
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _purge_artifact_files(
    directory: Path,
    glob_pattern: str,
    retention_days: int,
    exts: Optional[set[str]] = None,
) -> int:
    """Delete non-run artifacts (crawled notes / images) older than the TTL."""
    if not directory.exists():
        return 0
    removed = 0
    for path in sorted(directory.glob(glob_pattern)):
        if exts is not None and path.suffix.lower() not in exts:
            continue
        if _age_days(path) > retention_days:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("could not remove %s: %s", path, exc)
    return removed


def purge_old_runs(
    retention_days: int = 30,
    keep_summary: bool = True,
    dirs: Optional[list[Path]] = None,
) -> dict:
    """Delete run files older than ``retention_days``; optionally keep summaries.

    When ``dirs`` is omitted (production path) also applies TTL to derived
    artifacts: ``data/raw/crawled_notes_*.json`` and ``data/output/images/*``.
    Explicit ``dirs`` (tests) bypass artifact dirs so they never touch real data.
    Returns a dict with counts of removed files and kept summaries.
    """
    targets = dirs or [TRACE_DIR, CONSOLE_RUNS_DIR, CHECKPOINT_DIR]
    removed = 0
    kept_summaries = 0
    for directory in targets:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json*")):
            name = path.name
            if name.endswith(".tmp"):
                # 13.x: stale tmp files from crashed atomic writes never age out
                # on their own; sweep them whenever housekeeping runs.
                if _age_days(path) > retention_days:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError as exc:
                        logger.warning("could not remove %s: %s", path, exc)
                continue
            if name.endswith(".summary.json"):
                # 13.x: summaries previously accumulated forever (cold zone grew
                # without bound); give them the same TTL — with double grace so
                # a purged run's summary always outlives the run file itself.
                if _age_days(path) > retention_days * 2:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError as exc:
                        logger.warning("could not remove %s: %s", path, exc)
                continue
            if _age_days(path) > retention_days:
                if keep_summary:
                    _write_summary(path)
                    kept_summaries += 1
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("could not remove %s: %s", path, exc)
    artifact_removed = 0
    if dirs is None:
        artifact_removed += _purge_artifact_files(
            RAW_DIR, CRAWLED_GLOB, retention_days
        )
        artifact_removed += _purge_artifact_files(
            IMAGES_DIR, "*", retention_days, exts=IMAGE_EXTS
        )
    return {
        "removed": removed,
        "kept_summaries": kept_summaries,
        "artifact_removed": artifact_removed,
    }