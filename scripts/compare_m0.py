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
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.tool_eval.baseline import eval_digest, run_m0  # noqa: E402
from app.agent.tool_eval.case import load_cases  # noqa: E402
from app.agent.tool_eval.gate import compare_g3, load_jsonl  # noqa: E402
from app.agent.tool_eval.report import write_scorecard  # noqa: E402


def _resolve(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def _run_g3(args: argparse.Namespace) -> int:
    if not args.agent_run or not args.m0:
        print("g3 requires --agent-run and --m0", file=sys.stderr)
        return 2
    agent_path = _resolve(args.agent_run)
    m0_path = _resolve(args.m0)
    meta_path = agent_path.with_suffix(".meta.json")
    if not meta_path.is_file():
        print("G3 not_run: missing gate meta; G1/G2/G5 latch is unknown", file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not (meta.get("g1") and meta.get("g2") and meta.get("g5")):
        print("G3 not_run: G1/G2/G5 latch is closed", file=sys.stderr)
        return 2
    try:
        result = compare_g3(load_jsonl(agent_path), load_jsonl(m0_path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        "G3 pipeline "
        f"win_rate={result['win_rate']} delta={result['delta']} "
        f"numeric_pass={result['numeric_pass']} formal={result['formal']} "
        f"({result['note']})"
    )
    if args.out:
        out = _resolve(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="M0 bare-LLM baseline and mock G3 pipeline check")
    parser.add_argument("--stage", required=True, choices=["m0", "g3"])
    parser.add_argument("--eval", help="Path to the frozen eval JSONL (m0)")
    parser.add_argument("--backend", help="mock (live is rejected)")
    parser.add_argument("--model", help="model_id recorded on the scorecard")
    parser.add_argument("--out", help="m0 directory, or g3 json path")
    parser.add_argument("--agent-run", help="Gate run jsonl (g3)")
    parser.add_argument("--m0", help="M0 cases.jsonl (g3)")
    args = parser.parse_args()
    if args.stage == "g3":
        return _run_g3(args)
    if not args.eval or not args.backend or not args.model or not args.out:
        print("m0 requires --eval --backend --model --out", file=sys.stderr)
        return 2
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
