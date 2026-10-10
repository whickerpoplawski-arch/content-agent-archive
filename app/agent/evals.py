"""End-to-end Agent evals (optimization plan P1-b / section 3).

Runs the full Agent over a set of goal cases (offline mock backend by default)
and aggregates goal-completion and quality metrics, so the project can prove its
Agent actually delivers rather than only that components unit-test green.

Metrics per case: goal completion, step count, tool/LLM calls, tokens, quality-
gate pass rate, artifact count, and keyword coverage of the final output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.agent.orchestrator import XHSAgent
from app.agent.mock_factories import build_mock_backend, build_mock_registry


@dataclass
class EvalCase:
    goal: str
    expected_keywords: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    goal: str
    completed: bool
    steps: int
    tool_calls: int
    llm_calls: int
    est_tokens: int
    quality_pass_rate: float
    artifacts: int
    keyword_coverage: float = 0.0


@dataclass
class EvalSummary:
    goal_completion_rate: float
    avg_steps: float
    avg_tool_calls: float
    avg_quality_pass_rate: float
    quality_gate_pass_rate: float
    cases: list[CaseResult]

    def to_dict(self) -> dict:
        return {
            "goal_completion_rate": self.goal_completion_rate,
            "avg_steps": self.avg_steps,
            "avg_tool_calls": self.avg_tool_calls,
            "avg_quality_pass_rate": self.avg_quality_pass_rate,
            "quality_gate_pass_rate": self.quality_gate_pass_rate,
            "cases": [c.__dict__ for c in self.cases],
        }


def _sum_dict_values(d: Optional[dict]) -> int:
    return int(sum(d.values()) if d else 0)


def _quality_pass_rate(outputs: dict) -> float:
    reports = outputs.get("quality_report") or []
    evaluated = passed = 0
    for report in reports:
        if isinstance(report, dict):
            evaluated += int(report.get("evaluated", 0))
            passed += int(report.get("passed", 0))
    return (passed / evaluated) if evaluated else 0.0


def _keyword_coverage(outputs: dict, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    texts: list[str] = []
    for item in outputs.get("results", []) or []:
        if isinstance(item, dict):
            texts.append(f"{item.get('title', '')} {item.get('body', '')}")
    blob = " ".join(texts)
    if not blob:
        return 0.0
    hits = sum(1 for kw in keywords if kw and kw in blob)
    return hits / len(keywords)


async def run_eval_case(agent: XHSAgent, case: EvalCase) -> CaseResult:
    from pathlib import Path
    import tempfile
    from app.agent.memory import AgentMemory, LongTermMemory

    # Isolated temp profile so evals never touch the real long-term memory.
    memory = AgentMemory(LongTermMemory(Path(tempfile.mkdtemp()) / "profile.json"))
    result = await agent.run(case.goal, run_id="eval", memory=memory)
    return CaseResult(
        goal=case.goal,
        completed=result.plan.is_complete,
        steps=len(result.plan.steps),
        tool_calls=_sum_dict_values(result.tool_calls),
        llm_calls=int(result.usage.get("calls", 0)),
        est_tokens=int(result.usage.get("est_tokens", 0)),
        quality_pass_rate=_quality_pass_rate(result.outputs),
        artifacts=len(result.outputs.get("results", [])),
        keyword_coverage=_keyword_coverage(result.outputs, case.expected_keywords),
    )


def run_evals(
    cases: list[EvalCase],
    *,
    backend=None,
    registry=None,
    agent_factory: Optional[Callable[[], XHSAgent]] = None,
) -> EvalSummary:
    """Run every case through one Agent and aggregate the metrics."""
    agent = agent_factory or (
        lambda: XHSAgent(
            backend=backend or build_mock_backend(),
            registry=registry or build_mock_registry(),
        )
    )
    a = agent()
    results = asyncio.run(_run_all(a, cases))
    n = len(results) or 1
    completed = sum(1 for r in results if r.completed)
    qg_passed = sum(1 for r in results if r.quality_pass_rate >= 1.0)
    return EvalSummary(
        goal_completion_rate=completed / n,
        avg_steps=sum(r.steps for r in results) / n,
        avg_tool_calls=sum(r.tool_calls for r in results) / n,
        avg_quality_pass_rate=sum(r.quality_pass_rate for r in results) / n,
        quality_gate_pass_rate=qg_passed / n,
        cases=results,
    )


async def _run_all(agent: XHSAgent, cases: list[EvalCase]) -> list[CaseResult]:
    return [await run_eval_case(agent, case) for case in cases]