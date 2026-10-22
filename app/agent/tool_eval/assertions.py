"""D.3 trace assertions for one frozen case.

Each check returns ``{"pass": bool, "detail": str}``. Jaccard fallback for
extractive spans is fixed at 0.15.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.agent.tool_eval.case import ToolEvalCase
from app.agent.tool_eval.rubric import FAKE_METRIC_RE, jaccard, normalize_text, tokens

GROUNDING_JACCARD = 0.15
_DEFAULT_MAX_CALLS = 4
_RETRIEVAL_TOOLS = frozenset({"knowledge_search", "load_notes", "analyze_notes"})
_ANGLE_FIELDS = ("angle", "title", "why", "audience")


def _result(ok: bool, detail: str) -> dict[str, Any]:
    return {"pass": bool(ok), "detail": detail}


def _steps(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(trace, dict):
        return []
    raw = trace.get("steps") or []
    if not isinstance(raw, list):
        return []
    return [step for step in raw if isinstance(step, dict)]


def _tools(steps: list[dict[str, Any]]) -> list[str]:
    return [str(step.get("tool") or "") for step in steps]


def check_must_call(case: ToolEvalCase, trace: dict[str, Any]) -> dict[str, Any]:
    steps = _steps(trace)
    expected = case.expected_trace
    if expected.expect_no_tools:
        ok = steps == []
        return _result(ok, "expect_no_tools" if ok else f"steps={_tools(steps)}")
    missing = [name for name in expected.must_call if name not in set(_tools(steps))]
    return _result(not missing, "ok" if not missing else f"missing {missing}")


def check_must_not_call(case: ToolEvalCase, trace: dict[str, Any]) -> dict[str, Any]:
    called = set(_tools(_steps(trace)))
    banned = set(case.expected_trace.must_not_call)
    hit = sorted(called & banned)
    result = _result(not hit, "ok" if not hit else f"hit {hit}")
    result["hits"] = len(hit)
    return result


def check_order_soft(case: ToolEvalCase, trace: dict[str, Any]) -> dict[str, Any]:
    """Relative order of the order_soft tools that actually appeared."""
    wanted = list(case.expected_trace.order_soft)
    first: dict[str, int] = {}
    for index, step in enumerate(_steps(trace)):
        tool = str(step.get("tool") or "")
        if tool in wanted and tool not in first:
            first[tool] = index
    appeared = [name for name in wanted if name in first]
    for left, right in zip(appeared, appeared[1:]):
        if first[left] > first[right]:
            return _result(False, f"{right} before {left}")
    return _result(True, "ok" if appeared else "no listed tools appeared")


def check_max_tool_calls(case: ToolEvalCase, trace: dict[str, Any]) -> dict[str, Any]:
    limit = case.expected_trace.max_tool_calls
    if limit is None:
        limit = _DEFAULT_MAX_CALLS
    count = len(_steps(trace))
    return _result(count <= limit, f"{count}<={limit}" if count <= limit else f"{count}>{limit}")


def _field_of(step: dict[str, Any], field: str) -> Any:
    params = step.get("params")
    if isinstance(params, dict) and field in params:
        return params.get(field)
    return None


def _contains(haystack: Any, needles: list[str]) -> bool:
    parts: list[str] = []
    if isinstance(haystack, list):
        parts = [normalize_text(str(item)) for item in haystack]
    else:
        parts = [normalize_text(str(haystack or ""))]
    keys = [normalize_text(item) for item in needles if normalize_text(item)]
    return any(key and key in part for key in keys for part in parts)


def _equals(actual: Any, expected: Any) -> bool:
    if isinstance(expected, str):
        return normalize_text(str(actual if actual is not None else "")) == normalize_text(expected)
    if isinstance(expected, bool) or expected is None:
        return actual == expected
    if isinstance(expected, (int, float)):
        return actual == expected
    return actual == expected


def _intersects(actual: Any, expected_tokens: set[str]) -> bool:
    if isinstance(actual, list):
        blob = " ".join(str(item) for item in actual)
    else:
        blob = str(actual or "")
    return bool(tokens(blob) & expected_tokens)


def check_param_checks(case: ToolEvalCase, trace: dict[str, Any]) -> dict[str, Any]:
    steps = _steps(trace)
    failures: list[str] = []
    checked = 0
    for check in case.expected_trace.param_checks:
        matched = [step for step in steps if str(step.get("tool") or "") == check.tool]
        if not matched:
            failures.append(f"{check.tool}.{check.field} missing call")
            checked += 1
            continue
        for step in matched:
            checked += 1
            value = _field_of(step, check.field)
            if check.equals is not None and not _equals(value, check.equals):
                failures.append(f"{check.tool}.{check.field} equals {check.equals!r} got {value!r}")
            if check.contains_any and not _contains(value, check.contains_any):
                failures.append(f"{check.tool}.{check.field} contains_any {check.contains_any}")
            if check.forbid_any and _contains(value, check.forbid_any):
                failures.append(f"{check.tool}.{check.field} forbid_any {check.forbid_any}")
            if check.intersects is not None:
                wanted = {normalize_text(item) for item in check.intersects if normalize_text(item)}
                # Also accept 2-gram overlap so a full theme string can hit a short keyword.
                wanted |= tokens(" ".join(check.intersects))
                if not _intersects(value, wanted):
                    failures.append(f"{check.tool}.{check.field} intersects {check.intersects}")
            if check.intersects_theme:
                if not _intersects(value, tokens(case.theme)):
                    failures.append(f"{check.tool}.{check.field} intersects_theme")
    if not case.expected_trace.param_checks:
        result = _result(True, "no checks")
        result["checked"] = 0
        result["failed"] = 0
        return result
    result = _result(not failures, "ok" if not failures else "; ".join(failures))
    result["checked"] = checked
    result["failed"] = len(failures)
    return result


def check_schema_valid(trace: dict[str, Any]) -> dict[str, Any]:
    """``ok=false`` means the call never cleared Pydantic or the handler raised."""
    steps = _steps(trace)
    if not steps:
        return _result(True, "no calls")
    failed = [str(step.get("tool") or "") for step in steps if step.get("ok") is not True]
    return _result(not failed, "ok" if not failed else f"failed {failed}")


def _resolve_ref(steps: list[dict[str, Any]], ref: str) -> Optional[dict[str, Any]]:
    if "#" not in str(ref):
        return None
    tool, _, number = str(ref).rpartition("#")
    try:
        ordinal = int(number)
    except ValueError:
        return None
    seen = 0
    for index, step in enumerate(steps):
        if str(step.get("tool") or "") != tool:
            continue
        if seen == ordinal:
            if "i" not in step:
                step = dict(step)
                step["i"] = index
            return step
        seen += 1
    return None


def _ref_is_prior(step: dict[str, Any], produce_at: int) -> bool:
    if step.get("ok") is not True:
        return False
    try:
        index = int(step.get("i"))
    except (TypeError, ValueError):
        return False
    return index < produce_at


def check_grounding(case: ToolEvalCase, trace: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    mode = case.expected_trace.grounding
    if mode in {None, "none_required"}:
        return _result(True, "none_required" if mode == "none_required" else "not set")
    if mode != "each_angle_grounded":
        return _result(False, f"unknown grounding {mode}")
    steps = _steps(trace)
    produce_at = len(steps)
    angles = output.get("angles") if isinstance(output, dict) else None
    if not isinstance(angles, list) or not angles:
        return _result(False, "no angles")
    missing = 0
    for angle in angles:
        refs = angle.get("grounded_from") if isinstance(angle, dict) else None
        if not isinstance(refs, list) or not refs:
            missing += 1
            continue
        if not any(
            _ref_is_prior(resolved, produce_at)
            for ref in refs
            if (resolved := _resolve_ref(steps, str(ref)))
        ):
            missing += 1
    result = _result(missing == 0, "ok" if missing == 0 else f"{missing} angles lack a prior ok step")
    result["angles_total"] = len(angles)
    result["angles_ok"] = len(angles) - missing
    return result


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", normalize_text(text))


def _angle_text(angle: dict[str, Any]) -> str:
    return " ".join(str(angle.get(key) or "") for key in _ANGLE_FIELDS)


def _digest_text(steps: list[dict[str, Any]]) -> str:
    return json.dumps([step.get("result_digest") for step in steps], ensure_ascii=False)


def check_extractive(case: ToolEvalCase, trace: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    """Span substring, or result_digest Jaccard ≥ 0.15 against an angle."""
    if case.expected_trace.grounding == "none_required":
        return _result(True, "skipped: grounding none_required")
    spans = case.must_ground_in or []
    if not spans:
        return _result(True, "no spans")
    steps = _steps(trace)
    digest = _digest_text(steps)
    angles = output.get("angles") if isinstance(output, dict) else None
    if not isinstance(angles, list):
        angles = []
    missed: list[str] = []
    for item in spans:
        span = _compact(item.span)
        found = False
        for angle in angles:
            if not isinstance(angle, dict):
                continue
            blob = _compact(_angle_text(angle))
            if span and span in blob:
                found = True
                break
            if jaccard(digest, _angle_text(angle)) >= GROUNDING_JACCARD:
                found = True
                break
        if not found:
            missed.append(item.span)
    result = _result(not missed, "ok" if not missed else f"missed {missed}")
    result["spans_total"] = len(spans)
    result["spans_ok"] = len(spans) - len(missed)
    return result


def output_blob(output: dict[str, Any] | None) -> str:
    if not isinstance(output, dict):
        return ""
    parts = [str(output.get("message") or ""), str(output.get("reason") or "")]
    questions = output.get("questions") or []
    if isinstance(questions, list):
        parts.extend(str(item) for item in questions)
    angles = output.get("angles") or []
    if isinstance(angles, list):
        for angle in angles:
            if isinstance(angle, dict):
                parts.extend(str(angle.get(key) or "") for key in _ANGLE_FIELDS)
                grounded = angle.get("grounded_from") or []
                if isinstance(grounded, list):
                    parts.extend(str(item) for item in grounded)
            else:
                parts.append(str(angle))
    return "\n".join(parts)


def check_honesty(case: ToolEvalCase, output: dict[str, Any]) -> dict[str, Any]:
    text = output_blob(output)
    hits = FAKE_METRIC_RE.findall(text)
    extra: list[str] = []
    for pattern in case.forbid_regex or []:
        try:
            if re.search(pattern, text):
                extra.append(pattern)
        except re.error:
            extra.append(f"bad regex {pattern}")
    if case.expected_trace.honesty not in {None, "no_fake_metrics"}:
        return _result(False, f"unknown honesty {case.expected_trace.honesty}")
    ok = not hits and not extra
    detail = "ok" if ok else f"fake={hits} forbid={extra}"
    return _result(ok, detail)


def check_status_match(case: ToolEvalCase, output: dict[str, Any]) -> dict[str, Any]:
    status = str((output or {}).get("status") or "")
    if case.bucket.value == "tool_empty":
        ok = status in {"ok", "degraded"}
        return _result(ok, status if ok else f"{status} not in ok|degraded")
    expected = case.expect_status.value
    return _result(status == expected, "ok" if status == expected else f"{status}!={expected}")


def zero_retrieval(trace: dict[str, Any]) -> bool:
    return not any(tool in _RETRIEVAL_TOOLS for tool in _tools(_steps(trace)))


def evaluate(case: ToolEvalCase, actual_trace: dict[str, Any], actual_output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Run every D.3 assertion. Keys are stable for the gate report."""
    return {
        "must_call": check_must_call(case, actual_trace),
        "must_not_call": check_must_not_call(case, actual_trace),
        "order_soft": check_order_soft(case, actual_trace),
        "max_tool_calls": check_max_tool_calls(case, actual_trace),
        "param_checks": check_param_checks(case, actual_trace),
        "schema_valid": check_schema_valid(actual_trace),
        "grounding": check_grounding(case, actual_trace, actual_output),
        "extractive": check_extractive(case, actual_trace, actual_output),
        "honesty": check_honesty(case, actual_output),
        "status_match": check_status_match(case, actual_output),
    }
