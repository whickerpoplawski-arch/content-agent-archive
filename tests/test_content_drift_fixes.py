"""Fixes from the 2026-08-12 real-run (run 2f652c180bb9):

A. content_service._parse_contents normalizes the LLM's "drafts nested under
   title" shape ({"contents": [{"title": [DRAFT1, DRAFT2]}]}) that previously
   made ContentGenerateResponse validation fail and burn retries.
B. planner.append_steps / executor._step_text_params inherit the original
   step's audience/tone instead of silently falling back to hard-coded
   大学生女生/真实分享 on 补足 steps.
"""

from __future__ import annotations

import pytest

from app.models.schemas import ContentGenerateResponse


# ============================================================================
# A: content generation format normalization (real-run 2026-08-12)
# ============================================================================

def _parse(text: str) -> ContentGenerateResponse:
    from app.services.content_service import _parse_contents

    return _parse_contents(text)


def _draft(title: str) -> dict:
    return {
        "title": title,
        "body": "正文内容",
        "hashtags": ["#测试"],
        "cta": "快来聊聊",
        "image_suggestion": "拍一张照片",
        "content_type": "分享",
    }


def test_parse_contents_flattens_drafts_nested_under_title():
    """The real bug: LLM returns {"contents": [{"title": [D1, D2]}]}. The
    nested drafts must be flattened into two ContentItems."""
    d1, d2 = _draft("草稿一"), _draft("草稿二")
    resp = _parse('{"contents": [{"title": [' + str(d1).replace("'", '"')
                   + ", " + str(d2).replace("'", '"') + "]}]}")
    assert len(resp.contents) == 2
    assert resp.contents[0].title == "草稿一"
    assert resp.contents[1].title == "草稿二"
    # Nested drafts inherit the wrapper's other fields.
    assert resp.contents[0].hashtags == ["#测试"]
    assert resp.contents[1].content_type == "分享"


def test_parse_contents_normal_shape_unchanged():
    resp = _parse('{"contents": [' + str(_draft("正常标题")).replace("'", '"') + "]}"
                  .replace('"hashtags": ["#测试"]', '"hashtags": ["#测试"]'))
    assert len(resp.contents) == 1
    assert resp.contents[0].title == "正常标题"


def test_parse_contents_strips_markdown_fence():
    raw = '```json\n{"contents": [' + str(_draft("围栏标题")).replace("'", '"') + ']}\n```'
    resp = _parse(raw)
    assert resp.contents[0].title == "围栏标题"


def test_parse_contents_rejects_empty_object():
    with pytest.raises(ValueError):
        _parse("{}")


def test_parse_contents_rejects_null():
    with pytest.raises(ValueError):
        _parse("null")


# ============================================================================
# B: append_steps audience/tone inheritance (real-run 2026-08-12)
# ============================================================================

def test_append_steps_inherits_audience_and_tone():
    from app.agent.planner import Planner

    planner = Planner.__new__(Planner)
    steps = planner.append_steps(
        "为西电研究生生成5篇文案",
        3,
        audience="西电研究生",
        tone="真诚校园风",
    )
    assert len(steps) == 1
    assert steps[0].action == "generate_contents"
    assert steps[0].params["audience"] == "西电研究生"
    assert steps[0].params["tone"] == "真诚校园风"
    assert steps[0].params["count"] == 3


def test_append_steps_defaults_when_not_inherited():
    from app.agent.planner import Planner

    planner = Planner.__new__(Planner)
    steps = planner.append_steps("为西电研究生生成5篇文案", 2)
    assert steps[0].params["audience"] == "大学生女生"
    assert steps[0].params["tone"] == "真实分享"


def test_step_text_params_inherits_from_plan():
    from app.agent.executor import _step_text_params
    from app.agent.schemas import Plan, PlanStep

    plan = Plan(
        goal="g",
        steps=[
            PlanStep(action="crawl_notes", params={"keywords": ["k"]}),
            PlanStep(
                action="generate_contents",
                params={"audience": "西电研究生", "tone": "真诚校园风", "count": 3},
            ),
        ],
    )
    inherited = _step_text_params(plan, "generate_contents", ("audience", "tone"))
    assert inherited == {"audience": "西电研究生", "tone": "真诚校园风"}


def test_step_text_params_skips_missing_keys():
    from app.agent.executor import _step_text_params
    from app.agent.schemas import Plan, PlanStep

    plan = Plan(
        goal="g",
        steps=[PlanStep(action="generate_contents", params={"count": 3})],
    )
    assert _step_text_params(plan, "generate_contents", ("audience", "tone")) == {}
