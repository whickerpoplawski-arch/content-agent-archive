#!/usr/bin/env python3
"""Run the M0 bare-topic baseline and write a scorecard.

    python scripts/compare_m0.py --stage m0 \\
        --eval data/evals/tool_calling_v0.jsonl \\
        --backend mock --model mock-topic \\
        --out experiments/baselines/m0_bare_llm/mock-topic/2026-10-22

This round accepts ``--backend mock`` only. The scorecard says the run checks
the pipeline and is not a formal G3 result. Gates that this script does not
execute are written as ``not_run``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.tool_eval.baseline import eval_digest, run_m0  # noqa: E402
from app.agent.tool_eval.case import load_cases  # noqa: E402
from app.agent.tool_eval.report import write_scorecard  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="M0 bare-LLM baseline (mock pipeline)")
    parser.add_argument("--stage", required=True, choices=["m0"])
    parser.add_argument("--eval", required=True, help="Path to the frozen eval JSONL")
    parser.add_argument("--backend", required=True, help="mock (live is rejected in this round)")
    parser.add_argument("--model", required=True, help="model_id recorded on the scorecard")
    parser.add_argument("--out", required=True, help="Directory for cases.jsonl and scorecard.md")
    args = parser.parse_args()
    if args.backend != "mock":
        print("refusing non-mock backend; this round only validates the M0 pipeline", file=sys.stderr)
        return 2
    eval_path = Path(args.eval)
    if not eval_path.is_absolute():
        eval_path = PROJECT_ROOT / eval_path
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    try:
        cases = load_cases(eval_path)
        digest = eval_digest(str(eval_path))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        rows, meta = asyncio.run(
            run_m0(
                cases,
                model_id=args.model,
                backend_name=args.backend,
                eval_path=str(eval_path.relative_to(PROJECT_ROOT)) if eval_path.is_relative_to(PROJECT_ROOT) else str(eval_path),
                eval_sha256=digest,
            )
        )
    except (AssertionError, ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 2
    summary = write_scorecard(out_dir, rows, meta)
    print(
        f"wrote {out_dir} cases={summary['n']} "
        f"parse={summary['parse_success']}/{summary['n']} "
        f"main_mean={summary['main_mean']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
