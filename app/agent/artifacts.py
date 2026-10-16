"""Offload large tool results to disk and hand the agent a small handle.

The full payload stays readable through ``read_artifact``. Prompt assembly and
``$latest.<tool>`` (no field path) see the handle, not the raw blob.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger("agent.artifacts")

_ACTION_RE = re.compile(r"[^a-z0-9_]+")


class ArtifactRef(BaseModel):
    artifact: str
    summary: str
    count: Optional[int] = None
    preview: str = ""
    content_type: str = "application/json"
    artifact_handle: bool = Field(default=True, alias="_artifact_handle")

    model_config = {"populate_by_name": True}


def artifacts_root() -> Path:
    return Path(settings.agent_artifacts_dir)


def is_artifact_handle(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value.get("_artifact_handle"))
        and isinstance(value.get("artifact"), str)
        and bool(value.get("artifact"))
    )


def _safe_action(action: str) -> str:
    cleaned = _ACTION_RE.sub("_", (action or "tool").lower()).strip("_")
    return cleaned or "tool"


def _json_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def serialized_len(obj: Any) -> int:
    try:
        return len(_json_text(obj))
    except Exception:  # noqa: BLE001
        return len(str(obj))


def _summary(action: str, obj: Any, preview: str) -> tuple[str, Optional[int]]:
    count: Optional[int] = None
    if isinstance(obj, dict):
        if isinstance(obj.get("count"), int):
            count = obj["count"]
        elif isinstance(obj.get("contents"), list):
            count = len(obj["contents"])
        elif isinstance(obj.get("items"), list):
            count = len(obj["items"])
        elif isinstance(obj.get("topics"), list):
            count = len(obj["topics"])
    elif isinstance(obj, list):
        count = len(obj)
    summary = f"{action} ok"
    if count is not None:
        summary += f": count={count}"
    if preview:
        summary += f"; preview={preview[:80]}"
    return summary, count


def _resolve_under_root(path: str, root: Optional[Path] = None) -> Path:
    base = (root or artifacts_root()).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = base / target
    target = target.resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"artifact path escapes {base}")
    return target


def save_artifact(
    run_id: str,
    step_idx: int,
    action: str,
    obj: Any,
    *,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """Atomically write ``obj`` and return an ArtifactRef dict (with handle flag)."""
    base = root or artifacts_root()
    directory = base / (run_id or "adhoc")
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{int(step_idx)}_{_safe_action(action)}.json"
    path = directory / filename
    payload = _json_text(obj)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
    preview = payload[:500]
    summary, count = _summary(action, obj, preview)
    handle = {
        "_artifact_handle": True,
        "artifact": str(path),
        "summary": summary,
        "count": count,
        "preview": preview,
        "content_type": "application/json",
    }
    if isinstance(obj, dict) and isinstance(obj.get("quality_report"), (dict, list)):
        report_text = _json_text(obj["quality_report"])
        if len(report_text) <= 2000:
            handle["quality_report"] = obj["quality_report"]
    return handle


def load_artifact(path: str, *, root: Optional[Path] = None) -> Any:
    target = _resolve_under_root(path, root)
    return json.loads(target.read_text(encoding="utf-8"))


def materialize_result(result: Any) -> Any:
    """Return the full payload when ``result`` is a handle; otherwise ``result``."""
    if not is_artifact_handle(result):
        return result
    try:
        return load_artifact(result["artifact"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact materialize failed for %s: %s", result.get("artifact"), exc)
        return result


def maybe_offload(run_id: str, step_idx: int, action: str, result: Any) -> Any:
    """Replace oversized results with a handle when the offload flag is on."""
    if not settings.agent_artifact_offload_enabled:
        return result
    if is_artifact_handle(result):
        return result
    if serialized_len(result) <= int(settings.agent_artifact_chars):
        return result
    try:
        return save_artifact(run_id, step_idx, action, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact offload failed (keeping inline result): %s", exc)
        return result


def read_artifact_view(
    path: str,
    *,
    fields: Optional[list[str]] = None,
    limit: int = 20,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """Load a subset of an artifact. Lists are truncated to ``limit``."""
    data = load_artifact(path, root=root)
    selected: Any = data
    if fields and isinstance(data, dict):
        selected = {key: data.get(key) for key in fields}
    selected = _limit_value(selected, limit)
    return {"path": path, "data": selected}


def _limit_value(value: Any, limit: int) -> Any:
    if limit < 0:
        limit = 0
    if isinstance(value, list):
        return value[:limit]
    if isinstance(value, dict):
        trimmed: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, list):
                trimmed[key] = item[:limit]
            elif isinstance(item, str) and len(item) > 2000:
                trimmed[key] = item[:2000]
            else:
                trimmed[key] = item
        return trimmed
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000]
    return value
