"""StepCheckpointer: durable, atomic persistence of an in-flight Agent run.

Lets a run interrupted by a crash or app restart resume from its last saved
step instead of being irrecoverably marked failed (optimization plan P0 / 12.2).
Uses the same tmp + os.replace atomic-write pattern as run_manager._persist.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

CHECKPOINT_DIR = Path("data") / "agent" / "checkpoints"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StepCheckpointer:
    def __init__(self, directory: Path = CHECKPOINT_DIR) -> None:
        self.directory = directory

    def _path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def save(
        self,
        run_id: str,
        plan,
        memory_steps: list[dict[str, Any]],
        usage: dict[str, Any],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "plan": plan.model_dump(mode="json"),
            "memory": {"steps": memory_steps},
            "usage": usage,
            "updated_at": _now(),
        }
        path = self._path(run_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def load(self, run_id: str) -> Optional[dict[str, Any]]:
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def clear(self, run_id: str) -> None:
        path = self._path(run_id)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass