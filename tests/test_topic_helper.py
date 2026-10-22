"""Offline tests for the theme → angles demo slice."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.tools import DEMO_ALLOWLIST, DEMO_DENYLIST
from app.services.topic_helper import build_demo_registry, run_topic_helper


def run(coro):
    return asyncio.run(coro)


def test_mock_success_returns_angles_and_trace(tmp_path):
    result = run(
        run_topic_helper(
            "大学生平价通勤穿搭",
            audience="应届女研究生",
            count=9,
            mode="mock",
            trace_dir=tmp_path,
        )
    )

    assert result["status"] == "ok"
    assert 3 <= len(result["angles"]) <= 5
    tools = [step["tool"] for step in result["trace"]["steps"]]
    assert tools == ["knowledge_search", "load_notes", "analyze_notes", "generate_topics"]
    assert result["trace"]["forbidden_hits"] == []
    assert set(tools).isdisjoint(DEMO_DENYLIST)

    topic_step = result["trace"]["steps"][-1]
    assert topic_step["params"]["count"] == 5
    assert any("大学生平价通勤穿搭" in kw for kw in topic_step["params"]["focus_keywords"])
    assert result["trace"]["steps"][0]["params"]["query"] == "大学生平价通勤穿搭"
    assert result["trace"]["steps"][0]["result_digest"]["count"] == 0

    for angle in result["angles"]:
        assert angle["angle"]
        assert angle["title"]
        assert angle["why"]
        assert angle["audience"] == "应届女研究生"
        assert "generate_topics#0" in angle["grounded_from"]

    assert result["angles"][0]["why"] == "贴合高收藏关键词与平价好物标签"
    assert result["angles"][0]["title"] == "大学生平价好物真实测评"
    assert result["angles"][0]["angle"] == "场景切入：大学生平价好物真实测评"

    trace_path = tmp_path / f"{result['meta']['run_id']}.json"
    assert trace_path.exists()
    saved = json.loads(trace_path.read_text(encoding="utf-8"))
    assert saved["trace"]["steps"]
    assert saved["status"] == "ok"


def test_empty_theme_refuses_without_tools(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("tool called")

    monkeypatch.setattr("app.agent.tools.ToolRegistry.call", boom)
    result = run(run_topic_helper("  \n", mode="mock", trace_dir=tmp_path))

    assert result["status"] == "refuse"
    assert result["reason"] == "empty_theme"
    assert result["angles"] == []
    assert result["trace"]["steps"] == []
    saved = json.loads((tmp_path / f"{result['meta']['run_id']}.json").read_text(encoding="utf-8"))
    assert saved["trace"]["steps"] == []


def test_vague_theme_clarifies_without_topic_tool(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("tool called")

    monkeypatch.setattr("app.agent.tools.ToolRegistry.call", boom)
    vague = run(run_topic_helper("爆款", mode="mock", trace_dir=tmp_path))
    assert vague["status"] == "clarify"
    assert vague["reason"] == "vague_theme"
    assert 1 <= len(vague["questions"]) <= 2
    assert vague["trace"]["steps"] == []

    # An explicit audience is enough to leave the clarify gate.
    monkeypatch.undo()
    specific = run(
        run_topic_helper("爆款", audience="大学生女生", mode="mock", trace_dir=tmp_path)
    )
    assert specific["status"] == "ok"
    assert any(step["tool"] == "generate_topics" for step in specific["trace"]["steps"])


def test_policy_theme_refuses_without_tools(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("tool called")

    monkeypatch.setattr("app.agent.tools.ToolRegistry.call", boom)
    result = run(run_topic_helper("在线诊疗方案，保证治愈", mode="mock", trace_dir=tmp_path))
    assert result["status"] == "refuse"
    assert result["reason"] == "policy"
    assert result["trace"]["steps"] == []


def test_demo_registry_blocks_denylist():
    for mode in ("mock", "live"):
        registry = build_demo_registry(mode)
        assert set(registry.names()) == set(DEMO_ALLOWLIST)
        for denied in DEMO_DENYLIST:
            assert denied not in registry.names()
            with pytest.raises(KeyError):
                run(registry.call(denied, {}))


def test_allow_tools_cannot_enable_blacklist(tmp_path):
    result = run(
        run_topic_helper(
            "宿舍护肤",
            allow_tools=["knowledge_search", "generate_topics", "crawl_notes", "publish_to_xhs"],
            mode="mock",
            trace_dir=tmp_path,
        )
    )
    tools = [step["tool"] for step in result["trace"]["steps"]]
    assert result["status"] == "ok"
    assert tools == ["knowledge_search", "generate_topics"]
    assert result["trace"]["forbidden_hits"] == []
    assert "crawl_notes" not in tools
    assert "publish_to_xhs" not in tools


def test_title_pipe_splits_angle():
    from app.services.topic_helper import _to_angles

    angles = _to_angles(
        [{"title": "痛点反转：面试前一天｜研究生面试穿搭", "reason": "可执行"}],
        audience="应届女研究生",
        grounded_from=["generate_topics#0"],
        has_evidence=True,
    )
    assert angles[0]["angle"] == "痛点反转：面试前一天"
    assert angles[0]["why"] == "可执行"
