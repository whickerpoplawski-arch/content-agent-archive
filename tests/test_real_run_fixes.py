"""Fixes from the 2026-08-12 real-run report (docs/real-run-issues-20260812.md §13).

Covers the "三刀" P1 path: data-missing downgrade (13.6), crawl cancellation +
progress (13.2), empty-crawl circuit breaker (13.1), identical-error stop
(13.4), and the P3 health/version fingerprint (13.3).
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.main import app
from app.agent.executor import Executor
from app.agent.memory import AgentMemory, LongTermMemory
from app.agent.planner import Planner, Reflector
from app.agent.schemas import Plan, PlanStep, Recovery, Reflection, StepStatus
from app.agent.tools import CrawlParams, Tool, ToolRegistry
from app.agent.llm import MockLLMBackend

client = TestClient(app)


def run(coro):
    return asyncio.run(coro)


# ============================================================================
# P1-1: data_missing downgrade in _is_valid (13.6 三刀 #1)
# ============================================================================

def _crawler(**kw):
    from app.models.schemas import SearchCrawlRequest
    from app.services.local_site_crawler_service import XHSCrawler

    defaults = {"keywords": ["k"], "min_comments": 5, "min_likes": 100, "min_favorites": 10}
    defaults.update(kw)
    return XHSCrawler(SearchCrawlRequest(**defaults))


def test_is_valid_downgrades_missing_interaction_data():
    from app.models.schemas import NoteItem

    crawler = _crawler()
    note = NoteItem(
        title="t", content="正文",
        comments=0, comments_data_missing=True,
        likes=0, likes_data_missing=True,
        favorites=0, favorites_data_missing=True,
        publish_time="2026-01-01",
    )
    # 互动数据全部缺失 -> 降门槛放行（不再按 0 拒绝）
    assert crawler._is_valid(note) is True

    # 某互动非缺失但不足 -> 仍拒绝
    assert crawler._is_valid(note.model_copy(update={"likes_data_missing": False})) is False
    # 非缺失且达标 -> 通过
    note_ok = note.model_copy(
        update={
            "likes": 120, "likes_data_missing": False,
            "comments": 6, "comments_data_missing": False,
            "favorites": 12, "favorites_data_missing": False,
        }
    )
    assert crawler._is_valid(note_ok) is True


def test_is_valid_passes_when_publish_time_missing():
    from app.models.schemas import NoteItem

    crawler = _crawler()
    note = NoteItem(
        title="t", content="正文",
        likes=999, likes_data_missing=False,
        comments=999, comments_data_missing=False,
        favorites=999, favorites_data_missing=False,
        publish_time=None,
    )
    # 日期缺失 -> 放行（不假拒绝）
    assert crawler._is_valid(note) is True
    # 有日期但过期 -> 仍拒绝
    assert crawler._is_valid(note.model_copy(update={"publish_time": "2020-01-01"})) is False


def test_is_valid_still_requires_content_and_relevance():
    from app.models.schemas import NoteItem

    crawler = _crawler(topic_words=["西电"])
    note = NoteItem(
        title="t", content="正文",
        likes=0, likes_data_missing=True,
        comments=0, comments_data_missing=True,
        favorites=0, favorites_data_missing=True,
        publish_time="2026-01-01",
    )
    # 无内容 -> 拒绝
    assert crawler._is_valid(note.model_copy(update={"content": ""})) is False
    # 不相关话题 -> 拒绝
    assert crawler._is_valid(note.model_copy(update={"title": "无关", "content": "不相关内容"})) is False


# ============================================================================
# P1-2: crawl cancellation + progress through executor ctx (13.2)
# ============================================================================

class _FakePlaywright:
    class chromium:
        @staticmethod
        async def launch(**kwargs):
            return _FakeBrowser()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeBrowser:
    async def new_context(self, **kw):
        return _FakeContext()

    async def close(self):
        pass


class _FakeContext:
    async def new_page(self):
        return object()


def _mini_crawler(monkeypatch, links=()):
    from app.services.local_site_crawler_service import XHSCrawler

    class _MiniCrawler(XHSCrawler):
        async def _ensure_logged_in(self, page, context):
            return None

        async def _collect_card_links(self, page, keyword):
            return list(links)

        async def _dismiss_popups(self, page):
            return None

    monkeypatch.setattr(
        "app.services.local_site_crawler_service.async_playwright",
        lambda: _FakePlaywright(),
    )
    return _MiniCrawler


def test_crawl_cancelled_via_should_cancel(monkeypatch):
    from app.models.schemas import SearchCrawlRequest

    MiniCrawler = _mini_crawler(monkeypatch)
    crawler = MiniCrawler(SearchCrawlRequest(keywords=["k"], target_count=5))
    resp = run(crawler.crawl(should_cancel=lambda: True))
    assert resp.cancelled is True
    assert resp.count == 0
    assert resp.used_keywords == []


def _executor(registry, backend, tracer=None):
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="continue", reason="continue"),
    )
    planner = Planner(backend, registry.specs())
    reflector = Reflector(backend)
    return Executor(registry, reflector, planner, tracer=tracer, max_parallel=1)


class _Tracer:
    run_id = "r1"

    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))

    def tool_call(self, name, ok, elapsed_ms, summary=""):
        self.events.append(("tool_call", {"name": name, "ok": ok}))


def test_crawl_progress_piped_through_executor_ctx():
    """The executor sets _crawl_ctx around crawl_notes; a handler reading it can
    emit crawl_progress events (SSE/console now see 已收集 N/target)."""
    seen = []

    async def crawl_handler(params: CrawlParams):
        from app.agent.executor import _crawl_ctx

        ctx = _crawl_ctx.get()
        ctx["sink"]("crawl_progress", collected=1, target=5, keyword="k")
        return {"count": 1, "target_count": 5, "used_keywords": ["k"], "items": []}

    registry = ToolRegistry().register(
        Tool("crawl_notes", "crawl", CrawlParams, crawl_handler)
    )
    tracer = _Tracer()
    executor = _executor(registry, MockLLMBackend(), tracer=tracer)
    plan = Plan(
        goal="crawl",
        steps=[PlanStep(action="crawl_notes", params={"keywords": ["k"], "target_count": 5})],
    )
    result = run(
        executor.execute("crawl", "profile", plan, AgentMemory(LongTermMemory()))
    )
    assert ("crawl_progress", {"collected": 1, "target": 5, "keyword": "k"}) in tracer.events
    assert result.steps[0].status == StepStatus.SUCCEEDED


# ============================================================================
# P1-3: empty-crawl circuit breaker (13.1) + identical-error stop (13.4)
# ============================================================================

def test_crawl_empty_circuit_breaker_stops_after_second_empty():
    calls = {"n": 0}

    async def crawl_handler(params: CrawlParams):
        calls["n"] += 1
        return {"count": 0, "target_count": 5, "used_keywords": ["k"], "items": []}

    registry = ToolRegistry().register(
        Tool("crawl_notes", "crawl", CrawlParams, crawl_handler)
    )
    executor = _executor(registry, MockLLMBackend())
    plan = Plan(
        goal="crawl",
        steps=[
            PlanStep(action="crawl_notes", params={"keywords": ["k"], "target_count": 5}),
            PlanStep(action="crawl_notes", params={"keywords": ["k"], "target_count": 5}),
        ],
    )
    run(executor.execute("crawl", "profile", plan, AgentMemory(LongTermMemory())))

    # 第一次空爬放行(交给反射),第二次空爬熔断 -> 不再执行第三个工具
    assert calls["n"] == 2
    assert executor._stop_requested is True


def test_crawl_cancelled_result_stops_run():
    calls = {"n": 0}

    async def crawl_handler(params: CrawlParams):
        calls["n"] += 1
        return {"count": 3, "target_count": 5, "used_keywords": ["k"],
                "items": [], "cancelled": True}

    registry = ToolRegistry().register(
        Tool("crawl_notes", "crawl", CrawlParams, crawl_handler)
    )
    executor = _executor(registry, MockLLMBackend())
    plan = Plan(
        goal="crawl",
        steps=[
            PlanStep(action="crawl_notes", params={"keywords": ["k"], "target_count": 5}),
            PlanStep(action="crawl_notes", params={"keywords": ["k"], "target_count": 5}),
        ],
    )
    run(executor.execute("crawl", "profile", plan, AgentMemory(LongTermMemory())))
    assert calls["n"] == 1  # cancelled 结果 -> 立即停止
    assert executor._stop_requested is True


def test_identical_error_stops_retries():
    calls = {"n": 0}

    async def fail_handler(params: CrawlParams):
        calls["n"] += 1
        raise ValueError("boom")

    registry = ToolRegistry().register(
        Tool("crawl_notes", "crawl", CrawlParams, fail_handler)
    )
    backend = MockLLMBackend()
    backend.register(
        Recovery,
        lambda: Recovery(next_action="retry", reason="retry"),
    )
    executor = _executor(registry, backend)
    plan = Plan(
        goal="crawl",
        steps=[PlanStep(action="crawl_notes", params={"keywords": ["k"], "target_count": 5})],
    )
    result = run(
        executor.execute("crawl", "profile", plan, AgentMemory(LongTermMemory()))
    )
    # 第 1 次失败重试;第 2 次相同错误 -> 停止无效重试
    assert calls["n"] == 2
    assert result.steps[0].status == StepStatus.FAILED
    assert executor._stop_requested is True


# ============================================================================
# P3: health/version code fingerprint (13.3)
# ============================================================================

def test_health_reports_code_fingerprint():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code_fingerprint"]
    assert body["uptime_seconds"] >= 0


def test_health_version_endpoint():
    resp = client.get("/health/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]
    assert body["code_fingerprint"]
