"""Positive, negative, and edge cases for D.3 assertions."""

from __future__ import annotations

from app.agent.tool_eval.assertions import (
    check_extractive,
    check_grounding,
    check_honesty,
    check_max_tool_calls,
    check_must_call,
    check_must_not_call,
    check_order_soft,
    check_param_checks,
    check_schema_valid,
    check_status_match,
    evaluate,
)
from app.agent.tool_eval.case import ToolEvalCase


def _case(**overrides) -> ToolEvalCase:
    payload = {
        "id": "tc_001",
        "bucket": "must_retrieve",
        "theme": "宿舍党低成本护肤",
        "audience": "大学生女生",
        "count": 4,
        "allow_tools": None,
        "kb": "kb_snapshot_v0",
        "expect_status": "ok",
        "expected_trace": {
            "must_call": ["knowledge_search", "generate_topics"],
            "must_not_call": ["crawl_notes"],
            "order_soft": ["knowledge_search", "generate_topics"],
            "max_tool_calls": 4,
            "param_checks": [
                {"tool": "knowledge_search", "field": "query", "contains_any": ["护肤"]},
                {"tool": "generate_topics", "field": "count", "equals": 4},
                {"tool": "generate_topics", "field": "focus_keywords", "intersects_theme": True},
            ],
            "grounding": "each_angle_grounded",
            "honesty": "no_fake_metrics",
            "expect_no_tools": False,
        },
        "expect_angles": {"min": 3, "max": 5, "require_fields": ["angle", "title", "why", "audience", "grounded_from"]},
        "must_ground_in": [{"doc_id": "kb_001", "span": "宿舍三件套百元内"}],
        "forbid_regex": [],
        "tags": [],
        "notes": "",
    }
    payload.update(overrides)
    return ToolEvalCase.model_validate(payload)


def _trace():
    return {
        "steps": [
            {
                "i": 0,
                "tool": "knowledge_search",
                "params": {"query": "宿舍党低成本护肤", "top_k": 5},
                "ok": True,
                "result_digest": {"count": 1, "snippets": ["宿舍三件套百元内"]},
            },
            {
                "i": 1,
                "tool": "generate_topics",
                "params": {"count": 4, "focus_keywords": ["宿舍党低成本护肤"]},
                "ok": True,
                "result_digest": {"n_topics": 3},
            },
        ]
    }


def _output():
    return {
        "status": "ok",
        "angles": [
            {
                "angle": "宿舍护肤",
                "title": "宿舍护肤清单",
                "why": "依据：宿舍三件套百元内",
                "audience": "大学生女生",
                "grounded_from": ["knowledge_search#0", "generate_topics#0"],
            }
        ],
    }


def test_positive_case_passes_every_assertion():
    results = evaluate(_case(), _trace(), _output())
    failed = {name: item["detail"] for name, item in results.items() if not item["pass"]}
    assert failed == {}


def test_must_call_negative_and_empty_trace():
    case = _case()
    assert check_must_call(case, {"steps": []})["pass"] is False
    refuse = _case(
        id="tc_018",
        bucket="refuse",
        theme="",
        expect_status="refuse",
        expected_trace={
            "must_call": [],
            "must_not_call": ["crawl_notes"],
            "order_soft": [],
            "max_tool_calls": 0,
            "param_checks": [],
            "grounding": "none_required",
            "honesty": "no_fake_metrics",
            "expect_no_tools": True,
        },
        expect_angles={"min": 0, "max": 0, "require_fields": []},
        must_ground_in=None,
    )
    assert check_must_call(refuse, {"steps": []})["pass"] is True
    assert check_must_call(refuse, _trace())["pass"] is False


def test_must_not_call_and_order():
    case = _case()
    bad = _trace()
    bad["steps"].append({"i": 2, "tool": "crawl_notes", "params": {}, "ok": True, "result_digest": {}})
    assert check_must_not_call(case, bad)["pass"] is False
    swapped = {
        "steps": [
            {"i": 0, "tool": "generate_topics", "params": {"count": 4, "focus_keywords": ["宿舍党低成本护肤"]}, "ok": True},
            {"i": 1, "tool": "knowledge_search", "params": {"query": "宿舍护肤"}, "ok": True},
        ]
    }
    assert check_order_soft(case, swapped)["pass"] is False
    assert check_order_soft(case, {"steps": []})["pass"] is True


def test_max_calls_and_schema_edges():
    case = _case()
    assert check_max_tool_calls(case, _trace())["pass"] is True
    crowded = {"steps": [{"i": i, "tool": "generate_topics", "ok": True} for i in range(5)]}
    assert check_max_tool_calls(case, crowded)["pass"] is False
    broken = _trace()
    broken["steps"][1]["ok"] = False
    assert check_schema_valid(broken)["pass"] is False
    assert check_schema_valid({"steps": []})["pass"] is True


def test_param_checks_negative():
    case = _case()
    trace = _trace()
    trace["steps"][1]["params"]["count"] = 2
    assert check_param_checks(case, trace)["pass"] is False
    trace["steps"] = [step for step in trace["steps"] if step["tool"] != "knowledge_search"]
    missed = check_param_checks(case, trace)
    assert missed["pass"] is False
    assert missed["failed"] >= 1


def test_grounding_edges():
    case = _case()
    assert check_grounding(case, _trace(), _output())["pass"] is True
    missing = _output()
    missing["angles"][0]["grounded_from"] = ["knowledge_search#9"]
    assert check_grounding(case, _trace(), missing)["pass"] is False
    assert check_grounding(case, _trace(), {"angles": []})["pass"] is False
    failed_step = _trace()
    failed_step["steps"][0]["ok"] = False
    only_failed = _output()
    only_failed["angles"][0]["grounded_from"] = ["knowledge_search#0"]
    assert check_grounding(case, failed_step, only_failed)["pass"] is False


def test_extractive_substring_fallback_and_skip():
    case = _case()
    assert check_extractive(case, _trace(), _output())["pass"] is True
    bare = _output()
    bare["angles"][0]["why"] = "没有原文"
    bare["angles"][0]["title"] = "无关标题"
    bare["angles"][0]["angle"] = "无关"
    assert check_extractive(case, {"steps": []}, bare)["pass"] is False
    # Digest and angle share the same phrase, so Jaccard clears 0.15 without the span.
    echoed = _output()
    echoed["angles"][0]["why"] = "宿舍党低成本护肤清单怎么做"
    echoed["angles"][0]["title"] = "宿舍党低成本护肤"
    echoed["angles"][0]["angle"] = "宿舍党低成本护肤"
    digest_trace = {
        "steps": [
            {
                "i": 0,
                "tool": "knowledge_search",
                "ok": True,
                "result_digest": {"snippets": ["宿舍党低成本护肤"]},
            }
        ]
    }
    assert check_extractive(case, digest_trace, echoed)["pass"] is True
    empty_case = _case(
        bucket="tool_empty",
        expect_status="degraded",
        expected_trace={
            "must_call": ["knowledge_search", "generate_topics"],
            "must_not_call": [],
            "order_soft": ["knowledge_search", "generate_topics"],
            "max_tool_calls": 4,
            "param_checks": [],
            "grounding": "none_required",
            "honesty": "no_fake_metrics",
            "expect_no_tools": False,
        },
        expect_angles={"min": 0, "max": 5, "require_fields": []},
    )
    assert check_extractive(empty_case, {"steps": []}, {"angles": []})["pass"] is True


def test_honesty_and_status():
    case = _case()
    dirty = _output()
    dirty["message"] = "这份笔记爆款率很高"
    assert check_honesty(case, dirty)["pass"] is False
    assert check_honesty(case, _output())["pass"] is True
    assert check_status_match(case, {"status": "degraded"})["pass"] is False
    empty_case = _case(
        bucket="tool_empty",
        expect_status="degraded",
        expected_trace={
            "must_call": ["knowledge_search"],
            "must_not_call": [],
            "order_soft": [],
            "param_checks": [],
            "grounding": "none_required",
            "honesty": "no_fake_metrics",
            "expect_no_tools": False,
        },
        expect_angles={"min": 0, "max": 5, "require_fields": []},
        must_ground_in=None,
    )
    assert check_status_match(empty_case, {"status": "ok"})["pass"] is True
    assert check_status_match(empty_case, {"status": "refuse"})["pass"] is False
