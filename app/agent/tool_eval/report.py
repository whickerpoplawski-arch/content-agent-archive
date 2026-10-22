"""M0 scorecard (markdown + json). Gate rows that were not run stay ``not_run``."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.agent.tool_eval.rubric import in_main_mean

_BUCKET_ORDER = (
    "must_retrieve",
    "no_retrieve_ok",
    "tool_empty",
    "refuse",
    "clarify",
)


def _mean(scores: list[int]) -> float | None:
    if not scores:
        return None
    return sum(scores) / len(scores)


def _fmt_mean(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _fmt_meta(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def summarize(rows: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_bucket[str(row.get("bucket") or "")].append(row)

    buckets: dict[str, Any] = {}
    main_scores: list[int] = []
    for name in _BUCKET_ORDER:
        group = by_bucket.get(name, [])
        scores = [int((row.get("rubric") or {}).get("score") or 0) for row in group]
        entry = {
            "n": len(group),
            "mean": _mean(scores),
            "in_main_mean": in_main_mean(name),
        }
        if name in {"refuse", "clarify"}:
            passed = sum(1 for row in group if row.get("compliant") is True)
            entry["compliant"] = passed
        buckets[name] = entry
        if in_main_mean(name):
            main_scores.extend(scores)

    parsed = sum(1 for row in rows if row.get("parsed"))
    fake_hits = sum(int((row.get("rubric") or {}).get("fake_metric_hits") or 0) for row in rows)
    fake_cases = sum(1 for row in rows if int((row.get("rubric") or {}).get("fake_metric_hits") or 0) > 0)
    zero_tool = sum(
        1
        for row in rows
        if (row.get("trace") or {}).get("steps") == [] and int(row.get("tool_calls_total") or 0) == 0
    )
    n = len(rows)
    summary = {
        "stage": "m0",
        "backend": meta.get("backend"),
        "formal_g3": False,
        "note": "backend=mock。本报告只验证 M0 流水线，不作正式 G3 结论。",
        "model_id": meta.get("model_id"),
        "temperature": meta.get("temperature"),
        "max_tokens": meta.get("max_tokens"),
        "count": meta.get("count"),
        "prompt_sha256": meta.get("prompt_sha256"),
        "eval_path": meta.get("eval_path"),
        "eval_sha256": meta.get("eval_sha256"),
        "n": n,
        "parse_success": parsed,
        "parse_rate": (parsed / n) if n else None,
        "main_n": len(main_scores),
        "main_mean": _mean(main_scores),
        "buckets": buckets,
        "fake_metric_hits": fake_hits,
        "fake_metric_cases": fake_cases,
        "zero_tool_cases": zero_tool,
        "refuse_compliant": {
            "pass": buckets["refuse"]["compliant"],
            "n": buckets["refuse"]["n"],
        },
        "clarify_compliant": {
            "pass": buckets["clarify"]["compliant"],
            "n": buckets["clarify"]["n"],
        },
        "gates": {
            "G1": "not_run",
            "G2": "not_run",
            "G3": "not_run",
            "G4": "not_run",
            "G5": "not_run",
        },
    }
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    buckets = summary["buckets"]
    lines = [
        "# M0 分数卡",
        "",
        summary["note"],
        "",
        f"- backend: `{summary['backend']}`",
        f"- model_id: `{summary['model_id']}`",
        f"- temperature: `{_fmt_meta(summary['temperature'])}`",
        f"- max_tokens: `{_fmt_meta(summary['max_tokens'])}`",
        f"- count: `{_fmt_meta(summary['count'])}`",
        f"- prompt_sha256: `{summary['prompt_sha256']}`",
        f"- eval: `{summary['eval_path']}`",
        f"- eval_sha256: `{summary['eval_sha256']}`",
        f"- n: {summary['n']}",
        (
            f"- 解析成功率: {summary['parse_success']}/{summary['n']} "
            f"({summary['parse_rate']:.3f})"
            if summary["parse_rate"] is not None
            else "- 解析成功率: n/a"
        ),
        (
            f"- 主均分（must_retrieve + no_retrieve_ok + tool_empty，不含 refuse/clarify）: "
            f"{_fmt_mean(summary['main_mean'])} （n={summary['main_n']}）"
        ),
        (
            f"- FAKE_METRIC_RE 命中: {summary['fake_metric_hits']} 处 "
            f"/ {summary['fake_metric_cases']} 条 case"
        ),
        f"- 零工具: {summary['zero_tool_cases']}/{summary['n']}（trace.steps 为空且 tool_calls_total=0）",
        (
            f"- refuse 守规矩: {summary['refuse_compliant']['pass']}/{summary['refuse_compliant']['n']}"
            "（单独计数，不并入主均分）"
        ),
        (
            f"- clarify 守规矩: {summary['clarify_compliant']['pass']}/{summary['clarify_compliant']['n']}"
            "（单独计数，不并入主均分）"
        ),
        "",
        "## 分 bucket 平均 rubric（0–5）",
        "",
        "| bucket | n | 均分 | 计入主均分 |",
        "|---|---:|---:|---|",
    ]
    for name in _BUCKET_ORDER:
        entry = buckets[name]
        extra = ""
        if name in {"refuse", "clarify"}:
            extra = f"；守规矩 {entry['compliant']}/{entry['n']}"
        lines.append(
            f"| {name} | {entry['n']} | {_fmt_mean(entry['mean'])} | "
            f"{'yes' if entry['in_main_mean'] else 'no'}{extra} |"
        )
    lines.extend(
        [
            "",
            "## Gates",
            "",
            "- G1 调用正确: not_run",
            "- G2 结果融入: not_run",
            "- G3 相对基线: not_run（backend=mock，本轮只跑 M0，未做 Agent 对比，不作正式 G3 结论）",
            "- G4 失败可控: not_run",
            "- G5 可复现: not_run",
            "",
        ]
    )
    return "\n".join(lines)


def write_scorecard(out_dir: Path, rows: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = out_dir / "cases.jsonl"
    payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    cases_path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
    summary = summarize(rows, meta)
    (out_dir / "scorecard.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "scorecard.md").write_text(render_markdown(summary), encoding="utf-8")
    return summary
