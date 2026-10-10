"""Systematic run evaluation (调研规划: 系统效果评估, 2026-09-15).

Reads a completed Agent run's console record (data/agent/console_runs/<id>.json,
authoritative for plan/tool_calls/status) + trace (data/agent/runs/<id>.json,
authoritative for outputs/usage/duration) and produces a verdict across seven
dimensions aligned with the agentic-RAG / context-aware-retrieval plan:

  1. 终态完整性   terminal state + per-step statuses (绝不虚报 completed)
  2. 安全门       publish/feishu/images 调用必须为 0 (本次实战约束)
  3. 智能体路由   knowledge_search 是否被规划/调用 (M2 Adaptive-RAG 生效证据)
  4. Read-Gate    注入的 K1..Kn 是否被 planner rationale 实际引用
  5. 质量门       evaluator-optimizer 评审通过率
  6. 产物相关性   主题域关键词覆盖 + audience 命中
  7. 成本/效率    LLM calls / tokens / cache / 时长 (+ 知识库回灌 delta)

Usage:
  python scripts/evaluate_run.py <run_id> [--json] [--kb-before 63]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RUNS_DIR = PROJECT_ROOT / "data" / "agent" / "runs"
CONSOLE_RUNS_DIR = PROJECT_ROOT / "data" / "agent" / "console_runs"
FORBIDDEN_TOOLS = ("publish_to_xhs", "sync_to_feishu", "generate_images")
DOMAIN_KEYWORDS = ("毕业去向", "就业", "考研", "保研", "出国", "留学", "实习", "秋招", "校招", "西电")
K_TAG_RE = re.compile(r"\bK\d+\b")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_run(run_id: str, kb_before: int | None = None) -> dict:
    trace_path = RUNS_DIR / f"{run_id}.json"
    record_path = CONSOLE_RUNS_DIR / f"{run_id}.json"
    if not record_path.exists():
        raise SystemExit(f"console record not found: {record_path}")
    record = _load(record_path)
    trace = _load(trace_path) if trace_path.exists() else {}
    result = trace.get("result", {}) or {}
    usage = record.get("usage") or trace.get("usage") or {}
    tool_calls = record.get("tool_calls", {}) or {}
    plan_steps = record.get("plan", []) or []

    report: dict[str, dict] = {}

    # 1. 终态完整性
    statuses = [s.get("status") for s in plan_steps]
    report["terminal"] = {
        "status": record.get("status"),
        "trace_status": trace.get("status"),
        "stopped_reason": trace.get("stopped_reason", "") or record.get("error", ""),
        "steps": {
            "total": len(plan_steps),
            "succeeded": statuses.count("succeeded"),
            "skipped": statuses.count("skipped"),
            "failed": statuses.count("failed"),
        },
        "duration_s": round((trace.get("duration_ms") or 0) / 1000, 1),
    }

    # 2. 安全门: 副作用工具必须零调用
    report["safety"] = {
        "violations": {t: int(tool_calls.get(t, 0)) for t in FORBIDDEN_TOOLS},
        "pass": all(int(tool_calls.get(t, 0)) == 0 for t in FORBIDDEN_TOOLS),
    }

    # 3. 智能体路由: knowledge_search 是否进入计划与执行
    plan_actions = [s.get("action") for s in plan_steps]
    ks_calls = int(tool_calls.get("knowledge_search", 0))
    knowledge_events = [
        e for e in (trace.get("events", []) or []) if e.get("kind") == "knowledge_context"
    ]
    report["agentic_routing"] = {
        "knowledge_search_in_plan": "knowledge_search" in plan_actions,
        "knowledge_search_calls": ks_calls,
        "knowledge_context_injections": len(knowledge_events),
        "knowledge_context_chars": [e.get("chars") for e in knowledge_events],
        "fallback_to_crawl": "crawl_notes" in plan_actions,
    }

    # 4. Read-Gate: K 标签是否被 plan rationale 实际引用 + hints 消费级注入
    plan_text = json.dumps(plan_steps, ensure_ascii=False)
    k_tags_cited = sorted(set(K_TAG_RE.findall(plan_text)))
    hints_injected = sum(
        1 for s in plan_steps if (s.get("params") or {}).get("knowledge_hints")
    )
    report["read_gate"] = {
        "k_tags_available": bool(knowledge_events),
        "k_tags_cited_in_plan": k_tags_cited,
        "knowledge_hints_steps": hints_injected,
        "auto_search_step": "knowledge_search" in plan_actions,
        "pass": (bool(k_tags_cited) or hints_injected > 0) if knowledge_events else None,
    }

    # 5. 质量门
    reports = result.get("quality_report", []) or []
    evaluated = sum(int(r.get("evaluated", 0)) for r in reports if isinstance(r, dict))
    optimized = sum(int(r.get("optimized", 0)) for r in reports if isinstance(r, dict))
    passed = sum(int(r.get("passed", 0)) for r in reports if isinstance(r, dict))
    report["quality_gate"] = {
        "evaluated": evaluated,
        "optimized": optimized,
        "passed": passed,
        "pass_rate": round(passed / evaluated, 3) if evaluated else None,
    }

    # 6. 产物相关性
    outputs = result.get("results", []) or []
    texts = [f"{o.get('title', '')} {o.get('body', '')}" for o in outputs if isinstance(o, dict)]
    blob = " ".join(texts)
    keyword_hits = {kw: (kw in blob) for kw in DOMAIN_KEYWORDS}
    covered = sum(1 for v in keyword_hits.values() if v)
    core = ("毕业去向", "就业", "考研", "保研", "实习")
    report["relevance"] = {
        "artifacts": len(outputs),
        "titles": [o.get("title", "") for o in outputs if isinstance(o, dict)],
        "keywords_hit": [kw for kw, hit in keyword_hits.items() if hit],
        "coverage_rate": round(covered / len(DOMAIN_KEYWORDS), 3),
        "core_domain_hits": sum(1 for kw in core if keyword_hits[kw]),
        "audience_in_outputs": ("西电" in blob) or ("电子科技大" in blob),
    }

    # 7. 成本/效率 + 知识库回灌
    report["cost"] = {
        "llm_calls": usage.get("calls", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_tokens", 0),
        "estimated": usage.get("estimated", False),
    }
    kb_report: dict = {}
    if kb_before is not None:
        try:
            from app.agent.knowledge import get_knowledge_store

            kb_now = get_knowledge_store().count()
            kb_report = {"before": kb_before, "after": kb_now, "ingested": kb_now - kb_before}
        except Exception as exc:  # noqa: BLE001
            kb_report = {"error": str(exc)}
    report["knowledge_reingest"] = kb_report

    # 总评
    no_failed = report["terminal"]["steps"]["failed"] == 0 and report["terminal"]["status"] == "completed"
    gates = {
        "terminal_ok": no_failed and len(plan_steps) > 0,
        "safety_ok": report["safety"]["pass"],
        "quality_ok": (report["quality_gate"]["pass_rate"] or 0) >= 0.5 if evaluated else False,
        "relevance_ok": report["relevance"]["artifacts"] >= 3
        and report["relevance"]["core_domain_hits"] >= 3,
    }
    report["verdict"] = {
        "gates": gates,
        "overall": "PASS" if all(gates.values()) else "FAIL",
        "notes": {
            "agentic_routing_used": report["agentic_routing"]["knowledge_search_in_plan"]
            or ks_calls > 0,
            "read_gate_evidence": bool(k_tags_cited),
        },
    }
    return report


def render(report: dict) -> str:
    lines = []

    def add(prefix: str, obj: dict) -> None:
        for k, v in obj.items():
            lines.append(f"{prefix}{k}: {v}")

    add("[终态] ", report["terminal"])
    add("[安全门] ", report["safety"])
    add("[智能体路由] ", report["agentic_routing"])
    add("[Read-Gate] ", report["read_gate"])
    add("[质量门] ", report["quality_gate"])
    add("[相关性] ", {k: v for k, v in report["relevance"].items() if k != "keywords_hit"})
    lines.append(f"[相关性] keywords_hit: {', '.join(report['relevance']['keywords_hit']) or '(none)'}")
    add("[成本] ", report["cost"])
    if report["knowledge_reingest"]:
        add("[知识回灌] ", report["knowledge_reingest"])
    add("[总评] ", report["verdict"]["gates"])
    lines.append(f"[总评] overall: {report['verdict']['overall']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a completed agent run")
    parser.add_argument("run_id")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--kb-before", type=int, default=None, dest="kb_before")
    args = parser.parse_args()
    report = evaluate_run(args.run_id, kb_before=args.kb_before)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report["verdict"]["overall"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
