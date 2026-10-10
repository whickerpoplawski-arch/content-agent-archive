"""Login-state detection for the crawler (optimization plan P2-b)."""

from __future__ import annotations

from app.services.local_site_crawler_service import login_state_from_url


def test_login_state_detects_redirect_urls():
    assert login_state_from_url("https://www.xiaohongshu.com/login?from=...") is True
    assert login_state_from_url("https://www.xiaohongshu.com/passport/web/login") is True
    # /explore is the public homepage/feed, shown to both logged-in and logged-out
    # users; it must NOT be treated as a login redirect (would false-positive on
    # valid logged-in sessions and break the crawler).
    assert login_state_from_url("https://www.xiaohongshu.com/explore") is False


def test_login_state_accepts_normal_pages():
    assert (
        login_state_from_url("https://www.xiaohongshu.com/discovery/item/abc123") is False
    )
    assert login_state_from_url("") is False
    assert login_state_from_url(None) is False


# ============================================================================
# Optimization plan 12.7 / 12.8 (crawler side)
# ============================================================================

import asyncio


def run(coro):
    return asyncio.run(coro)


class _FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return self._page._count(self._selector)

    async def inner_text(self):
        return self._page._text(self._selector)

    async def get_attribute(self, name):
        return self._page._attr(self._selector, name)

    async def all_inner_texts(self):
        return self._page._all_texts(self._selector)


class _FakePage:
    def __init__(self, meta=None, texts=None, title="标题 - 小红书"):
        self._meta = meta or {}
        self._texts = texts or {}
        self._title = title

    def locator(self, selector):
        return _FakeLocator(self, selector)

    async def goto(self, url, **kw):
        self.url = url

    async def title(self):
        return self._title

    def _count(self, selector):
        return 1 if (selector in self._meta or selector in self._texts) else 0

    def _text(self, selector):
        return self._texts.get(selector)

    def _attr(self, selector, name):
        return self._meta.get(selector)

    def _all_texts(self, selector):
        return []


def test_interaction_dom_fallback():
    """plan 12.7: DOM fallback parses interaction counts; data_missing marks gaps."""
    from app.services.local_site_crawler_service import XHSCrawler
    from app.models.schemas import SearchCrawlRequest

    crawler = XHSCrawler(SearchCrawlRequest(keywords=["k"], target_count=1))
    card = {"url": "https://www.xiaohongshu.com/explore/abc", "author": "A",
            "card_date": None, "keyword": "k"}

    # OG meta missing for all three; DOM provides likes=1.2万 and comments=300.
    page = _FakePage(
        meta={},
        texts={
            "#detail-desc": "正文内容",
            crawler.LIKE_BTN_SELECTORS[0]: "1.2万",
            crawler.COMMENT_BTN_SELECTORS[0]: "300",
        },
    )
    note = run(crawler._fetch_note_detail(page, card))
    assert note.likes == 12000
    assert note.likes_data_missing is False
    assert note.comments == 300
    assert note.comments_data_missing is False
    assert note.favorites == 0
    assert note.favorites_data_missing is True

    # Both sources missing -> 0 + data_missing True.
    page2 = _FakePage(meta={}, texts={"#detail-desc": "正文"})
    note2 = run(crawler._fetch_note_detail(page2, card))
    assert note2.likes == 0 and note2.likes_data_missing is True
    assert note2.comments == 0 and note2.comments_data_missing is True
    assert note2.favorites == 0 and note2.favorites_data_missing is True


def test_headless_config(monkeypatch):
    """plan 12.8: settings.crawl_headless is passed to chromium.launch."""
    from app.services.local_site_crawler_service import (
        XHSCrawler,
        SearchCrawlRequest,
        crawl_local_site_notes,
    )
    from app.core.config import settings

    captured = {}

    class _FakePlaywright:
        class chromium:
            @staticmethod
            async def launch(**kwargs):
                captured.update(kwargs)
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

    class _MiniCrawler(XHSCrawler):
        async def _ensure_logged_in(self, page, context):
            return None

        async def _collect_card_links(self, page, keyword):
            return []

        async def _dismiss_popups(self, page):
            return None

    monkeypatch.setattr(
        "app.services.local_site_crawler_service.async_playwright",
        lambda: _FakePlaywright(),
    )
    monkeypatch.setattr(settings, "crawl_headless", True)

    crawler = _MiniCrawler(SearchCrawlRequest(keywords=["测试"], target_count=1))
    resp = run(crawler.crawl())
    assert captured.get("headless") is True
    assert resp.count == 0

    # crawl_local_site_notes passes settings.crawl_headless into the crawler.
    monkeypatch.setattr(settings, "crawl_headless", False)
    received = {}

    def spy_init(self, request, headless=True):
        received["headless"] = headless

    async def spy_crawl(self, on_progress=None, should_cancel=None):
        from app.models.schemas import SearchCrawlResponse
        return SearchCrawlResponse(target_count=0, count=0, used_keywords=[], items=[])

    monkeypatch.setattr(XHSCrawler, "__init__", spy_init)
    monkeypatch.setattr(XHSCrawler, "crawl", spy_crawl)
    run(crawl_local_site_notes(SearchCrawlRequest(keywords=["测试"], target_count=1)))
    assert received["headless"] is False
