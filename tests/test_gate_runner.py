"""Smoke test for the mock gate runner and the live G3 compare latch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _row(score: int, *, model: str = "deepseek-v4-flash", bucket: str = "must_retrieve", count: int = 4, mode: str = "live"):
    return {
        "id": "tc_001",
        "bucket": bucket,
        "count": count,
        "mode": mode,
        "model_id": model,
        "temperature": 0.7,
        "rubric": {"score": score},
    }


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


def test_compare_g3_live_same_decode_is_formal():
    from app.agent.tool_eval.gate import compare_g3

    result = compare_g3([_row(4)], [_row(3)], backend="live")
    assert result["formal"] is True
    assert result["backend"] == "live"
    assert result["verdict"] == "PASS"
    assert result["wins"] == 1
    assert result["delta"] == 1.0
    assert "backend=live" in result["note"]
    assert "正式 G3" in result["note"]


def test_compare_g3_mock_stays_pipeline():
    from app.agent.tool_eval.gate import compare_g3

    result = compare_g3([_row(4, mode="mock")], [_row(3, mode="mock")], backend="mock")
    assert result["formal"] is False
    assert result["verdict"] == "pipeline"
    assert "不作正式 G3" in result["note"]


def test_compare_g3_live_model_mismatch_voids_formal():
    from app.agent.tool_eval.gate import compare_g3

    result = compare_g3([_row(5, model="agent-model")], [_row(1, model="m0-model")], backend="live")
    assert result["formal"] is False
    assert result["verdict"] == "FAIL"
    assert result["same_decode"] is False
    assert "作废" in result["note"]


def test_compare_g3_skips_refuse_and_clarify():
    from app.agent.tool_eval.gate import compare_g3

    agent = [
        _row(1, bucket="refuse"),
        _row(1, bucket="clarify"),
    ]
    agent[1] = {**agent[1], "id": "tc_002"}
    m0 = [
        _row(5, bucket="refuse"),
        {**_row(5, bucket="clarify"), "id": "tc_002"},
    ]
    result = compare_g3(agent, m0, backend="live")
    assert result["n"] == 0
    assert result["wins"] == 0
    assert result["formal"] is True
    assert result["verdict"] == "FAIL"


def test_live_g3_cli_uses_latch_and_does_not_call_network(tmp_path):
    agent = tmp_path / "agent.jsonl"
    m0 = tmp_path / "m0.jsonl"
    latch = tmp_path / "latch.meta.json"
    out = tmp_path / "g3.json"
    agent.write_text(json.dumps(_row(4), ensure_ascii=False) + "\n", encoding="utf-8")
    m0.write_text(json.dumps(_row(3), ensure_ascii=False) + "\n", encoding="utf-8")
    latch.write_text(json.dumps({"g1": True, "g2": True, "g5": True}), encoding="utf-8")
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = ""
    env["CODEX_HOME"] = str(tmp_path / "nocodex")
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/compare_m0.py",
            "--stage",
            "g3",
            "--backend",
            "live",
            "--agent-run",
            str(agent),
            "--m0",
            str(m0),
            "--latch-meta",
            str(latch),
            "--out",
            str(out),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "formal=True" in proc.stdout
    assert "backend=live" in proc.stdout
    assert "正式 G3" in proc.stdout
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["formal"] is True
    assert saved["verdict"] == "PASS"


def test_live_m0_cli_without_key_prints_commands(tmp_path):
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = ""
    env["CODEX_HOME"] = str(tmp_path / "nocodex")
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/compare_m0.py",
            "--stage",
            "m0",
            "--eval",
            "data/evals/tool_calling_v0.jsonl",
            "--backend",
            "live",
            "--model",
            "deepseek-v4-flash",
            "--out",
            str(tmp_path / "m0"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 2
    assert "blocked: need OPENAI_API_KEY (or Codex)" in proc.stderr
    assert "--backend live" in proc.stderr
    assert "--latch-meta" in proc.stderr
    assert not (tmp_path / "m0").exists()


def test_live_eval_without_key_exits_before_scores(tmp_path):
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = ""
    env["CODEX_HOME"] = str(tmp_path / "nocodex")
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/eval_tool_calling.py",
            "--mode",
            "live",
            "--eval",
            "data/evals/tool_calling_v0.jsonl",
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
        env=env,
    )
    assert proc.returncode == 2
    assert "blocked: need OPENAI_API_KEY (or Codex)" in proc.stderr
    assert list((tmp_path / "gate").glob("*")) == []
    assert list((tmp_path / "runs").glob("*")) == []
