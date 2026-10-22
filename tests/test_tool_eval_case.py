"""Schema checks for the tool-calling eval set. No Gate runner."""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.tool_eval.case import (
    DEFAULT_EVAL_PATH,
    DEFAULT_SNAPSHOT_PATH,
    ToolEvalCase,
    load_cases,
    load_snapshot,
    verify_sha256,
)
from app.services.topic_helper import _VAGUE_THEMES, _hits_policy

REPO = Path(__file__).resolve().parents[1]
QUOTAS = {
    "must_retrieve": 10,
    "no_retrieve_ok": 6,
    "tool_empty": 5,
    "refuse": 5,
    "clarify": 4,
}


def _norm(text: str) -> str:
    return "".join(text.split())


def test_cases_match_schema_quotas_and_ids():
    cases = load_cases()
    assert len(cases) == 30
    assert [case.id for case in cases] == [f"tc_{i:03d}" for i in range(1, 31)]
    assert Counter(case.bucket.value for case in cases) == QUOTAS
    assert {case.kb for case in cases} == {"kb_snapshot_v0"}
    for case in cases:
        trace = case.expected_trace
        assert trace.must_call or trace.expect_no_tools
        for token_check in trace.param_checks:
            if token_check.contains_any:
                assert all(token in case.theme for token in token_check.contains_any)


def test_ground_spans_exist_in_snapshot():
    cases = load_cases()
    docs = {doc["doc_id"]: doc for doc in load_snapshot()["docs"]}
    assert 20 <= len(docs) <= 40
    counts = {"must_retrieve": 0, "tool_empty": 0}
    for case in cases:
        if not case.must_ground_in:
            continue
        counts[case.bucket.value] = counts.get(case.bucket.value, 0) + 1
        for item in case.must_ground_in:
            doc = docs[item.doc_id]
            blob = _norm(doc["title"] + doc["text"])
            assert _norm(item.span) in blob
    assert counts["must_retrieve"] >= 3
    assert counts["tool_empty"] >= 3
    blob = "".join(doc["title"] + doc["text"] for doc in docs.values())
    for token in ("蜡片", "方言", "播客", "无人区", "露营", "古籍", "热泉", "微生物"):
        assert token not in blob


def test_refuse_and_clarify_match_keyword_table():
    for case in load_cases():
        if case.bucket.value == "clarify":
            assert case.audience is None
            assert case.theme in _VAGUE_THEMES
            assert case.expected_trace.expect_no_tools
        if case.bucket.value != "refuse":
            continue
        assert case.expected_trace.expect_no_tools
        assert case.expect_status.value == "refuse"
        if "empty_theme" in (case.notes or ""):
            assert not case.theme.strip()
        if "reason=policy" in (case.notes or ""):
            assert _hits_policy(case.theme)


def test_sha256_sidecar_matches():
    digest = verify_sha256()
    assert len(digest) == 64
    recorded = (REPO / "data" / "evals" / "DATASET_CARD.md").read_text(encoding="utf-8")
    assert digest in recorded
    assert "READY_TO_FREEZE" in recorded
    assert "永不进训练" in recorded


def test_freeze_check_cli():
    proc = subprocess.run(
        [sys.executable, "scripts/freeze_evals.py", "--file", "data/evals/tool_calling_v0.jsonl", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_sha_mismatch_and_bad_case(tmp_path):
    eval_copy = tmp_path / "tool_calling_v0.jsonl"
    eval_copy.write_bytes(DEFAULT_EVAL_PATH.read_bytes())
    sidecar = tmp_path / "tool_calling_v0.sha256"
    sidecar.write_text("0" * 64 + "  tool_calling_v0.jsonl\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        verify_sha256(eval_copy, sidecar)

    payload = load_cases()[0].model_dump(mode="json")
    payload.pop("kb")
    with pytest.raises(ValidationError):
        ToolEvalCase.model_validate(payload)

    payload = load_cases()[0].model_dump(mode="json")
    payload["allow_tools"] = ["publish_to_xhs"]
    with pytest.raises(ValidationError):
        ToolEvalCase.model_validate(payload)


def test_snapshot_rebuild_is_deterministic(tmp_path):
    out = tmp_path / "kb_snapshot_v0.json"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/build_kb_snapshot.py",
            "--raw",
            "data/raw/kb_seed_v0.jsonl",
            "--out",
            str(out),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_bytes() == DEFAULT_SNAPSHOT_PATH.read_bytes()
