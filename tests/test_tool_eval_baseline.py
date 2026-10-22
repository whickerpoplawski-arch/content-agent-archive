"""M0 bare path: no tools, mock backend can score a couple of frozen cases."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agent.tool_eval.baseline import (
    BarePathViolation,
    assert_bare_trace,
    guard_registry,
    prompt_sha256,
    run_m0,
    run_m0_case,
)
from app.agent.tool_eval.case import load_cases
from app.agent.tool_eval.report import write_scorecard
from app.agent.tool_eval.rubric import FAKE_METRIC_RE, score_output
from app.services.topic_service import load_prompt_template

REPO = Path(__file__).resolve().parents[1]


def test_assert_bare_trace_rejects_tools():
    assert_bare_trace({"steps": []}, 0)
    with pytest.raises(BarePathViolation):
        assert_bare_trace({"steps": [{"tool": "knowledge_search"}]}, 0)
    with pytest.raises(BarePathViolation):
        assert_bare_trace({"steps": []}, 1)


def test_guard_blocks_registry_call():
    from app.agent.tools import ToolRegistry

    async def _hit():
        with guard_registry() as counter:
            with pytest.raises(BarePathViolation):
                await ToolRegistry.call(None, "generate_topics", {})
            assert counter["n"] == 1

    asyncio.run(_hit())


def test_prompt_is_the_shared_template():
    text = load_prompt_template()
    assert "只输出 JSON" in text
    assert prompt_sha256() == __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()


def test_mock_runs_two_cases_with_zero_tools(tmp_path):
    wanted = {"tc_001", "tc_009"}
    cases = [case for case in load_cases() if case.id in wanted]
    assert len(cases) == 2

    async def _run():
        return await run_m0(
            cases,
            model_id="mock-topic",
            backend_name="mock",
            eval_path="data/evals/tool_calling_v0.jsonl",
            eval_sha256="abc",
        )

    rows, meta = asyncio.run(_run())
    assert meta["model_id"] == "mock-topic"
    assert meta["backend"] == "mock"
    assert meta["temperature"] == 0.7
    assert meta["max_tokens"] is None
    assert meta["count"] == 4
    assert meta["prompt_sha256"] == prompt_sha256()
    assert len(rows) == 2
    for row in rows:
        assert row["trace"]["steps"] == []
        assert row["tool_calls_total"] == 0
        assert row["parsed"] is True
        assert row["status"] == "ok"
        assert 3 <= len(row["angles"]) <= 5
        assert row["angles"][0]["grounded_from"] == []
        assert row["rubric"]["score"] == 5
        assert row["rubric"]["fake_metric_hits"] == 0
        assert row["in_main_mean"] is True
        assert "只输出 JSON" in load_prompt_template()
        blob = " ".join(angle["title"] + angle["why"] for angle in row["angles"])
        assert FAKE_METRIC_RE.search(blob) is None
        assert row["theme"].strip() in row["angles"][0]["title"]
    summary = write_scorecard(tmp_path, rows, meta)
    assert (tmp_path / "cases.jsonl").is_file()
    assert (tmp_path / "scorecard.md").is_file()
    assert (tmp_path / "scorecard.json").is_file()
    assert summary["gates"]["G3"] == "not_run"
    assert "不作正式 G3" in (tmp_path / "scorecard.md").read_text(encoding="utf-8")
    assert summary["main_n"] == 2
    assert summary["refuse_compliant"]["n"] == 0


def test_refuse_case_is_not_in_the_main_mean():
    case = next(item for item in load_cases() if item.bucket.value == "refuse")
    row = asyncio.run(
        run_m0_case(
            case,
            model_id="mock-topic",
            temperature=0.7,
            max_tokens=None,
            prompt_digest=prompt_sha256(),
        )
    )
    assert row["tool_calls_total"] == 0
    assert row["trace"]["steps"] == []
    assert row["in_main_mean"] is False
    assert row["compliant"] is False
    assert row["status"] == "ok"


def test_fake_metric_drops_the_honest_point():
    case = next(item for item in load_cases() if item.id == "tc_001")
    row = {
        "status": "ok",
        "parsed": True,
        "message": "",
        "angles": [
            {
                "angle": "宿舍护肤",
                "title": "宿舍党低成本护肤清单",
                "why": "这份清单的爆款率很高",
                "audience": "大学生女生",
                "grounded_from": [],
            }
        ],
        "raw": {"topics": []},
        "trace": {"steps": []},
        "tool_calls_total": 0,
    }
    scored = score_output(case, row)
    assert scored["honest"] == 0
    assert scored["fake_metric_hits"] >= 1


def _live_settings(monkeypatch, *, key: str, model: str = "deepseek-v4-flash"):
    from app.core.config import settings

    monkeypatch.setattr(settings, "openai_api_key", key)
    monkeypatch.setattr(settings, "openai_model", model)


def test_live_without_key_is_blocked(monkeypatch):
    _live_settings(monkeypatch, key="")

    async def _run():
        await run_m0([], model_id="deepseek-v4-flash", backend_name="live")

    with pytest.raises(RuntimeError, match="blocked: need OPENAI_API_KEY"):
        asyncio.run(_run())


def test_live_rejects_a_different_model(monkeypatch):
    _live_settings(monkeypatch, key="sk-test", model="deepseek-v4-flash")

    async def _run():
        await run_m0([], model_id="other-model", backend_name="live")

    with pytest.raises(ValueError, match="formal G3"):
        asyncio.run(_run())


def test_live_m0_calls_generate_topics_and_stays_bare(monkeypatch, tmp_path):
    from app.models.schemas import TopicGenerateResponse, TopicItem

    _live_settings(monkeypatch, key="sk-test-secret", model="deepseek-v4-flash")
    calls = {"n": 0}

    def fake_generate(request):
        calls["n"] += 1
        assert request.summary.strip()
        return TopicGenerateResponse(
            topics=[
                TopicItem(title="宿舍护肤清单：怎么做", reason="按步骤写清怎么做，控制在几分钟。"),
                TopicItem(title="宿舍护肤避雷步骤", reason="说明哪里容易做错以及怎么改。"),
                TopicItem(title="宿舍护肤公式与分钟", reason="按分钟拆开，写清每一步。"),
                TopicItem(title="宿舍护肤如何分步", reason="方便照着做。"),
            ]
        )

    monkeypatch.setattr("app.services.topic_service.generate_topics", fake_generate)
    cases = [case for case in load_cases() if case.id == "tc_001"]
    rows, meta = asyncio.run(
        run_m0(
            cases,
            model_id="deepseek-v4-flash",
            backend_name="live",
            eval_path="data/evals/tool_calling_v0.jsonl",
            eval_sha256="abc",
        )
    )
    assert calls["n"] == 1
    assert meta["backend"] == "live"
    assert meta["model_id"] == "deepseek-v4-flash"
    assert meta["temperature"] == 0.7
    assert meta["max_tokens"] is None
    row = rows[0]
    assert row["tool_calls_total"] == 0
    assert row["trace"]["steps"] == []
    assert row["parsed"] is True
    assert row["angles"][0]["grounded_from"] == []
    assert "sk-test-secret" not in json.dumps(row, ensure_ascii=False)
    card = write_scorecard(tmp_path, rows, meta)
    assert card["formal_g3"] is False
    assert "backend=live" in card["note"]


def test_live_error_redacts_the_api_key(monkeypatch):
    _live_settings(monkeypatch, key="sk-test-secret", model="deepseek-v4-flash")

    def boom(_request):
        raise RuntimeError("auth failed for sk-test-secret")

    monkeypatch.setattr("app.services.topic_service.generate_topics", boom)
    cases = [case for case in load_cases() if case.id == "tc_001"]
    rows, _meta = asyncio.run(
        run_m0(
            cases,
            model_id="deepseek-v4-flash",
            backend_name="live",
            eval_path="data/evals/tool_calling_v0.jsonl",
            eval_sha256="abc",
        )
    )
    blob = json.dumps(rows[0], ensure_ascii=False)
    assert "sk-test-secret" not in blob
    assert "[redacted]" in rows[0]["message"]
    assert rows[0]["parsed"] is False
    assert rows[0]["tool_calls_total"] == 0


def test_parse_topics_payload_strips_fences():
    from app.services.topic_service import parse_topics_payload

    parsed = parse_topics_payload('```json\n{"topics":[{"title":"a","reason":"b"}]}\n```')
    assert parsed.topics[0].title == "a"
    assert parsed.topics[0].reason == "b"
