#!/usr/bin/env python3
"""Run the tool-calling gate on the frozen eval set.

    python scripts/eval_tool_calling.py --mode mock \\
        --eval data/evals/tool_calling_v0.jsonl \\
        --repeat 3 --out experiments/runs --report reports/gate

``--mode mock`` checks the pipeline. ``--mode live`` calls the configured
model. Live G3 is formal only when the mock G1/G2/G5 latch is already green
(``--latch-meta``) and the M0 file used the same model, temperature, and count.
A missing API key exits 2 and prints the commands. It does not invent scores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.tool_eval.case import load_cases, load_snapshot, verify_sha256  # noqa: E402
from app.agent.tool_eval.gate import (  # noqa: E402
    compare_g3,
    fingerprint,
    latch_open,
    load_jsonl,
    render_diag,
    render_gate_markdown,
    score_agent_row,
    summarize_gates,
    write_jsonl,
)
from app.agent.tool_eval.assertions import evaluate  # noqa: E402
from app.agent.tool_eval.baseline import live_run_commands, require_live_credentials  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services.topic_helper import run_topic_helper  # noqa: E402

_MOCK_M0 = "experiments/baselines/m0_bare_llm/mock-topic/2026-10-22/cases.jsonl"

_DECISION = (
    "tool_empty 处理：knowledge_search 已调用且 kb_count=0 时，"
    "不再回退 load_notes/analyze_notes；直接 generate_topics 后 status=degraded。"
    "冻结 jsonl 未改字节。"
)


def _commit7() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return proc.stdout.strip() or "unknown"


async def _run_cases(cases, docs, mode: str, temperature: float, trace_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for case in cases:
        output = await run_topic_helper(
            case.theme,
            audience=case.audience,
            count=case.count if case.count is not None else 4,
            allow_tools=case.allow_tools,
            mode=mode,
            trace_dir=trace_dir,
            kb_docs=docs,
        )
        trace = output.get("trace") or {"steps": []}
        assertions = evaluate(case, trace, output)
        row = {
            "id": case.id,
            "bucket": case.bucket.value,
            "theme": case.theme,
            "audience": case.audience,
            "count": case.count if case.count is not None else 4,
            "allow_tools": case.allow_tools,
            "mode": mode,
            "model_id": (output.get("meta") or {}).get("model") or mode,
            "temperature": temperature,
            "max_tokens": None,
            "status": output.get("status"),
            "reason": output.get("reason") or "",
            "message": output.get("message") or "",
            "questions": output.get("questions") or [],
            "angles": output.get("angles") or [],
            "trace": {"steps": trace.get("steps") or [], "forbidden_hits": trace.get("forbidden_hits") or []},
            "assertions": assertions,
            "rubric": score_agent_row(case, output),
            "input": {
                "theme": case.theme,
                "audience": case.audience,
                "count": case.count,
                "allow_tools": case.allow_tools,
                "mode": mode,
                "kb": case.kb,
            },
        }
        rows.append(row)
    return rows


def _repeats_match(runs: list[list[dict]]) -> bool:
    if len(runs) <= 1:
        return True
    base = [fingerprint(row) for row in runs[0]]
    return all([fingerprint(row) for row in run] == base for run in runs[1:])


def main() -> int:
    parser = argparse.ArgumentParser(description="Tool-calling gate runner")
    parser.add_argument("--mode", required=True, choices=["mock", "live"])
    parser.add_argument("--eval", default="data/evals/tool_calling_v0.jsonl")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", default="experiments/runs")
    parser.add_argument("--report", default="reports/gate")
    parser.add_argument(
        "--m0",
        default=None,
        help="M0 cases.jsonl. Mock defaults to the checked-in mock-topic card. Live requires an explicit live card.",
    )
    parser.add_argument(
        "--latch-meta",
        default=None,
        help="Mock gate meta with g1/g2/g5 true. Required for a formal live G3 when this run's own G5 is not_run.",
    )
    args = parser.parse_args()
    if args.mode == "live":
        try:
            require_live_credentials()
        except RuntimeError:
            print(live_run_commands(), file=sys.stderr)
            return 2
    if args.repeat < 1:
        print("repeat must be >= 1", file=sys.stderr)
        return 2
    eval_path = Path(args.eval)
    if not eval_path.is_absolute():
        eval_path = PROJECT_ROOT / eval_path
    try:
        digest = verify_sha256(eval_path)
        cases = load_cases(eval_path)
        docs = load_snapshot()["docs"]
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2

    temperature = float(settings.openai_temperature)
    runs: list[list[dict]] = []
    with tempfile.TemporaryDirectory(prefix="gate-trace-") as tmp:
        trace_dir = Path(tmp)
        for _ in range(args.repeat):
            runs.append(asyncio.run(_run_cases(cases, docs, args.mode, temperature, trace_dir)))

    matched = _repeats_match(runs)
    summary = summarize_gates(runs[0], repeat=args.repeat, repeats_match=matched, sha_ok=True)
    g3 = None
    m0_arg = args.m0 if args.m0 is not None else (_MOCK_M0 if args.mode == "mock" else None)
    m0_path = None
    if m0_arg:
        m0_path = Path(m0_arg)
        if not m0_path.is_absolute():
            m0_path = PROJECT_ROOT / m0_path
    inherited = False
    if args.latch_meta:
        latch_path = Path(args.latch_meta)
        if not latch_path.is_absolute():
            latch_path = PROJECT_ROOT / latch_path
        try:
            inherited_meta = json.loads(latch_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(exc, file=sys.stderr)
            return 2
        inherited = bool(inherited_meta.get("g1") and inherited_meta.get("g2") and inherited_meta.get("g5"))
    if (latch_open(summary) or inherited) and m0_path is not None and m0_path.is_file():
        g3 = compare_g3(runs[0], load_jsonl(m0_path), backend=args.mode)
    commit = _commit7()
    day = date.today().isoformat()
    stem = f"run_{day}_{commit}_{args.mode}"
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    report_dir = Path(args.report)
    if not report_dir.is_absolute():
        report_dir = PROJECT_ROOT / report_dir
    run_path = out_dir / f"{stem}.jsonl"
    meta_path = out_dir / f"{stem}.meta.json"
    report_name = f"{day}_{commit}_live_gate.md" if args.mode == "live" else f"{day}_{commit}_gate.md"
    diag_name = f"{day}_live_diag.md" if args.mode == "live" else f"{day}_diag.md"
    report_path = report_dir / report_name
    diag_path = report_dir / diag_name
    write_jsonl(run_path, runs[0])
    meta = {
        "sha256": digest,
        "mode": args.mode,
        "repeat": args.repeat,
        "commit": commit,
        "date": day,
        "g1": summary["G1"]["pass"],
        "g2": summary["G2"]["pass"],
        "g4": summary["G4"]["pass"],
        "g5": summary["G5"]["pass"],
        "g5_verdict": summary["G5"]["verdict"],
        "latch_open": latch_open(summary) or inherited,
        "g3": g3["verdict"] if g3 else "not_run",
        "g3_formal": bool(g3 and g3.get("formal")),
        "decision": _DECISION,
        "run": str(run_path.relative_to(PROJECT_ROOT)) if run_path.is_relative_to(PROJECT_ROOT) else str(run_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_gate_markdown(summary, sha=digest, mode=args.mode, g3=g3, decision=_DECISION),
        encoding="utf-8",
    )
    diag_path.write_text(render_diag(summary), encoding="utf-8")
    print(
        f"wrote {report_path} G1={summary['G1']['verdict']} G2={summary['G2']['verdict']} "
        f"G3={meta['g3']} G4={summary['G4']['verdict']} G5={summary['G5']['verdict']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
