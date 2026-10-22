"""Thin theme → angles entry for the tool-calling demo.

Deterministic orchestration over a filtered registry (``registry.call``), not the
full ``XHSAgent`` loop. The mock planner always schedules ``generate_contents``,
which this slice must not enable. The call path is still the real tool path:
Pydantic params, the registered handler, and a persisted step trace.

Contract: ``docs/contracts/tool_calling_io_v0.md``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from app.agent.mock_factories import build_mock_registry
from app.agent.tools import DEMO_ALLOWLIST, ToolRegistry, build_default_registry

DEFAULT_AUDIENCE = "大学生女生"
DEFAULT_COUNT = 4
TRACE_DIR = Path("data") / "agent" / "topic_helper"

# v0 keyword gate only. A fuller policy table is out of scope for this slice.
_POLICY_TERMS = (
    "确诊",
    "处方",
    "包治",
    "治愈率",
    "临床疗效",
    "代开药",
    "诊疗方案",
    "保证治愈",
)

# Over-broad themes with no audience become clarify, not a topic-tool call.
_VAGUE_THEMES = {
    "爆款",
    "热门",
    "随便",
    "都行",
    "选题",
    "内容",
    "笔记",
    "分享",
    "小红书",
    "涨粉",
    "流量",
}

_UNSUPPORTED_CLAIM = re.compile(r"爆款率|涨粉\d|点赞过万|收藏率|互动数据")
_SAFE_WHY = "围绕主题给出可执行切入口，未引用互动数据"


def build_demo_registry(
    mode: str = "mock",
    allow_tools: Optional[list[str]] = None,
) -> ToolRegistry:
    """Mock or live registry containing only the demo allowlist.

    ``allow_tools`` may narrow the allowlist. Names outside ``DEMO_ALLOWLIST``
    are dropped, so crawl / publish handlers are never registered.
    """
    if mode == "mock":
        base: ToolRegistry = build_mock_registry()
    elif mode == "live":
        base = build_default_registry()
    else:
        raise ValueError(f"unsupported mode: {mode}")
    names = _effective_allowlist(allow_tools)
    if not names:
        return ToolRegistry()
    return base.filtered(names)


async def run_topic_helper(
    theme: str,
    *,
    audience: Optional[str] = None,
    count: int = DEFAULT_COUNT,
    allow_tools: Optional[list[str]] = None,
    mode: str = "mock",
    trace_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Run the demo path and return ``{status, angles, trace, meta}``.

    Empty, policy, and vague themes return before any ``registry.call``.
    """
    if mode not in ("mock", "live"):
        raise ValueError(f"unsupported mode: {mode}")

    run_id = uuid.uuid4().hex[:12]
    dest = Path(trace_dir) if trace_dir is not None else TRACE_DIR
    trace_path = dest / f"{run_id}.json"
    explicit_audience = bool(audience and audience.strip())
    audience_value = audience.strip() if explicit_audience else DEFAULT_AUDIENCE
    cleaned = (theme or "").strip()
    meta = {"mode": mode, "model": _model_name(mode), "trace_path": str(trace_path), "run_id": run_id}

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _persist(trace_path, payload)
        return payload

    if not cleaned:
        return finish(_terminal(
            status="refuse",
            reason="empty_theme",
            message="主题为空，无法选题",
            meta=meta,
        ))

    if _hits_policy(cleaned) or (explicit_audience and _hits_policy(audience_value)):
        return finish(_terminal(
            status="refuse",
            reason="policy",
            message="主题涉及诊疗承诺或违禁内容，已拒绝",
            meta=meta,
        ))

    if _is_vague(cleaned) and not explicit_audience:
        return finish({
            "status": "clarify",
            "reason": "vague_theme",
            "angles": [],
            "questions": [
                "这个主题想服务哪类人？例如大学生、通勤党、宿舍党。",
                "更具体的场景是什么？例如平价穿搭、护肤避雷、宿舍收纳。",
            ],
            "message": "主题过泛，需要补充人群或场景后再选题",
            "trace": {"steps": [], "forbidden_hits": []},
            "meta": meta,
        })

    allowed = _effective_allowlist(allow_tools)
    if "generate_topics" not in allowed:
        return finish(_terminal(
            status="degraded",
            message="generate_topics 不在本次允许的工具里，拒绝编造选题",
            meta=meta,
        ))

    wanted = _clamp_count(count)
    registry = build_demo_registry(mode, allowed)
    steps: list[dict[str, Any]] = []

    knowledge: Optional[dict[str, Any]] = None
    if "knowledge_search" in allowed:
        knowledge, _ok = await _try_call(
            registry,
            "knowledge_search",
            {"query": cleaned, "top_k": 5},
            steps,
        )

    kb_count = 0
    hits: list[dict[str, Any]] = []
    if isinstance(knowledge, dict):
        kb_count = int(knowledge.get("count") or 0)
        raw_hits = knowledge.get("hits") or []
        if isinstance(raw_hits, list):
            hits = [h for h in raw_hits if isinstance(h, dict)]

    analysis: Optional[dict[str, Any]] = None
    # Template B: empty KB, and the caller left sample-note tools enabled.
    if kb_count <= 0 and "load_notes" in allowed and "analyze_notes" in allowed:
        notes, notes_ok = await _try_call(registry, "load_notes", {}, steps)
        items = notes if notes_ok and isinstance(notes, list) else []
        if items:
            analyzed, analyzed_ok = await _try_call(
                registry, "analyze_notes", {"items": items}, steps
            )
            if analyzed_ok and isinstance(analyzed, dict):
                analysis = analyzed

    summary = cleaned
    extra_keywords: list[str] = []
    top_keywords: list[str] = []
    top_tags: list[str] = []
    title_patterns: list[str] = []
    insight_points: list[str] = []
    if analysis:
        analysis_summary = str(analysis.get("summary") or "").strip()
        if analysis_summary:
            summary = f"{cleaned}。{analysis_summary}"
        extra_keywords = [str(k) for k in (analysis.get("top_keywords") or [])]
        top_keywords = extra_keywords[:8]
        top_tags = [str(t) for t in (analysis.get("top_tags") or [])][:8]
        title_patterns = [str(p) for p in (analysis.get("title_patterns") or [])][:8]
        insight_points = [str(p) for p in (analysis.get("insight_points") or [])][:6]

    hints = _knowledge_hints(hits)
    topic_params = {
        "summary": summary,
        "audience": audience_value,
        "count": wanted,
        "focus_keywords": _focus_keywords(cleaned, extra_keywords),
        "top_keywords": top_keywords,
        "top_tags": top_tags,
        "title_patterns": title_patterns,
        "insight_points": insight_points,
        "knowledge_hints": hints,
    }
    topics_result, topics_ok = await _try_call(
        registry, "generate_topics", topic_params, steps
    )
    trace = {"steps": steps, "forbidden_hits": []}
    grounded = _grounded_from(steps)
    has_evidence = kb_count > 0 or analysis is not None

    if not topics_ok or not isinstance(topics_result, dict):
        return finish({
            "status": "degraded",
            "angles": [],
            "message": "generate_topics 失败，拒绝编造选题",
            "trace": trace,
            "meta": meta,
        })

    raw_topics = topics_result.get("topics") or []
    if not isinstance(raw_topics, list):
        raw_topics = []
    angles = _to_angles(
        raw_topics,
        audience=audience_value,
        grounded_from=grounded,
        has_evidence=has_evidence,
    )[:wanted]

    if len(angles) < 3:
        return finish({
            "status": "degraded",
            "angles": angles,
            "message": "选题不足 3 条，拒绝凑数",
            "trace": trace,
            "meta": meta,
        })

    return finish({
        "status": "ok",
        "angles": angles,
        "trace": trace,
        "meta": meta,
    })


def _terminal(*, status: str, message: str, meta: dict[str, Any], reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "angles": []}
    if reason:
        payload["reason"] = reason
    payload["message"] = message
    payload["trace"] = {"steps": [], "forbidden_hits": []}
    payload["meta"] = meta
    return payload


def _effective_allowlist(allow_tools: Optional[list[str]]) -> list[str]:
    if not allow_tools:
        return list(DEMO_ALLOWLIST)
    selected: list[str] = []
    allowed = set(DEMO_ALLOWLIST)
    for name in allow_tools:
        if name in allowed and name not in selected:
            selected.append(name)
    return selected


def _clamp_count(count: Any) -> int:
    try:
        value = int(count)
    except (TypeError, ValueError):
        value = DEFAULT_COUNT
    return min(5, max(3, value))


def _hits_policy(text: str) -> bool:
    return any(term in text for term in _POLICY_TERMS)


def _is_vague(theme: str) -> bool:
    compact = re.sub(r"[\s，,。！!？?、]+", "", theme)
    return compact in _VAGUE_THEMES


def _model_name(mode: str) -> str:
    if mode == "mock":
        return "mock"
    from app.core.config import settings

    return settings.openai_model or "live"


def _focus_keywords(theme: str, extra: Optional[list[str]] = None) -> list[str]:
    keys: list[str] = []

    def add(value: str) -> None:
        text = value.strip()
        if text and text not in keys:
            keys.append(text)

    add(theme)
    for part in re.split(r"[\s,，、/]+", theme):
        add(part)
    for item in extra or []:
        add(str(item))
    return keys[:8]


def _knowledge_hints(hits: list[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    for hit in hits[:5]:
        title = str(hit.get("title") or "").strip()
        text = str(hit.get("text") or "").strip()[:120]
        if not title and not text:
            continue
        doc_id = str(hit.get("doc_id") or "")
        source = str(hit.get("source") or "")
        hints.append(f"{doc_id}|{source}|{title} {text}".strip())
    return hints


def _params_for_trace(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Keep the contract's ``params`` field, but digest bulky note payloads."""
    if name == "analyze_notes":
        items = params.get("items") or []
        return {"n_items": len(items) if isinstance(items, list) else 0}
    if name == "load_notes":
        items = params.get("items")
        if isinstance(items, list):
            return {"n_items": len(items)}
        return {}
    if name == "knowledge_search":
        recorded = {"query": params.get("query"), "top_k": params.get("top_k", 5)}
        if params.get("source"):
            recorded["source"] = params["source"]
        return recorded
    if name == "generate_topics":
        summary = str(params.get("summary") or "")
        return {
            "summary": summary[:200],
            "audience": params.get("audience"),
            "count": params.get("count"),
            "focus_keywords": list(params.get("focus_keywords") or []),
            "n_knowledge_hints": len(params.get("knowledge_hints") or []),
        }
    return {key: value for key, value in params.items() if not isinstance(value, (list, dict))}


def _result_digest(name: str, result: Any) -> dict[str, Any]:
    if name == "knowledge_search" and isinstance(result, dict):
        hits = result.get("hits") or []
        titles = [str(h.get("title") or "") for h in hits if isinstance(h, dict)]
        return {"count": int(result.get("count") or 0), "titles": titles[:8]}
    if name == "load_notes" and isinstance(result, list):
        return {"n_notes": len(result)}
    if name == "analyze_notes" and isinstance(result, dict):
        keywords = [str(k) for k in (result.get("top_keywords") or [])]
        return {"n_keywords": len(keywords), "top_keywords": keywords[:8]}
    if name == "generate_topics" and isinstance(result, dict):
        topics = result.get("topics") or []
        return {"n_topics": len(topics) if isinstance(topics, list) else 0}
    return {"type": type(result).__name__}


async def _invoke(
    registry: ToolRegistry,
    name: str,
    params: dict[str, Any],
    steps: list[dict[str, Any]],
) -> Any:
    recorded = _params_for_trace(name, params)
    try:
        result = await registry.call(name, params)
    except Exception as exc:
        steps.append({
            "i": len(steps),
            "tool": name,
            "params": recorded,
            "ok": False,
            "result_digest": {"error": type(exc).__name__, "message": str(exc)[:180]},
        })
        raise
    steps.append({
        "i": len(steps),
        "tool": name,
        "params": recorded,
        "ok": True,
        "result_digest": _result_digest(name, result),
    })
    return result


async def _try_call(
    registry: ToolRegistry,
    name: str,
    params: dict[str, Any],
    steps: list[dict[str, Any]],
) -> tuple[Any, bool]:
    try:
        return await _invoke(registry, name, params, steps), True
    except Exception:
        return None, False


def _grounded_from(steps: list[dict[str, Any]]) -> list[str]:
    """``tool#n`` is the nth call of that tool (0-based), matching the contract example."""
    ordinal: dict[str, int] = {}
    ids: list[str] = []
    for step in steps:
        tool = str(step.get("tool") or "")
        n = ordinal.get(tool, 0)
        ordinal[tool] = n + 1
        if step.get("ok"):
            ids.append(f"{tool}#{n}")
    return ids


def _angle_from_title(title: str) -> str:
    """Use the left side of ``角度｜标题`` when present; otherwise a placeholder."""
    for sep in ("｜", "|"):
        if sep in title:
            left = title.split(sep, 1)[0].strip()
            if left:
                return left
    return f"场景切入：{title}"


def _safe_why(reason: str, *, has_evidence: bool) -> str:
    text = reason.strip()
    if not text:
        return _SAFE_WHY
    if not has_evidence and _UNSUPPORTED_CLAIM.search(text):
        return _SAFE_WHY
    return text


def _topic_fields(topic: Any) -> tuple[str, str]:
    if isinstance(topic, dict):
        return str(topic.get("title") or "").strip(), str(topic.get("reason") or "").strip()
    return str(getattr(topic, "title", "") or "").strip(), str(getattr(topic, "reason", "") or "").strip()


def _to_angles(
    topics: list[Any],
    *,
    audience: str,
    grounded_from: list[str],
    has_evidence: bool,
) -> list[dict[str, Any]]:
    # Prefer a generate_topics id; fall back to whatever succeeded.
    cite = [item for item in grounded_from if item.startswith("generate_topics#")]
    if not cite:
        cite = list(grounded_from[:1])
    else:
        extra = [item for item in grounded_from if item.startswith("knowledge_search#") or item.startswith("analyze_notes#")]
        cite = extra + cite
    angles: list[dict[str, Any]] = []
    for topic in topics:
        title, reason = _topic_fields(topic)
        if not title:
            continue
        angles.append({
            "angle": _angle_from_title(title),
            "title": title,
            "why": _safe_why(reason, has_evidence=has_evidence),
            "audience": audience,
            "grounded_from": list(cite),
        })
    return angles


def _persist(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
