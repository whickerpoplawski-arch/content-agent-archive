"""CLI wrapper for housekeeping.purge_old_runs (optimization plan 10.1).

Usage:
    python scripts/cleanup_runs.py                  # 30-day retention, keep summaries
    python scripts/cleanup_runs.py --retention 14
    python scripts/cleanup_runs.py --no-summary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.housekeeping import purge_old_runs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge stale agent run files.")
    parser.add_argument("--retention", type=int, default=30,
                        help="Delete files older than N days (default 30).")
    parser.add_argument("--no-summary", action="store_true",
                        help="Do not keep a summary for purged runs.")
    args = parser.parse_args()

    result = purge_old_runs(
        retention_days=args.retention,
        keep_summary=not args.no_summary,
    )
    print(f"removed={result['removed']} kept_summaries={result['kept_summaries']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())