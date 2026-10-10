"""
XHS Crawler - production run script.
Run: python test_crawler.py
"""
import asyncio
import sys
import json

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from app.models.schemas import SearchCrawlRequest
from app.services.local_site_crawler_service import crawl_local_site_notes


async def main():
    request = SearchCrawlRequest(
        target_count=10,           # 目标采集数量
        max_pages_per_keyword=2,   # 每个关键词滚动次数
        use_infinite_scroll=True,
        content_type="图文",
    )

    print("=== XHS 爬虫启动 ===")
    print(f"目标: {request.target_count} 条图文笔记（评论≥200，近1年）\n")

    result = await crawl_local_site_notes(request)

    print(f"\n=== 采集完成: {result.count} / {result.target_count} 条 ===")
    print(f"使用关键词({len(result.used_keywords)}个): {result.used_keywords[:5]}...\n")

    for i, note in enumerate(result.items, 1):
        print(f"[{i:02d}] {note.title[:40]}")
        print(f"     作者:{note.author}  评论:{note.comments}  点赞:{note.likes}  收藏:{note.favorites}")
        print(f"     日期:{note.publish_time}  关键词:{note.keyword_used}")
        print(f"     URL: {note.url}")
        print()

    out = [n.model_dump() for n in result.items]
    with open("data/raw/test_crawl_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"结果已保存到 data/raw/test_crawl_result.json")


if __name__ == "__main__":
    asyncio.run(main())
