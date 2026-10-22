"""Aggregate D.3 assertions into G1–G5 and render the gate report.

G3 runs only after mock G1, G2, and G5 pass. A mock G3 number checks the
pipeline and is not a formal conclusion.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.agent.tool_eval.assertions import (
    GROUNDING_JACCARD,
    evaluate,
    output_blob,
    zero_retrieval,
)
from app.agent.tool_eval.case import ToolEvalCase
from app.agent.tool_eval.rubric import FAKE_METRIC_RE, in_main_mean, score_output

_G1_KEYS = (
    "must_call",
    "must_not_call",
    "order_soft",
    "max_tool_calls",
    "param_checks",
    "schema_valid",
    "status_match",
)
_CAUSE = {
    "must_not_call": "选错工具",
    "order_soft": "选错工具",
    "must_call": "漏调",
    "max_tool_calls": "漏调",
    "param_checks": "参数错",
    "schema_valid": "参数错",
    "grounding": "未融入",
    "extractive": "未融入",
    "honesty": "伪造数据",
    "status_match": "格式坏",
}


def case_passed(assertions: dict[str, dict[str, Any]], keys: tuple[str, ...]) -> bool:
    return all(bool((assertions.get(key) or {}).get("pass")) for key in keys)


def fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
    steps = tuple(str(step.get("tool") or "") for step in (row.get("trace") or {}).get("steps") or [])
    flags = tuple(
        (name, bool((result or {}).get("pass")))
        for name, result in sorted((row.get("assertions") or {}).items())
    )
    return (row.get("id"), row.get("status"), steps, flags)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def summarize_gates(rows: list[dict[str, Any]], *, repeat: int, repeats_match: bool, sha_ok: bool) -> dict[str, Any]:
    n = len(rows)
    must_call_ok = sum(1 for row in rows if row["assertions"]["must_call"]["pass"])
    blacklist_hits = sum(int(row["assertions"]["must_not_call"].get("hits") or 0) for row in rows)
    param_fail = sum(int(row["assertions"]["param_checks"].get("failed") or 0) for row in rows)
    param_total = sum(int(row["assertions"]["param_checks"].get("checked") or 0) for row in rows)
    schema_ok = 0
    schema_total = 0
    no_retrieve = [row for row in rows if row.get("bucket") == "no_retrieve_ok"]
    no_retrieve_clean = sum(
        1 for row in no_retrieve if zero_retrieval(row.get("trace") or {})
    )
    for row in rows:
        for step in (row.get("trace") or {}).get("steps") or []:
            schema_total += 1
            if step.get("ok") is True:
                schema_ok += 1

    schema_rate = (schema_ok / schema_total) if schema_total else 1.0
    g1_pass = (
        all(case_passed(row["assertions"], _G1_KEYS) for row in rows)
        and blacklist_hits == 0
        and param_fail == 0
        and schema_rate >= 0.95
        and no_retrieve_clean == len(no_retrieve)
    )

    grounded_angles = 0
    grounded_ok = 0
    spans_total = 0
    spans_ok = 0
    for row in rows:
        grounding = row["assertions"]["grounding"]
        grounded_angles += int(grounding.get("angles_total") or 0)
        grounded_ok += int(grounding.get("angles_ok") or 0)
        extractive = row["assertions"]["extractive"]
        spans_total += int(extractive.get("spans_total") or 0)
        spans_ok += int(extractive.get("spans_ok") or 0)
    g2_pass = all(row["assertions"]["grounding"]["pass"] and row["assertions"]["extractive"]["pass"] for row in rows)

    controlled = [row for row in rows if row.get("bucket") in {"tool_empty", "refuse", "clarify"}]
    fake_cases = 0
    for row in controlled:
        if FAKE_METRIC_RE.search(output_blob({"message": row.get("message"), "reason": row.get("reason"), "angles": row.get("angles"), "questions": row.get("questions")})):
            fake_cases += 1
    refuse_clarify = [row for row in rows if row.get("bucket") in {"refuse", "clarify"}]
    refuse_ok = sum(
        1
        for row in refuse_clarify
        if (row.get("trace") or {}).get("steps") == [] and row["assertions"]["status_match"]["pass"]
    )
    empty_rows = [row for row in rows if row.get("bucket") == "tool_empty"]
    empty_ok = sum(1 for row in empty_rows if row.get("status") == "degraded" or row["assertions"]["honesty"]["pass"])
    g4_pass = (
        fake_cases == 0
        and refuse_ok == len(refuse_clarify)
        and empty_ok == len(empty_rows)
        and all(row["assertions"]["honesty"]["pass"] for row in controlled)
    )

    if repeat < 3:
        g5_verdict = "not_run"
    elif not sha_ok or not repeats_match:
        g5_verdict = "FAIL"
    else:
        g5_verdict = "PASS"

    bucket_scores: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        bucket_scores[str(row.get("bucket") or "")].append(int((row.get("rubric") or {}).get("score") or 0))

    failures = failure_samples(rows)
    return {
        "n": n,
        "G1": {
            "verdict": "PASS" if g1_pass else "FAIL",
            "pass": g1_pass,
            "must_call": f"{must_call_ok}/{n}",
            "blacklist_hits": blacklist_hits,
            "param_fail_cases": param_fail,
            "param_cases": param_total,
            "schema_ok": schema_ok,
            "schema_total": schema_total,
            "schema_rate": schema_rate,
            "no_retrieve": f"{no_retrieve_clean}/{len(no_retrieve)}",
        },
        "G2": {
            "verdict": "PASS" if g2_pass else "FAIL",
            "pass": g2_pass,
            "angles": f"{grounded_ok}/{grounded_angles}" if grounded_angles else "0/0",
            "prior": f"{grounded_ok}/{grounded_angles}" if grounded_angles else "0/0",
            "spans": f"{spans_ok}/{spans_total}" if spans_total else "0/0",
            "jaccard": GROUNDING_JACCARD,
        },
        "G4": {
            "verdict": "PASS" if g4_pass else "FAIL",
            "pass": g4_pass,
            "refuse_clarify": f"{refuse_ok}/{len(refuse_clarify)}",
            "tool_empty": f"{sum(1 for row in empty_rows if row.get('status') == 'degraded')}/{len(empty_rows)}",
            "tool_empty_clause": f"{empty_ok}/{len(empty_rows)}",
            "fake_cases": fake_cases,
        },
        "G5": {
            "verdict": g5_verdict,
            "pass": g5_verdict == "PASS",
            "repeat": repeat,
            "repeats_match": repeats_match,
            "sha_ok": sha_ok,
        },
        "buckets": {
            name: {"n": len(bucket_scores.get(name, [])), "mean": _mean(bucket_scores.get(name, []))}
            for name in ("must_retrieve", "no_retrieve_ok", "tool_empty", "refuse", "clarify")
        },
        "failures": failures,
    }


def failure_samples(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for row in rows:
        for name, result in (row.get("assertions") or {}).items():
            if result.get("pass"):
                continue
            samples.append(
                {
                    "id": str(row.get("id")),
                    "gate": _gate_of(name),
                    "cause": _CAUSE.get(name, "格式坏"),
                    "detail": f"{name}: {result.get('detail')}",
                }
            )
            break
        if len(samples) >= limit:
            break
    return samples


def _gate_of(name: str) -> str:
    if name in {"grounding", "extractive"}:
        return "G2"
    if name == "honesty":
        return "G4"
    if name == "status_match":
        return "G4"
    return "G1"


def _same_number(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return left == right


def compare_g3(
    agent_rows: list[dict[str, Any]],
    m0_rows: list[dict[str, Any]],
    *,
    backend: str = "mock",
) -> dict[str, Any]:
    """Compare ok-class buckets. Live + matching decode is the formal G3."""
    m0_by_id = {str(row.get("id")): row for row in m0_rows}
    wins = ties = losses = 0
    deltas: list[float] = []
    skipped = 0
    count_mismatch = False
    for row in agent_rows:
        if not in_main_mean(str(row.get("bucket") or "")):
            continue
        other = m0_by_id.get(str(row.get("id")))
        if other is None:
            skipped += 1
            continue
        if row.get("count") != other.get("count"):
            count_mismatch = True
        agent_score = float((row.get("rubric") or {}).get("score") or 0)
        m0_score = float((other.get("rubric") or {}).get("score") or 0)
        deltas.append(agent_score - m0_score)
        if agent_score > m0_score:
            wins += 1
        elif agent_score == m0_score:
            ties += 1
        else:
            losses += 1
    n = wins + ties + losses
    win_rate = ((wins + 0.5 * ties) / n) if n else None
    delta = _mean(deltas)
    numeric_pass = bool(n and ((win_rate or 0) >= 0.60 or (delta or 0) >= 0.3))
    agent_model = str((agent_rows[0].get("model_id") if agent_rows else "") or "")
    m0_model = str((m0_rows[0].get("model_id") if m0_rows else "") or "")
    agent_temp = agent_rows[0].get("temperature") if agent_rows else None
    m0_temp = m0_rows[0].get("temperature") if m0_rows else None
    same_decode = agent_model == m0_model and _same_number(agent_temp, m0_temp) and not count_mismatch
    formal = backend == "live" and same_decode
    if backend == "live" and formal:
        verdict = "PASS" if numeric_pass else "FAIL"
        note = "backend=live。这是正式 G3（mock G3 只验证流水线）。"
    elif backend == "live":
        verdict = "FAIL"
        note = "backend=live。model_id、temperature 或 count 与 M0 不一致，正式 G3 作废。"
    else:
        verdict = "pipeline"
        note = "backend=mock，仅流水线，不作正式 G3 结论。"
    return {
        "verdict": verdict,
        "formal": formal,
        "backend": backend,
        "numeric_pass": numeric_pass and same_decode,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "n": n,
        "skipped": skipped,
        "win_rate": win_rate,
        "delta": delta,
        "agent_model": agent_model,
        "m0_model": m0_model,
        "same_decode": same_decode,
        "note": note,
    }


def latch_open(summary: dict[str, Any]) -> bool:
    return bool(summary["G1"]["pass"] and summary["G2"]["pass"] and summary["G5"]["pass"])


def render_gate_markdown(
    summary: dict[str, Any],
    *,
    sha: str,
    mode: str,
    g3: dict[str, Any] | None,
    decision: str,
) -> str:
    g1 = summary["G1"]
    g2 = summary["G2"]
    g4 = summary["G4"]
    g5 = summary["G5"]
    fail_pct = 100.0 * (1.0 - g1["schema_rate"])
    if g3 is None:
        g3_line = "G3 相对基线         not_run"
    else:
        delta = g3["delta"]
        n = g3["n"] or 1
        win_rate = g3["win_rate"] if g3["win_rate"] is not None else 0.0
        g3_line = (
            f"G3 相对基线         {g3['verdict']}"
            f"    win {g3['wins'] / n:.2f} | tie {g3['ties'] / n:.2f} | loss {g3['losses'] / n:.2f}"
            f" | win_rate {win_rate:.2f} | Δ {(delta or 0):+.2f}   ({g3['note']})"
        )
    bucket_bits = []
    for name, entry in summary["buckets"].items():
        mean = entry["mean"]
        shown = "n/a" if mean is None else f"{mean:.2f}"
        bucket_bits.append(f"{name} {shown}")
    lines = [
        f"===== content-agent · Tool-Calling Gate  (eval=v0 sha={sha[:12]} / agent={mode} / n={summary['n']}) =====",
        (
            f"G1 调用正确         {g1['verdict']}"
            f"    must_call {g1['must_call']} | 黑名单命中 {g1['blacklist_hits']}"
            f" | 参数失败 case {g1['param_fail_cases']}/{g1['param_cases']}"
            f" | schema 失败 {g1['schema_total'] - g1['schema_ok']}/{g1['schema_total']} ({fail_pct:.1f}%)"
            f" | no_retrieve 零检索 {g1['no_retrieve']}"
        ),
        (
            f"G2 结果融入         {g2['verdict']}"
            f"    每个 angle 有 step 引用 {g2['angles']} | 引用先于产出 {g2['prior']}"
            f" | 抽取引文 {g2['spans']} | Jaccard 兜底 {g2['jaccard']}"
        ),
        g3_line,
        (
            f"G4 失败可控         {g4['verdict']}"
            f"    refuse/clarify 守规矩 {g4['refuse_clarify']}"
            f" | tool_empty 降级 {g4['tool_empty']}"
            f" | 伪造指标命中 case {g4['fake_cases']}"
        ),
        (
            f"G5 可复现           {g5['verdict']}"
            f"    repeat={g5['repeat']} 逐 case 一致={g5['repeats_match']} | sha256 校验={'通过' if g5['sha_ok'] else '失败'}"
        ),
        "质量（分 bucket 平均 rubric，0–5）",
        "  " + " | ".join(bucket_bits),
        "失败样例 Top5",
    ]
    if summary["failures"]:
        for item in summary["failures"]:
            lines.append(f"  {item['id']} {item['gate']} {item['cause']}：{item['detail']}")
    else:
        lines.append("  无")
    lines.append(f"备注：{decision}")
    if g3 and g3.get("formal"):
        lines.append("备注：backend=live。这是正式 G3。此前 mock G3 只验证流水线。未跑项为 not_run。")
    else:
        lines.append("备注：mock 下 G3 只验证流水线；正式 G3 需 live 或微调模型。未跑项为 not_run。")
    lines.append("")
    return "\n".join(lines)


def render_diag(summary: dict[str, Any]) -> str:
    lines = ["# Gate 失败样例", ""]
    if not summary["failures"]:
        lines.append("无失败样例。")
        lines.append("")
        return "\n".join(lines)
    for item in summary["failures"]:
        lines.append(f"- {item['id']} · {item['gate']} · {item['cause']} · {item['detail']}")
    lines.append("")
    return "\n".join(lines)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")


def score_agent_row(case: ToolEvalCase, output: dict[str, Any]) -> dict[str, Any]:
    row = {
        "status": output.get("status"),
        "parsed": output.get("status") in {"ok", "degraded", "refuse", "clarify"},
        "message": output.get("message") or "",
        "angles": output.get("angles") or [],
        "raw": {},
        "trace": output.get("trace") or {"steps": []},
        "tool_calls_total": len((output.get("trace") or {}).get("steps") or []),
    }
    return score_output(case, row)
