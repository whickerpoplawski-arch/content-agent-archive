#!/usr/bin/env python3
"""Run the tool-calling gate on the frozen eval set.

    python scripts/eval_tool_calling.py --mode mock \\
        --eval data/evals/tool_calling_v0.jsonl \\
        --repeat 3 --out experiments/runs --report reports/gate

``--mode mock`` only. The frozen sha is checked first (exit 2 on mismatch).
G3 runs only after G1, G2, and G5 are green, and a mock G3 is marked as a
pipeline check rather than a formal result.
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
from app.core.config import settings  # noqa: E402
from app.services.topic_helper import run_topic_helper  # noqa: E402

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
    parser.add_argument("--mode", required=True, choices=["mock"])
    parser.add_argument("--eval", default="data/evals/tool_calling_v0.jsonl")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", default="experiments/runs")
    parser.add_argument("--report", default="reports/gate")
    parser.add_argument(
        "--m0",
        default="experiments/baselines/m0_bare_llm/mock-topic/2026-10-22/cases.jsonl",
        help="M0 cases.jsonl used only when G1/G2/G5 are green",
    )
    args = parser.parse_args()
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
    m0_path = Path(args.m0)
    if not m0_path.is_absolute():
        m0_path = PROJECT_ROOT / m0_path
    if latch_open(summary) and m0_path.is_file():
        g3 = compare_g3(runs[0], load_jsonl(m0_path))
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
    report_path = report_dir / f"{day}_{commit}_gate.md"
    diag_path = report_dir / f"{day}_diag.md"
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
        "latch_open": latch_open(summary),
        "g3": g3["verdict"] if g3 else "not_run",
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
