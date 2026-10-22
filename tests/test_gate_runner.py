"""Smoke test for the mock gate runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_sha_mismatch_exits_2(tmp_path):
    eval_path = tmp_path / "tool_calling_v0.jsonl"
    eval_path.write_text('{"id":"tc_001"}\n', encoding="utf-8")
    sidecar = tmp_path / "tool_calling_v0.sha256"
    sidecar.write_text("0" * 64 + "  tool_calling_v0.jsonl\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/eval_tool_calling.py",
            "--mode",
            "mock",
            "--eval",
            str(eval_path),
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--report",
            str(tmp_path / "gate"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


def test_mock_gate_writes_report(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/eval_tool_calling.py",
            "--mode",
            "mock",
            "--eval",
            "data/evals/tool_calling_v0.jsonl",
            "--repeat",
            "3",
            "--out",
            str(tmp_path / "runs"),
            "--report",
            str(tmp_path / "gate"),
            "--m0",
            "experiments/baselines/m0_bare_llm/mock-topic/2026-10-22/cases.jsonl",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    reports = list((tmp_path / "gate").glob("*_gate.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "G1 调用正确" in text
    assert "G2 结果融入" in text
    assert "G4 失败可控" in text
    assert "G5 可复现" in text
    assert "G3 相对基线" in text
    assert "不作正式 G3 结论" in text or "not_run" in text
    assert "不再回退 load_notes/analyze_notes" in text
    assert "status=degraded" in text
    runs = list((tmp_path / "runs").glob("run_*_mock.jsonl"))
    assert len(runs) == 1
    rows = [json.loads(line) for line in runs[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 30
    assert rows[0]["assertions"]["must_call"]["pass"] is True
    meta = json.loads(runs[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["sha256"]
    if meta["g1"] and meta["g2"] and meta["g5"]:
        follow = subprocess.run(
            [
                sys.executable,
                "scripts/compare_m0.py",
                "--stage",
                "g3",
                "--agent-run",
                str(runs[0]),
                "--m0",
                "experiments/baselines/m0_bare_llm/mock-topic/2026-10-22/cases.jsonl",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        assert follow.returncode == 0, follow.stderr
        assert "pipeline" in follow.stdout
        assert "不作正式" in follow.stdout
    else:
        follow = subprocess.run(
            [
                sys.executable,
                "scripts/compare_m0.py",
                "--stage",
                "g3",
                "--agent-run",
                str(runs[0]),
                "--m0",
                "experiments/baselines/m0_bare_llm/mock-topic/2026-10-22/cases.jsonl",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        assert follow.returncode == 2
        assert "not_run" in follow.stderr
