"""End-to-end Agent evals (optimization plan P1-b / section 3)."""

from __future__ import annotations

from app.agent.evals import EvalCase, run_evals


def test_run_evals_aggregates_goal_completion(tmp_path, monkeypatch):
    # Isolate trace writes to a temp dir so evals never pollute data/agent/runs.
    monkeypatch.setattr("app.agent.tracing.RUNS_DIR", tmp_path)

    cases = [
        EvalCase(goal="Analyze sample notes and create two posts", expected_keywords=["平价好物"]),
        EvalCase(goal="Create xhs content for college students", expected_keywords=["学生"]),
    ]
    summary = run_evals(cases)
    assert summary.goal_completion_rate == 1.0
    assert len(summary.cases) == 2
    assert all(c.completed for c in summary.cases)
    assert all(c.artifacts >= 1 for c in summary.cases)
    assert summary.to_dict()["goal_completion_rate"] == 1.0
    assert "goal_completion_rate" in summary.to_dict()