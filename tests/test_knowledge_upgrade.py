"""Tests for knowledge-store upgrade (调研规划 M0/M1/M2, 2026-09-15).

Covers: retrieval eval harness, contextual chunk prefix (Anthropic
Contextual Retrieval 的免 LLM 版), multi-variant RRF fusion (CompCQR 思想),
crawl-recency decay, digest/retrieval scale discrimination + injection
budget + K-tag numbering (Read-Gate), and the knowledge_search agentic tool.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.agent.knowledge import KnowledgeStore
from app.core.config import settings


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from app.agent.embedder import get_embedder

    try:
        get_embedder()
    except FileNotFoundError:
        pytest.skip("embedding model not present in data/models/")
    s = KnowledgeStore(path=tmp_path / "knowledge.db")
    monkeypatch.setattr("app.agent.knowledge._store", s)
    yield s
    monkeypatch.undo()


SEED = [
    ("宿舍收纳大法！小空间也不怕乱。床底收纳箱是神器。", "宿舍收纳", "crawl"),
    ("新手护肤避雷指南，别再交智商税。", "护肤避雷", "crawl"),
    ("西电研究生秋招经验：刷题重点在动态规划。", "秋招经验", "manual"),
]


def _seed(store):
    for text, title, source in SEED:
        store.add(text, title=title, source=source)


# ---------- M0: retrieval eval harness ----------


def test_retrieval_evals_full_hit_on_trivial_corpus(store):
    from app.agent.retrieval_evals import build_corpus_cases, evaluate_retrieval

    _seed(store)
    cases = build_corpus_cases(store)
    assert len(cases) == len(SEED)  # doc-level only: no goal metadata seeded
    summary = evaluate_retrieval(store, cases)
    assert summary.n_cases == len(SEED)
    assert summary.hit_at[5] == 1.0
    assert summary.failure_rate == 0.0
    assert summary.misses == []


def test_retrieval_evals_goal_level_cases(store):
    from app.agent.retrieval_evals import build_corpus_cases, evaluate_retrieval

    store.add(
        "宿舍收纳技巧分享。",
        title="收纳笔记",
        source="crawl",
        metadata={"goal": "为大学生女生生成宿舍收纳文案"},
    )
    cases = build_corpus_cases(store)
    goal_cases = [c for c in cases if c.style == "goal"]
    assert len(goal_cases) == 1 and goal_cases[0].goal == "为大学生女生生成宿舍收纳文案"
    summary = evaluate_retrieval(store, cases)
    assert summary.hit_at[5] == 1.0
    assert summary.by_style["goal"]["hit@5"] == 1.0


def test_rebuild_corpus_preserves_count(store):
    from app.agent.retrieval_evals import rebuild_corpus

    _seed(store)
    fresh = rebuild_corpus(store)
    try:
        assert fresh.count() == store.count()
        assert fresh.search("动态规划", top_k=1)[0]["title"] == "秋招经验"
    finally:
        fresh.close()


# ---------- M1: contextual chunk prefix ----------


def test_contextual_prefix_in_chunks_and_hits(store):
    _seed(store)
    hits = store.search("宿舍收纳大法", top_k=1)
    assert hits
    text = hits[0]["text"]
    assert text.startswith("[知识|crawl|宿舍收纳")
    assert "床底收纳箱" in text  # 原文仍完整保留在前缀之后


def test_prefix_enables_title_only_lexical_recall(store):
    """语境化的核心收益: 标题词不在正文里, 前缀把它带进 BM25 双索引。"""
    store.add("床底收纳箱、门后挂钩、分层置物架都是神器。", title="网红收纳好物榜单", source="crawl")
    hits = store.search("网红收纳好物榜单", top_k=1)
    assert hits and hits[0]["title"] == "网红收纳好物榜单"


def test_prefixed_add_still_idempotent(store):
    doc_id = "ctxidem12345"
    store.add("幂等语境化测试内容", title="幂等", doc_id=doc_id)
    store.add("幂等语境化测试内容", title="幂等", doc_id=doc_id)
    assert store.count() == 1


def test_long_doc_chunking_with_prefix_stays_bounded(store):
    long_text = "句子。" * 300  # 600 chars -> 多 chunk
    store.add(long_text, title="长文档", source="crawl")
    hits = store.search("长文档", top_k=5)
    assert len(hits) >= 2  # 前缀收紧窗口后仍切成多块
    assert all(h["text"].startswith("[知识|crawl|长文档") for h in hits)


# ---------- M1: multi-variant RRF fusion ----------


def test_search_multi_fuses_variants(store):
    _seed(store)
    # 变体1 只命中宿舍, 变体2 只命中护肤; 双变体融合应两篇都召回。
    hits = store.search_multi(["宿舍收纳大法", "护肤避雷指南"], top_k=4)
    titles = {h["title"] for h in hits}
    assert {"宿舍收纳", "护肤避雷"} <= titles


def test_search_multi_empty_variants(store):
    assert store.search_multi([]) == []
    assert store.search_multi(["  ", ""]) == []


def test_search_multi_beats_single_query_on_split_evidence(store):
    """一个 goal 跨两篇文档且单查询只命中一半时, 变体融合补齐另一半。"""
    store.add("暑期实习内推渠道盘点，大厂日常实习。", title="实习内推", source="crawl")
    store.add("考研时间线与复习规划全流程。", title="考研规划", source="crawl")
    variants = ["暑期实习 内推", "考研 时间线"]
    fused = {h["title"] for h in store.search_multi(variants, top_k=4)}
    assert {"实习内推", "考研规划"} <= fused


# ---------- M1: crawl-recency decay ----------


def test_recency_decay_ranks_fresh_crawl_first(store):
    """同文本两篇 crawl 笔记, 旧的那篇应因时效衰减排后。"""
    old_days = 200
    store.add("独特的时效性内容甲甲甲", title="甲", source="crawl", doc_id="decay_old_1")
    store.add("独特的时效性内容乙乙乙", title="乙", source="crawl", doc_id="decay_new_1")
    with store._lock:
        stale = time.time() - old_days * 86400
        store._conn.execute("UPDATE documents SET created_at=? WHERE id='decay_old_1'", (stale,))
        store._conn.commit()
    hits = store.search("独特的时效性内容", top_k=2)
    assert [h["title"] for h in hits] == ["乙", "甲"]


def test_manual_source_not_decayed(store):
    old_days = 400
    store.add("知识资产不随时间衰减的论据", title="资产", source="manual", doc_id="asset_old_1")
    with store._lock:
        stale = time.time() - old_days * 86400
        store._conn.execute("UPDATE documents SET created_at=? WHERE id='asset_old_1'", (stale,))
        store._conn.commit()
    assert store.search("知识资产不随时间衰减的论据", top_k=1)


# ---------- M1: injection v2 (digest / retrieval / budget / K-tags) ----------


def test_knowledge_context_digest_mode_small_store(store):
    from app.agent.orchestrator import _knowledge_context

    _seed(store)
    text = _knowledge_context("为大学生女生生成3篇宿舍收纳文案")
    assert text.startswith("\nKnowledge base digest")
    assert "[K1|" in text and "[K2|" in text
    assert "宿舍收纳" in text and "护肤避雷" in text


def test_knowledge_context_empty_store_returns_empty(store):
    from app.agent.orchestrator import _knowledge_context

    assert _knowledge_context("任意目标") == ""


def test_knowledge_context_retrieval_mode_large_store(store, monkeypatch):
    from app.agent.orchestrator import _knowledge_context

    monkeypatch.setattr(settings, "knowledge_digest_max_docs", 1)
    _seed(store)
    text = _knowledge_context("为大学生女生生成3篇宿舍收纳文案")
    assert text.startswith("\nRelevant knowledge")
    assert "[K1|crawl]" in text


def test_knowledge_context_budget_capped(store, monkeypatch):
    from app.agent.orchestrator import _knowledge_context

    monkeypatch.setattr(settings, "knowledge_context_budget_chars", 160)
    monkeypatch.setattr(settings, "knowledge_digest_max_docs", 30)
    _seed(store)
    text = _knowledge_context("任意目标")
    body = text.split("\n", 1)[1]
    assert 0 < len(body) <= 160 + 120  # 至少注入一条, 总量受预算约束


def test_query_variants_focus_and_dedup():
    from app.agent.orchestrator import _query_variants

    variants = _query_variants("围绕「西电实习」为西电学生生成文案")
    assert variants[0] == "围绕「西电实习」为西电学生生成文案"
    assert len(variants) <= 3
    assert "西电实习" in variants[1]
    assert _query_variants("随便聊聊") == ["随便聊聊"]
    assert _query_variants("") == [""]


# ---------- M2: knowledge_search agentic tool ----------


def test_default_registry_has_knowledge_search():
    from app.agent.tools import build_default_registry

    registry = build_default_registry()
    assert "knowledge_search" in registry.names()
    spec = registry.get("knowledge_search").spec()
    assert "source" in spec.params_schema_json
    assert "source" in registry.get("knowledge_search").description


def test_knowledge_search_tool_returns_hits(store):
    from app.agent.tools import KnowledgeSearchParams, _knowledge_search

    _seed(store)
    result = asyncio.run(
        _knowledge_search(KnowledgeSearchParams(query="动态规划刷题", top_k=2))
    )
    assert result["count"] >= 1
    hit = result["hits"][0]
    assert {"doc_id", "title", "source", "score", "text"} <= set(hit)


def test_knowledge_search_tool_source_filter(store):
    from app.agent.tools import KnowledgeSearchParams, _knowledge_search

    _seed(store)
    result = asyncio.run(
        _knowledge_search(KnowledgeSearchParams(query="秋招", source="manual", top_k=3))
    )
    assert result["count"] >= 1
    assert all(h["source"] == "manual" for h in result["hits"])


def test_knowledge_search_tool_empty_store_hint(store, monkeypatch):
    from app.agent.tools import KnowledgeSearchParams, _knowledge_search

    result = asyncio.run(_knowledge_search(KnowledgeSearchParams(query="任意")))
    assert result["count"] == 0
    assert "crawl_notes" in result["note"]


def test_knowledge_search_tool_error_is_graceful(monkeypatch):
    from app.agent import knowledge as knowledge_mod
    from app.agent.tools import KnowledgeSearchParams, _knowledge_search

    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(knowledge_mod, "get_knowledge_store", _boom)
    result = asyncio.run(_knowledge_search(KnowledgeSearchParams(query="任意")))
    assert result["count"] == 0
    assert "unavailable" in result["note"]


def test_mock_registry_stubs_knowledge_search():
    from app.agent.mock_factories import build_mock_registry

    registry = build_mock_registry()
    tool = registry.get("knowledge_search")
    assert tool.handler is not None
    assert tool.handler.__name__ == "_stub_knowledge_search"


# ---------- M2: planner routing rules + Read-Gate ----------


def test_planner_template_has_adaptive_routing_and_read_gate():
    from app.agent.planner import PLANNER_TEMPLATE

    flat = " ".join(PLANNER_TEMPLATE.split())  # 换行/缩进归一, 锚点断言不受排版影响
    assert "knowledge_search" in flat
    assert "Adaptive retrieval" in flat
    assert "fall back to crawl_notes" in flat
    assert "[K1]..[Kn]" in flat
    assert "Never invent K tags" in flat
    assert "MUST name the Kx" in flat


# ---------- M2-v2: 结构化强制(调用级插步 + 消费级 hints) ----------


def _ktext() -> str:
    return (
        "\nRelevant knowledge from past runs (cite as K1..Kn when used):\n"
        "- [K1|output] 西电实习避坑指南: 三段实习的老学姐经验\n"
        "- [K2|crawl] 西电保研: 预推免与导师联系\n"
    )


def test_inject_knowledge_steps_inserts_search_and_hints():
    from app.agent.orchestrator import _inject_knowledge_steps
    from app.agent.schemas import Plan, PlanStep

    plan = Plan(goal="g", steps=[
        PlanStep(action="load_notes", params={}),
        PlanStep(action="analyze_notes", params={"items": "$latest.load_notes"}),
        PlanStep(action="generate_topics", params={}),
        PlanStep(action="generate_contents", params={}),
    ])
    stats = _inject_knowledge_steps(plan, _ktext(), "西电毕业去向目标")
    assert stats == {"search_step": 1, "hint_steps": 2}
    assert plan.steps[0].action == "knowledge_search"
    assert plan.steps[0].params["query"] == "西电毕业去向目标"
    for step in plan.steps:
        if step.action in ("generate_topics", "generate_contents"):
            assert len(step.params["knowledge_hints"]) == 2
            assert step.params["knowledge_hints"][0].startswith("[K1|output]")


def test_inject_knowledge_steps_idempotent_and_noop():
    from app.agent.orchestrator import _inject_knowledge_steps
    from app.agent.schemas import Plan, PlanStep

    # 无知识文本 -> 完全 no-op
    plan = Plan(goal="g", steps=[PlanStep(action="generate_topics", params={})])
    assert _inject_knowledge_steps(plan, "", "g") == {}
    assert plan.steps[0].action == "generate_topics"

    # 已有 knowledge_search 步 / 已带 hints -> 不重复注入
    plan = Plan(goal="g", steps=[
        PlanStep(action="knowledge_search", params={"query": "x"}),
        PlanStep(action="generate_topics", params={"knowledge_hints": ["K9|manual|既有"]}),
    ])
    stats = _inject_knowledge_steps(plan, _ktext(), "g")
    assert stats == {"search_step": 0, "hint_steps": 0}
    assert len(plan.steps) == 2
    assert plan.steps[1].params["knowledge_hints"] == ["K9|manual|既有"]


def test_request_models_accept_knowledge_hints():
    from app.models.schemas import ContentGenerateRequest, TopicGenerateRequest

    t = TopicGenerateRequest(summary="s", top_keywords=[], top_tags=[], title_patterns=[], insight_points=[], knowledge_hints=["K1|output|x"])
    c = ContentGenerateRequest(topic="t", reason="r", knowledge_hints=["K1|output|x"])
    assert t.knowledge_hints == ["K1|output|x"]
    assert c.knowledge_hints == ["K1|output|x"]
    assert TopicGenerateRequest(summary="s", top_keywords=[], top_tags=[], title_patterns=[], insight_points=[]).knowledge_hints == []


def test_tools_forward_knowledge_hints(store, monkeypatch):
    """_topics/_contents 把 hints 透传给 service(用 stub 截获断言)。"""
    import app.agent.tools as tools_mod

    captured = {}

    def fake_topics(req):
        captured["topics"] = req
        class _R:
            def model_dump(self):
                return {"topics": [{"title": "t", "reason": "r"}]}
        return _R()

    def fake_contents(req):
        captured["contents"] = req
        class _R:
            def model_dump(self):
                return {"contents": [{"title": "t", "body": "b"}]}
        return _R()

    # _topics/_contents 函数内 lazy import service, 须 patch service 模块本体
    import app.services.content_service as content_svc
    import app.services.topic_service as topic_svc

    monkeypatch.setattr(topic_svc, "generate_topics", fake_topics)
    monkeypatch.setattr(content_svc, "generate_contents", fake_contents)
    import asyncio

    asyncio.run(tools_mod._topics(tools_mod.TopicsParams(summary="s", knowledge_hints=["K1|output|x"])))
    asyncio.run(tools_mod._contents(tools_mod.ContentsParams(topic="t", reason="r", knowledge_hints=["K1|output|x"])))
    assert captured["topics"].knowledge_hints == ["K1|output|x"]
    assert captured["contents"].knowledge_hints == ["K1|output|x"]
