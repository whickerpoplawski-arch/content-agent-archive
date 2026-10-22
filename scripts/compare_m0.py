#!/usr/bin/env python3
"""Run the M0 bare-topic baseline, or compare it to an agent gate run (G3).

Mock baseline (pipeline only, not a formal G3):

    python scripts/compare_m0.py --stage m0 \\
        --eval data/evals/tool_calling_v0.jsonl \\
        --backend mock --model mock-topic \\
        --out experiments/baselines/m0_bare_llm/mock-topic/2026-10-22

Live baseline (same ``settings.openai_model``, temperature, and count as the
agent path). A missing key prints ``blocked: need OPENAI_API_KEY (or Codex)``
and the copy-paste commands, then exits 2. It does not invent scores.

    python scripts/compare_m0.py --stage m0 \\
        --eval data/evals/tool_calling_v0.jsonl \\
        --backend live --model "$OPENAI_MODEL" \\
        --out experiments/baselines/m0_bare_llm/<model>/<date>

Formal G3 (ok-class buckets only) after G1/G2/G5 are already green. Live runs
are stochastic, so pass the mock latch file instead of requiring repeat=3:

    python scripts/compare_m0.py --stage g3 --backend live \\
        --agent-run experiments/runs/run_<date>_<commit7>_live.jsonl \\
        --m0 experiments/baselines/m0_bare_llm/<model>/<date>/cases.jsonl \\
        --latch-meta experiments/runs/run_2026-10-22_51e8496_mock.meta.json \\
        --out reports/gate/<date>_<commit7>_live_g3.json
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

from app.agent.tool_eval.baseline import (  # noqa: E402
    eval_digest,
    live_run_commands,
    require_live_credentials,
    run_m0,
)
from app.agent.tool_eval.case import load_cases  # noqa: E402
from app.agent.tool_eval.gate import compare_g3, load_jsonl  # noqa: E402
from app.agent.tool_eval.report import write_scorecard  # noqa: E402


def _resolve(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def _latch_meta(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"G3 not_run: missing gate meta {path}")
    meta = json.loads(path.read_text(encoding="utf-8"))
    if not (meta.get("g1") and meta.get("g2") and meta.get("g5")):
        raise ValueError("G3 not_run: G1/G2/G5 latch is closed")
    return meta


def _infer_backend(agent_rows: list[dict], explicit: str | None) -> str:
    if explicit:
        return explicit
    modes = {str(row.get("mode") or "") for row in agent_rows}
    if modes == {"live"}:
        return "live"
    return "mock"


def _run_g3(args: argparse.Namespace) -> int:
    if not args.agent_run or not args.m0:
        print("g3 requires --agent-run and --m0", file=sys.stderr)
        return 2
    if args.backend not in {None, "mock", "live"}:
        print("g3 --backend must be mock or live", file=sys.stderr)
        return 2
    agent_path = _resolve(args.agent_run)
    m0_path = _resolve(args.m0)
    latch_path = _resolve(args.latch_meta) if args.latch_meta else agent_path.with_suffix(".meta.json")
    try:
        _latch_meta(latch_path)
        agent_rows = load_jsonl(agent_path)
        backend = _infer_backend(agent_rows, args.backend)
        if backend not in {"mock", "live"}:
            print("g3 --backend must be mock or live", file=sys.stderr)
            return 2
        result = compare_g3(agent_rows, load_jsonl(m0_path), backend=backend)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        f"G3 {result['verdict']} "
        f"win_rate={result['win_rate']} delta={result['delta']} "
        f"numeric_pass={result['numeric_pass']} formal={result['formal']} "
        f"backend={result['backend']} "
        f"({result['note']})"
    )
    if args.out:
        out = _resolve(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="M0 bare-LLM baseline and G3 compare")
    parser.add_argument("--stage", required=True, choices=["m0", "g3"])
    parser.add_argument("--eval", help="Path to the frozen eval JSONL (m0)")
    parser.add_argument("--backend", help="mock or live")
    parser.add_argument("--model", help="model_id recorded on the scorecard; live defaults to settings.openai_model")
    parser.add_argument("--out", help="m0 directory, or g3 json path")
    parser.add_argument("--agent-run", help="Gate run jsonl (g3)")
    parser.add_argument("--m0", help="M0 cases.jsonl (g3)")
    parser.add_argument(
        "--latch-meta",
        help="Gate meta JSON with g1, g2, and g5 true. Defaults to the agent run's sibling .meta.json",
    )
    args = parser.parse_args()
    if args.stage == "g3":
        return _run_g3(args)
    if not args.eval or not args.backend or not args.out:
        print("m0 requires --eval --backend --out", file=sys.stderr)
        return 2
    if args.backend not in {"mock", "live"}:
        print("m0 --backend must be mock or live", file=sys.stderr)
        return 2
    model = args.model
    if args.backend == "live":
        try:
            recorded = require_live_credentials()
        except RuntimeError:
            print(live_run_commands(args.model), file=sys.stderr)
            return 2
        if not model:
            model = recorded
        elif model != recorded:
            print(
                f"model_id {model} != settings.openai_model {recorded}; "
                "refusing so a formal G3 is not void",
                file=sys.stderr,
            )
            return 2
    elif not model:
        print("m0 mock requires --model", file=sys.stderr)
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
                model_id=model,
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
