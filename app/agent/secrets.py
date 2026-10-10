"""Secret scanning for traces (optimization plan 10.3 / P2-c).

Secrets such as OPENAI_API_KEY must live only in Settings and never be written
into trace / run / checkpoint JSON. This helper finds any JSON file that leaks a
given secret string so it can be asserted against in tests and audits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def scan_for_secrets(dirs: Iterable[Path], secrets: Iterable[str]) -> list[str]:
    """Return paths of JSON files (recursive) that contain any secret string."""
    secret_list = [s for s in secrets if s]
    if not secret_list:
        return []
    hits: list[str] = []
    for directory in dirs:
        root = Path(directory)
        if not root.exists():
            continue
        for path in root.glob("**/*.json"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if any(s in text for s in secret_list):
                hits.append(str(path))
    return hits