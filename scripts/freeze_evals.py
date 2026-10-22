#!/usr/bin/env python3
"""Write or check the sha256 sidecar for a tool-calling eval JSONL file.

    python scripts/freeze_evals.py --file data/evals/tool_calling_v0.jsonl
    python scripts/freeze_evals.py --file data/evals/tool_calling_v0.jsonl --check

``--check`` exits 2 when the digest does not match. This script does not score
cases and does not mark the dataset card FROZEN.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.tool_eval.case import file_sha256, load_cases, sha_sidecar_path, verify_sha256  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze or check a tool-calling eval JSONL sha256")
    parser.add_argument("--file", required=True, help="Path to the eval JSONL")
    parser.add_argument("--check", action="store_true", help="Verify the sidecar instead of writing it")
    args = parser.parse_args()
    eval_path = Path(args.file)
    if not eval_path.is_absolute():
        eval_path = PROJECT_ROOT / eval_path
    sidecar = sha_sidecar_path(eval_path)
    try:
        cases = load_cases(eval_path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.check:
        try:
            digest = verify_sha256(eval_path, sidecar)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"ok {digest} cases={len(cases)}")
        return 0
    digest = file_sha256(eval_path)
    sidecar.write_text(f"{digest}  {eval_path.name}\n", encoding="utf-8")
    print(f"wrote {sidecar} {digest} cases={len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
