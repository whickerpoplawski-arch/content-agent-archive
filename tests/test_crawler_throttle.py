"""Tests for crawler anti-detection throttling (docs/crawl-anti-detection.md).

All timing is monkeypatched — these tests never touch the real platform.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.schemas import SearchCrawlRequest
from app.services.local_site_crawler_service import XHSCrawler


@pytest.fixture()
def crawler():
    req = SearchCrawlRequest(
        keywords=["宿舍收纳"], topic_words=[], min_comments=0, min_likes=0,
        min_favorites=0, target_count=5, content_type="图文",
    )
    return XHSCrawler(req, headless=True)


def test_page_gap_uses_configured_delay(crawler, monkeypatch):
    """详情页间隔走 settings 配置区间."""
    from app.core.config import settings

    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings.crawl_delay_min = 3.0
    settings.crawl_delay_max = 6.0
    settings.crawl_burst_size = 0  # disable burst for this test
    asyncio.run(crawler._page_gap())
    assert 3.0 <= sleeps[0] <= 6.0


def test_page_gap_burst_rest_after_n_pages(crawler, monkeypatch):
    """连续 burst_size 页后应插入长休息而非普通间隔."""
    from app.core.config import settings

    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings.crawl_burst_size = 3
    settings.crawl_delay_min, settings.crawl_delay_max = 1.0, 2.0
    settings.crawl_burst_rest_min, settings.crawl_burst_rest_max = 15.0, 30.0
    for _ in range(3):
        asyncio.run(crawler._page_gap())
    assert len(sleeps) == 3
    assert 1.0 <= sleeps[0] <= 2.0
    assert 1.0 <= sleeps[1] <= 2.0
    assert 15.0 <= sleeps[2] <= 30.0  # third page triggers the long rest


def test_randomized_delays_deterministic_bounds(crawler, monkeypatch):
    from app.core.config import settings

    settings.crawl_delay_min, settings.crawl_delay_max = 3.0, 6.0
    settings.crawl_burst_size = 0
    vals = []
    for _ in range(20):
        vals.append(crawler._rand(settings.crawl_delay_min, settings.crawl_delay_max))
    assert all(3.0 <= v <= 6.0 for v in vals)
    assert len(set(vals)) > 5  # actually random, not fixed
