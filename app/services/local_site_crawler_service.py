import asyncio
import json
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import quote

from playwright.async_api import async_playwright, Page, BrowserContext

from app.models.schemas import (
    SearchCrawlRequest,
    SearchCrawlResponse,
    NoteItem,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XHS_BASE = "https://www.xiaohongshu.com"
COOKIES_FILE = "data/raw/xhs_cookies.json"
STATE_FILE   = "data/raw/xhs_state.json"

INVALID_AD_WORDS = ["商单", "广告", "投放合作", "品牌合作"]
VIDEO_TYPE_KEYWORDS = ["视频", "直播", "合集"]

LOGIN_REDIRECT_MARKERS = ["/login", "login_redirect", "passport", "signin"]
# 注意：`/explore` 是小红书首页/Feed（登录态与未登录都会落地于此），
# 把它当作登录重定向标记会误判已登录会话，导致爬虫永远失败，故不纳入。


def login_state_from_url(url: str) -> bool:
    """Return True when the URL indicates a login-required redirect (plan P2-b)."""
    if not url:
        return False
    return any(marker in url for marker in LOGIN_REDIRECT_MARKERS)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class XHSCrawler:
    """
    Two-phase crawler for real xiaohongshu.com.

    Phase 1 – Search pages: collect card URLs, skip video cards, pre-filter by date.
    Phase 2 – Detail pages: extract all data from detail page as single source of truth.
    Filters: 图文-only, comments > threshold, within 1 year, relevant topic.
    """

    # ---------- Search-page selectors ----------
    CARD_SELECTOR      = "section.note-item"
    CARD_LINK_SELECTOR = "a.cover"
    CARD_AUTHOR_SELECTOR = ".author"
    CARD_DATE_SELECTOR   = ".time"
    VIDEO_CARD_SELECTORS = [
        ".video-badge", ".play-icon", ".duration", "[class*='video-mark']",
    ]

    # ---------- Detail page selectors ----------
    # Content body: primary #detail-desc, with DOM fallbacks for real pages
    # where the primary selector is missing (optimization plan 13.6 / 12.7).
    DETAIL_CONTENT_SELECTOR = "#detail-desc"
    DETAIL_CONTENT_SELECTORS = ["#detail-desc", ".note-content", "[class*='desc']", ".content"]
    DETAIL_DATE_SELECTORS   = [".bottom-container .date", ".note-header .date", ".date"]
    DETAIL_TAG_SELECTORS    = ["#detail-desc .tag", "#hash-tag", ".tag"]

    # Counts from Open Graph meta tags (most reliable)
    META_COMMENT = "meta[name='og:xhs:note_comment']"
    META_LIKE    = "meta[name='og:xhs:note_like']"
    META_COLLECT = "meta[name='og:xhs:note_collect']"

    # DOM fallback selectors (12.7): button-adjacent count text when OG meta is
    # missing on the detail page. Best-effort; verified by unit tests on parsing.
    LIKE_BTN_SELECTORS = [
        ".engage-bar .like .count",
        ".engage-bar .count",
        ".like-count",
        "[class*='like'] [class*='count']",
        ".count",
    ]
    COMMENT_BTN_SELECTORS = [
        ".engage-bar .comment .count",
        ".engage-bar .count",
        ".comment-count",
        "[class*='comment'] [class*='count']",
        ".count",
    ]
    COLLECT_BTN_SELECTORS = [
        ".engage-bar .collect .count",
        ".engage-bar .count",
        ".collect-count",
        "[class*='collect'] [class*='count']",
        ".count",
    ]

    POPUP_CLOSE_SELECTORS = [
        ".close-btn", ".modal-close", "[class*='close-icon']", "button.close",
    ]

    def __init__(self, request: SearchCrawlRequest, headless: bool = True):
        self.request = request
        self.headless = headless
        self.collected: List[NoteItem] = []
        self.seen_urls: Set[str] = set()
        self.used_keywords: List[str] = []
        self.needs_login = False
        self.cancelled = False
        # 风控规避节流状态 (docs/crawl-anti-detection.md)
        self._pages_since_rest = 0

    # ------------------------------------------------------------------
    # Anti-detection throttling helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rand(key_min: float, key_max: float) -> float:
        from app.core.config import settings
        return random.uniform(key_min, max(key_min, key_max))

    async def _polite_pause(self, lo: float, hi: float, why: str) -> None:
        """Randomized human-pace delay between platform requests."""
        delay = self._rand(lo, hi)
        print(f"  ~ [{why}] 停留 {delay:.1f}s")
        await asyncio.sleep(delay)

    async def _page_gap(self) -> None:
        """Delay between detail-page visits; every `burst_size` pages take a
        longer rest so a crawl looks like intermittent reading, not a sweep."""
        from app.core.config import settings
        self._pages_since_rest += 1
        if (
            settings.crawl_burst_size > 0
            and self._pages_since_rest >= settings.crawl_burst_size
        ):
            self._pages_since_rest = 0
            await self._polite_pause(
                settings.crawl_burst_rest_min, settings.crawl_burst_rest_max, "长休息"
            )
        else:
            await self._polite_pause(
                settings.crawl_delay_min, settings.crawl_delay_max, "详情页间隔"
            )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def crawl(self, on_progress=None, should_cancel=None) -> SearchCrawlResponse:
        keywords = self.request.keywords

        def _interrupted() -> bool:
            """13.2: cooperative cancellation so an in-flight crawl can be
            interrupted (stop_requested / asyncio cancel) at loop boundaries
            instead of running to completion."""
            return bool(should_cancel and should_cancel())
        state_path = Path(STATE_FILE)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )

            ctx_kwargs = {
                # viewport 随机化: 固定 1440x900 是常见爬虫指纹; 从常见笔记本
                # 分辨率中随机取一个, 每次爬取略不同。
                "viewport": {
                    "width": random.choice([1366, 1440, 1536, 1600, 1920]),
                    "height": random.choice([768, 864, 900, 1000, 1080]),
                },
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            if state_path.exists():
                ctx_kwargs["storage_state"] = str(state_path)
                print(f"[XHSCrawler] 已加载持久化登录态：{STATE_FILE}")

            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            await self._ensure_logged_in(page, context)

            try:
                for kw_idx, keyword in enumerate(keywords):
                    if _interrupted():
                        self.cancelled = True
                        break
                    if len(self.collected) >= self.request.target_count:
                        break
                    # 多关键词之间冷却: 关键词切换是平台最易观测的扫控行为。
                    if kw_idx > 0:
                        from app.core.config import settings
                        await self._polite_pause(
                            settings.crawl_keyword_cooldown_min,
                            settings.crawl_keyword_cooldown_max,
                            "关键词冷却",
                        )
                        if _interrupted():
                            self.cancelled = True
                            break
                    self.used_keywords.append(keyword)
                    print(f"\n[搜索] {keyword}")
                    links = await self._collect_card_links(page, keyword)
                    print(f"  → 找到候选卡片 {len(links)} 张")

                    for card in links:
                        if _interrupted():
                            self.cancelled = True
                            break
                        if len(self.collected) >= self.request.target_count:
                            break
                        note = await self._fetch_note_detail(page, card)
                        if note and self._is_valid(note):
                            self._add_note(note)
                            if on_progress:
                                await on_progress(
                                    {
                                        "collected": len(self.collected),
                                        "target": self.request.target_count,
                                        "keyword": keyword,
                                    }
                                )
                        # 每次详情页访问后节流(无论成败), 不与目标数耦合。
                        await self._page_gap()

                await browser.close()

            except Exception:
                await browser.close()
                raise

        print(f"\n[完成] 共采集 {len(self.collected)} / {self.request.target_count} 条"
              + ("（已中断）" if self.cancelled else ""))
        return SearchCrawlResponse(
            target_count=self.request.target_count,
            count=len(self.collected),
            used_keywords=self.used_keywords,
            items=self.collected,
            needs_login=self.needs_login,
            cancelled=self.cancelled,
        )

    # ------------------------------------------------------------------
    # Phase 1: collect card links
    # ------------------------------------------------------------------

    async def _collect_card_links(self, page: Page, keyword: str) -> List[dict]:
        from app.core.config import settings

        search_url = f"{XHS_BASE}/search_result?keyword={quote(keyword)}&source=web_explore_feed"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(
            settings.crawl_search_delay_min, settings.crawl_search_delay_max
        ))
        await self._dismiss_popups(page)

        # One scroll pass to load more cards, then collect
        await self._scroll_page(page, rounds=2)

        links: List[dict] = []
        seen_in_batch: Set[str] = set()

        cards = await page.locator(self.CARD_SELECTOR).all()
        for card in cards:
            try:
                if await self._is_video_card(card):
                    continue

                href = await card.locator(self.CARD_LINK_SELECTOR).first.get_attribute("href")
                if not href:
                    continue

                url = href if href.startswith("http") else f"{XHS_BASE}{href}"
                if url in self.seen_urls or url in seen_in_batch:
                    continue
                seen_in_batch.add(url)

                author_raw = await self._safe_text(card, self.CARD_AUTHOR_SELECTOR)
                # .author element may include date/count on second line — take first line only
                author    = author_raw.split("\n")[0].strip() if author_raw else None
                date_text = await self._safe_text(card, self.CARD_DATE_SELECTOR)
                card_date = self._normalize_date(date_text)

                # Pre-filter: skip cards clearly older than 1 year
                if date_text and card_date and not self._is_within_one_year(card_date):
                    continue

                links.append({
                    "url": url,
                    "author": author.strip() if author else None,
                    "card_date": card_date,
                    "keyword": keyword,
                })
            except Exception:
                continue

        return links

    async def _is_video_card(self, card) -> bool:
        for selector in self.VIDEO_CARD_SELECTORS:
            try:
                if await card.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        try:
            tag_area = card.locator(".bottom-tag-area")
            if await tag_area.count() > 0:
                text = await tag_area.first.inner_text()
                if any(kw in text for kw in VIDEO_TYPE_KEYWORDS):
                    return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Phase 2: fetch detail page (single source of truth)
    # ------------------------------------------------------------------

    async def _fetch_note_detail(self, page: Page, card: dict) -> Optional[NoteItem]:
        url = card["url"]
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # 停留时间人类化: 旧值 1.0-1.8s 过快, 真实阅读至少数秒。
            await asyncio.sleep(random.uniform(2.0, 4.0))
            await self._dismiss_popups(page)

            # Title: strip " - 小红书" suffix; take first line; cap at 100 chars
            raw_title = await page.title()
            title = re.sub(r"\s*[-–—]\s*小红书.*$", "", raw_title).strip()
            title = title.split("\n")[0].strip()[:100]

            content       = await self._safe_text_candidates(page, self.DETAIL_CONTENT_SELECTORS) or ""
            date_raw      = await self._safe_text_candidates(page, self.DETAIL_DATE_SELECTORS)
            comments_text = await self._meta_content(page, self.META_COMMENT)
            like_text     = await self._meta_content(page, self.META_LIKE)
            collect_text  = await self._meta_content(page, self.META_COLLECT)

            # 12.7: fall back to DOM button counts when OG meta is absent; mark
            # *_data_missing when both sources yield nothing instead of faking 0.
            like_missing = like_text is None
            if like_missing:
                like_text = await self._dom_count(page, self.LIKE_BTN_SELECTORS)
            comments_missing = comments_text is None
            if comments_missing:
                comments_text = await self._dom_count(page, self.COMMENT_BTN_SELECTORS)
            collect_missing = collect_text is None
            if collect_missing:
                collect_text = await self._dom_count(page, self.COLLECT_BTN_SELECTORS)

            tags: List[str] = []
            for selector in self.DETAIL_TAG_SELECTORS:
                try:
                    loc = page.locator(selector)
                    if await loc.count() > 0:
                        tags = [t.strip() for t in await loc.all_inner_texts() if t.strip()]
                        break
                except Exception:
                    continue

            publish_time = self._normalize_date(date_raw) or card.get("card_date")

            return NoteItem(
                title=title,
                content=content.strip(),
                author=card.get("author"),
                comments=self._parse_number(comments_text),
                likes=self._parse_number(like_text),
                favorites=self._parse_number(collect_text),
                likes_data_missing=like_missing and not like_text,
                comments_data_missing=comments_missing and not comments_text,
                favorites_data_missing=collect_missing and not collect_text,
                tags=tags,
                publish_time=publish_time,
                url=url,
                content_type="图文",
                keyword_used=card["keyword"],
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _is_valid(self, note: NoteItem) -> bool:
        # Content must exist (title can be empty)
        if not note.content:
            return False

        text = f"{note.title} {note.content}"

        # Skip ads
        if any(w in text for w in INVALID_AD_WORDS):
            return False

        # Must be relevant topic（为空则跳过此过滤）
        if self.request.topic_words and not any(w in text for w in self.request.topic_words):
            return False

        # 评论数、点赞数、收藏数:仅当互动数据非缺失时才要求 >= 设定值。
        # *_data_missing=true(OG meta 与 DOM 兜底都取不到)时降门槛放行,只保留
        # 内容+时间+相关性校验,避免真实页面互动缺失导致整批 0 条(13.6 三刀#1)。
        if not note.comments_data_missing and note.comments < self.request.min_comments:
            return False
        if not note.likes_data_missing and note.likes < self.request.min_likes:
            return False
        if not note.favorites_data_missing and note.favorites < self.request.min_favorites:
            return False

        # Published within 1 year (日期缺失时放行,不假拒绝;有日期且过期才拒绝)
        if note.publish_time and not self._is_within_one_year(note.publish_time):
            return False

        return True

    def _is_within_one_year(self, publish_time: Optional[str]) -> bool:
        if not publish_time:
            return False
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                dt = datetime.strptime(publish_time.strip(), fmt)
                return dt >= datetime.now() - timedelta(days=365)
            except ValueError:
                continue
        return False

    def _add_note(self, note: NoteItem) -> None:
        url = (note.url or "").strip()
        if url and url in self.seen_urls:
            return
        if url:
            self.seen_urls.add(url)
        self.collected.append(note)
        preview = (note.title or note.content)[:30]
        print(f"  * [已收集 {len(self.collected)} / {self.request.target_count}] {preview}")

    # ------------------------------------------------------------------
    # Page utilities
    # ------------------------------------------------------------------

    async def _scroll_page(self, page: Page, rounds: int = 2) -> None:
        for _ in range(rounds):
            await page.evaluate("window.scrollBy(0, 900)")
            await asyncio.sleep(random.uniform(1.5, 3.0))

    async def _ensure_logged_in(self, page: Page, context: BrowserContext) -> None:
        await page.goto(XHS_BASE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(2.0, 3.5))

        # 登录态失效/未登录感知（plan P2-b）：若被重定向到登录页则快速失败。
        if login_state_from_url(page.url):
            self.needs_login = True
            raise RuntimeError(
                "[XHSCrawler] 检测到登录重定向（登录态失效/未登录）。"
                "请重新完成登录并保存登录态后重试。"
            )

        # 已有持久化 state，直接跳过登录
        if Path(STATE_FILE).exists():
            print("[XHSCrawler] 持久化登录态已生效，跳过登录 *")
            return

        # 首次登录：等用户手动登录后按 Enter，然后保存 state。
        # 非交互环境（如 Agent 自主运行/后台任务）不能阻塞等待 input()，应快速失败。
        if not sys.stdin.isatty():
            raise RuntimeError(
                "[XHSCrawler] 小红书未登录且当前为非交互模式，无法等待手动登录。"
                "请先在交互终端运行 python test_crawler.py 完成登录并保存 "
                f"{STATE_FILE}，再重试。"
            )
        print("[XHSCrawler] 请在弹出的浏览器窗口中完成小红书登录，登录完成后回到此终端按 Enter 继续…")
        await asyncio.get_event_loop().run_in_executor(None, input)

        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=STATE_FILE)
        print(f"[XHSCrawler] 登录态已保存至 {STATE_FILE}，下次启动自动跳过登录 *")

    async def _dismiss_popups(self, page: Page) -> None:
        for selector in self.POPUP_CLOSE_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0:
                    await btn.click()
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Text extraction
    # ------------------------------------------------------------------

    async def _meta_content(self, page: Page, selector: str) -> Optional[str]:
        try:
            loc = page.locator(selector)
            if await loc.count() > 0:
                return await loc.first.get_attribute("content")
        except Exception:
            pass
        return None

    async def _dom_count(self, page: Page, selectors: List[str]) -> Optional[str]:
        """Try each button-adjacent selector and return the first non-empty
        inner text (used as the OG-meta fallback for interaction counts)."""
        for selector in selectors:
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    text = (await loc.first.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return None

    async def _safe_text(self, element, selector: str) -> Optional[str]:
        try:
            loc = element.locator(selector).first
            if await loc.count() > 0:
                text = await loc.inner_text()
                return text.strip() if text and text.strip() else None
        except Exception:
            pass
        return None

    async def _safe_text_candidates(self, element, selectors: List[str]) -> Optional[str]:
        for sel in selectors:
            text = await self._safe_text(element, sel)
            if text:
                return text
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _normalize_date(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        text = text.strip()
        m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        if m:
            return m.group(1).replace("/", "-")
        m = re.search(r"(\d+)天前", text)
        if m:
            return (datetime.now() - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
        if "小时前" in text or "分钟前" in text or "刚刚" in text:
            return datetime.now().strftime("%Y-%m-%d")
        m = re.search(r"(\d{1,2})-(\d{1,2})", text)
        if m:
            return f"{datetime.now().year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
        return None

    def _parse_number(self, text: Optional[str]) -> int:
        if not text:
            return 0
        text = text.strip().replace(",", "")
        low = text.lower()
        if "w" in low or "万" in low:
            m = re.search(r"(\d+(?:\.\d+)?)", low)
            if m:
                return int(float(m.group(1)) * 10000)
        if "k" in low:
            m = re.search(r"(\d+(?:\.\d+)?)", low)
            if m:
                return int(float(m.group(1)) * 1000)
        m = re.search(r"(\d+)", text)
        return int(m.group(1)) if m else 0

    # ------------------------------------------------------------------
    # Cookie loader
    # ------------------------------------------------------------------

    async def _load_cookies(self, context: BrowserContext) -> None:
        path = Path(COOKIES_FILE)
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "cookies" in raw:
                raw = raw["cookies"]

            SAME_SITE_MAP = {"strict": "Strict", "lax": "Lax", "no_restriction": "None"}
            cookies = []
            for c in raw:
                cookie: dict = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c.get("path", "/"),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                }
                if "expirationDate" in c:
                    cookie["expires"] = int(c["expirationDate"])
                elif "expires" in c:
                    cookie["expires"] = int(c["expires"])
                ss = str(c.get("sameSite", "")).lower()
                cookie["sameSite"] = SAME_SITE_MAP.get(ss, "None")
                cookies.append(cookie)

            await context.add_cookies(cookies)
            print(f"[XHSCrawler] 已加载 {len(cookies)} 个 Cookie")
        except Exception as e:
            print(f"[XHSCrawler] Cookie 加载失败: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def crawl_local_site_notes(
    request: SearchCrawlRequest,
    on_progress=None,
    should_cancel=None,
) -> SearchCrawlResponse:
    from app.core.config import settings

    crawler = XHSCrawler(request, headless=settings.crawl_headless)
    return await crawler.crawl(on_progress=on_progress, should_cancel=should_cancel)
