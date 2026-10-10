"""Append-only audit log for management actions (optimization plan 10.3 / P2-c).

Stop, resume, and housekeeping are consequential, potentially destructive
actions; record who/when/what so a long-running agent can be audited.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

AUDIT_LOG = Path("data") / "agent" / "audit.log"


def audit(action: str, run_id: str = "", detail: str = "") -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with AUDIT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{ts}\t{action}\t{run_id}\t{detail}\n")
    except OSError:
        pass