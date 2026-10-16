"""Offline mock factories: a deterministic LLM backend and a hermetic tool
registry so the full Agent loop (plan -> execute -> reflect) runs with no API key
and no external services. Used by tests and the CLI demo.
"""

from __future__ import annotations

from app.agent.llm import MockLLMBackend
from app.agent.consolidation import DreamOp, DreamResult, ProceduralInsight
from app.agent.schemas import (
    Plan,
    PlanStep,
    Recovery,
    Reflection,
    ContentEvaluation,
    ContentDraft,
    UserFacts,
)
from app.agent.tools import (
    ToolRegistry,
    build_default_registry,
    TopicsParams,
    ContentsParams,
    ImagesParams,
    PublishParams,
    FeishuParams,
    CrawlParams,
)

SAMPLE_NOTES = [
    {
        "title": "学生党必入的5款平价好物推荐",
        "content": "整理了几款宿舍必备的平价好物，性价比超高。",
        "likes": 320, "favorites": 210, "comments": 42,
        "tags": ["平价好物", "学生党", "好物分享"], "author": "阿花",
        "url": "https://www.xiaohongshu.com/explore/1",
    },
    {
        "title": "宿舍收纳大法！小空间也不怕乱",
        "content": "分享几个宿舍收纳技巧，空间瞬间变大一倍。",
        "likes": 280, "favorites": 190, "comments": 30,
        "tags": ["收纳", "宿舍", "教程"], "author": "小满",
        "url": "https://www.xiaohongshu.com/explore/2",
    },
    {
        "title": "新手护肤避雷指南，别再交智商税",
        "content": "盘点那些被吹上天的护肤雷品，帮你省钱。",
        "likes": 410, "favorites": 260, "comments": 66,
        "tags": ["护肤", "避雷", "指南"], "author": "安安",
        "url": "https://www.xiaohongshu.com/explore/3",
    },
    {
        "title": "通勤穿搭公式，三分钟出门",
        "content": "一套简单的通勤穿搭公式，早起不纠结。",
        "likes": 260, "favorites": 150, "comments": 21,
        "tags": ["穿搭", "通勤", "分享"], "author": "栗子",
        "url": "https://www.xiaohongshu.com/explore/4",
    },
    {
        "title": "自制奶茶教程，好喝又便宜",
        "content": "在家就能做的奶茶配方，比奶茶店还好喝。",
        "likes": 350, "favorites": 230, "comments": 48,
        "tags": ["奶茶", "教程", "美食"], "author": "桃桃",
        "url": "https://www.xiaohongshu.com/explore/5",
    },
]


def _mock_plan() -> Plan:
    return Plan(
        goal="",
        steps=[
            PlanStep(action="load_notes", params={}, rationale="获取笔记样本"),
            PlanStep(
                action="analyze_notes",
                params={"items": "$latest.load_notes"},
                rationale="分析爆款规律",
            ),
            PlanStep(
                action="generate_topics",
                params={
                    "summary": "$latest.analyze_notes.summary",
                    "top_keywords": "$latest.analyze_notes.top_keywords",
                    "top_tags": "$latest.analyze_notes.top_tags",
                    "title_patterns": "$latest.analyze_notes.title_patterns",
                    "insight_points": "$latest.analyze_notes.insight_points",
                    "audience": "大学生女生",
                    "count": 3,
                },
                rationale="基于分析生成选题",
            ),
            PlanStep(
                action="generate_contents",
                params={
                    "topic": "$latest.generate_topics.topics.0.title",
                    "reason": "$latest.generate_topics.topics.0.reason",
                    "audience": "大学生女生",
                    "tone": "真实分享",
                    "count": 2,
                },
                rationale="为选题生成文案",
            ),
        ],
    )


def build_mock_backend() -> MockLLMBackend:
    backend = MockLLMBackend()
    backend.register(Plan, _mock_plan)
    backend.register(
        Reflection,
        lambda: Reflection(should_stop=False, next_action="continue", reason="mock: continue"),
    )
    backend.register(
        Recovery,
        lambda: Recovery(next_action="replan", reason="mock: replan"),
    )
    backend.register(
        ContentEvaluation,
        lambda: ContentEvaluation(score=9, verdict="pass", issues=[]),
    )
    backend.register(
        ContentDraft,
        lambda: ContentDraft(
            title="优化标题", body="优化后的正文内容，真实口语化分享。",
            hashtags=["#平价好物", "#学生党"], cta="收藏不迷路～",
            image_suggestion="暖色调平铺特写", content_type="分享",
        ),
    )
    backend.register(
        UserFacts,
        lambda: UserFacts(
            target_domains=["大学生平价好物", "宿舍生活"],
            preferred_tone="真实口语化分享",
            preferred_structure="标题+正文+标签+互动引导",
            avoid_topics=["虚假宣传", "过度营销"],
        ),
    )
    backend.register(
        ProceduralInsight,
        lambda: ProceduralInsight(
            title="样本数据链路直出即可",
            insight="离线样本数据质量稳定时，load_notes 后可直接生成，无需爬取。",
            applies_to=["离线", "演示"],
        ),
    )
    backend.register(
        DreamResult,
        lambda: DreamResult(
            ops=[
                DreamOp(
                    op="ADD",
                    title="样本数据链路直出即可",
                    insight="离线样本数据质量稳定时，load_notes 后可直接生成，无需爬取。",
                    applies_to=["离线", "演示"],
                    confidence=0.55,
                    when_to_use="目标是演示、离线或样本数据，且不需要新鲜爬取时。",
                )
            ]
        ),
    )
    return backend


async def _load_notes_stub(params) -> list[dict]:
    return SAMPLE_NOTES


async def _stub_topics(params: TopicsParams) -> dict:
    return {
        "topics": [
            {"title": "大学生平价好物真实测评", "reason": "贴合高收藏关键词与平价好物标签"},
            {"title": "宿舍收纳避雷教程", "reason": "教程类+避雷表达更受欢迎"},
            {"title": "新手护肤避雷清单", "reason": "避雷/清单型标题点击率高"},
        ]
    }


async def _stub_contents(params: ContentsParams) -> dict:
    topic = params.topic or "大学生平价好物真实测评"
    return {
        "contents": [
            {
                "title": f"{topic}｜亲测不踩雷",
                "body": "这期整理了近期回购的平价好物，真的都是自用才敢推荐，学生党放心冲！",
                "hashtags": ["#平价好物", "#学生党", "#真实测评", "#好物分享"],
                "cta": "你们还有哪些宝藏好物？评论区告诉我吧～",
                "image_suggestion": "一张平铺在书桌上的各类好物特写，暖色调",
                "content_type": "测评",
            },
            {
                "title": f"{topic}｜宿舍也能用",
                "body": "宿舍党看过来，这几件便宜又实用，收纳、护肤一次搞定，亲测好用。",
                "hashtags": ["#宿舍", "#平价", "#测评"],
                "cta": "收藏起来慢慢看，下次不迷路～",
                "image_suggestion": "宿舍桌面收纳前后对比图，明亮自然光",
                "content_type": "分享",
            },
        ]
    }


async def _stub_images(params: ImagesParams) -> list[str]:
    return ["data/output/images/demo_0.png", "data/output/images/demo_1.png"]


async def _stub_publish(params: PublishParams) -> dict:
    return {"success": True, "message": "mock 发布成功（未真实调用 XHS）", "mode": "mcp"}


async def _stub_feishu(params: FeishuParams) -> dict:
    return {"success": True, "message": "mock 飞书同步（离线跳过）"}


async def _stub_crawl(params: CrawlParams) -> dict:
    return {"target_count": params.target_count, "count": 0, "used_keywords": [], "items": []}


async def _stub_knowledge_search(params) -> dict:
    # 密闭性: mock run 里 planner 若选 knowledge_search, 不触真实 knowledge.db。
    return {"count": 0, "hits": [], "note": "mock: knowledge base offline"}


def build_mock_registry() -> ToolRegistry:
    overrides = {
        "load_notes": _load_notes_stub,
        "generate_topics": _stub_topics,
        "generate_contents": _stub_contents,
        "generate_images": _stub_images,
        "publish_to_xhs": _stub_publish,
        "sync_to_feishu": _stub_feishu,
        "crawl_notes": _stub_crawl,
        "knowledge_search": _stub_knowledge_search,
    }
    return build_default_registry(overrides)
