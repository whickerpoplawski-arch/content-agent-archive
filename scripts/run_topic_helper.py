#!/usr/bin/env python3
"""Theme → angles demo (tool-calling slice).

Mock is the default and does not crawl, log in, or call a live model:

    python scripts/run_topic_helper.py --theme "大学生平价通勤穿搭" --mode mock

Prints the contract JSON (``angles``, ``trace``, ``status``) and writes the
same payload under ``data/agent/topic_helper/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.topic_helper import run_topic_helper  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Xiaohongshu topic helper (demo allowlist)")
    parser.add_argument("--theme", required=True, help="一句话主题；空字符串会 refuse")
    parser.add_argument("--audience", default=None, help="默认不传；过泛主题会先追问")
    parser.add_argument("--count", type=int, default=4, help="角度数，夹紧到 3–5")
    parser.add_argument("--mode", choices=("mock", "live"), default="mock")
    parser.add_argument(
        "--allow-tools",
        default="",
        help="逗号分隔，只能收窄演示白名单；黑名单名称会被丢弃",
    )
    parser.add_argument("--trace-dir", default=None, help="轨迹目录，默认 data/agent/topic_helper")
    args = parser.parse_args()
    allow = [part.strip() for part in args.allow_tools.split(",") if part.strip()] or None
    result = asyncio.run(
        run_topic_helper(
            args.theme,
            audience=args.audience,
            count=args.count,
            allow_tools=allow,
            mode=args.mode,
            trace_dir=args.trace_dir,
        )
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
