"""SQLite learnings, migration, scrub, and episodic index."""

from __future__ import annotations

from app.agent.memory import LongTermMemory
from app.agent.memory_scrub import ScrubRejected, scrub_memory_text
from app.agent.memory_store import ProfileStore
from app.agent.schemas import Plan, PlanStep, StepStatus


def _profile():
    return {
        "audience": "西电学生",
        "tone": "真实克制",
        "user_facts": {
            "sig-a": {
                "target_domains": ["宿舍好物"],
                "preferred_tone": "真实克制",
                "preferred_structure": "清单",
                "avoid_topics": ["夸张营销"],
            },
            "sig-b": {
                "target_domains": ["宿舍好物"],
                "preferred_tone": "真实克制",
                "preferred_structure": "清单",
                "avoid_topics": ["夸张营销"],
            },
            "sig-c": {
                "target_domains": ["美妆"],
                "preferred_tone": "活泼",
                "preferred_structure": "故事",
                "avoid_topics": ["虚假宣传"],
            },
        },
        "procedural_insights": [
            {
                "id": "pins_demo",
                "title": "美妆先分析",
                "insight": "美妆选题先分析再直出",
                "applies_to": ["美妆"],
                "lifecycle": "active",
                "confidence": 0.7,
                "hits": 2,
            }
        ],
        "episodic": [
            {
                "goal": "为大学生写宿舍好物文案",
                "signature": "",
                "steps_ok": True,
                "results": 2,
                "hits": 1,
            }
        ],
    }


def test_scrub_rejects_secret_and_redacts_email():
    secret = scrub_memory_text("api_key=sk-abcdefghijklmnopqrstuvwxyz", reject_secrets=True)
    assert secret.ok is False
    assert "unchanged" in secret.reason
    email = scrub_memory_text("联系 me@example.com 以后别写夸张", reject_secrets=True)
    assert email.ok is True
    assert "me@example.com" not in email.text
    assert email.redactions >= 1
    phone = scrub_memory_text("电话 13800138000 不要写硬广", reject_secrets=True)
    assert "13800138000" not in phone.text


def test_secret_learn_does_not_wipe_existing_memory(tmp_path):
    store = ProfileStore(tmp_path / "memory.db")
    kept = store.learn("口吻保持克制，不要夸张", kind="preference")
    try:
        store.learn("api_key=sk-abcdefghijklmnopqrstuvwxyz", kind="preference")
        raised = False
    except ScrubRejected:
        raised = True
    assert raised is True
    assert store.get(kept["id"])["status"] == "active"
    assert store.count_learnings("active") == 1


def test_migrate_profile_dedups_facts_and_is_idempotent(tmp_path):
    store = ProfileStore(tmp_path / "memory.db")
    stats = store.migrate_profile(_profile())
    assert stats["preferences"] == 2
    assert stats["facts"] >= 2
    facts = store.list_learnings(kind="fact")
    # 三份 goal 桶里只有两种事实内容。
    assert len(facts) == 2
    vetoes = [row for row in store.list_learnings(kind="veto") if row["status"] == "active"]
    assert any("夸张营销" in row["text"] for row in vetoes)
    assert any("虚假宣传" in row["text"] for row in vetoes)
    episodic = store.recall_episodic("宿舍好物")
    assert episodic and "宿舍" in episodic[0]["goal"]
    again = store.migrate_profile(_profile())
    assert again["episodic"] == 0
    assert len(store.list_learnings(kind="fact")) == 2
    audience = store.get("lrn_scalar_audience")
    assert audience is not None
    assert audience["user_id"] == "local"
    assert "西电学生" in audience["text"]


def test_forget_survives_remigration(tmp_path):
    store = ProfileStore(tmp_path / "memory.db")
    store.migrate_profile(_profile())
    veto = next(row for row in store.list_learnings(kind="veto") if "夸张营销" in row["text"])
    assert store.forget(veto["id"]) is True
    store.migrate_profile(_profile())
    assert store.get(veto["id"])["status"] == "forgotten"


def test_explicit_learns_are_not_capped_at_twenty(tmp_path):
    store = ProfileStore(tmp_path / "memory.db")
    for index in range(30):
        store.learn(f"偏好 {index}: 保留这条用户纠正", kind="correction")
    assert store.count_learnings("active") == 30


def test_remember_lesson_indexes_titles(tmp_path):
    from app.agent.memory import AgentMemory
    from app.agent.orchestrator import _remember_lesson

    memory = AgentMemory(LongTermMemory(tmp_path / "profile.json"))
    goal = "为大学生写一篇宿舍好物开箱"
    plan = Plan(
        goal=goal,
        steps=[PlanStep(action="generate_contents", status=StepStatus.SUCCEEDED)],
    )
    outputs = {
        "results": [
            {
                "title": "宿舍好物：插座收纳",
                "body": "开头先写断电再插排，别一上来就营销。",
                "topic": "宿舍好物",
            }
        ],
        "top_keywords": ["插座"],
    }
    _remember_lesson(memory, goal, plan, outputs, run_id="run_dorm_1")
    rows = memory.long_term.store.recall_episodic("写一篇和上周宿舍好物类似的")
    assert rows
    assert rows[0]["run_id"] == "run_dorm_1"
    assert "宿舍好物：插座收纳" in rows[0]["titles"]
    assert rows[0]["snippet"]
    # JSON 画像仍在，SQLite 才是可检索的正文索引。
    assert memory.long_term.get("episodic")


def test_consolidate_cursor_merges_near_duplicates(tmp_path):
    store = ProfileStore(tmp_path / "memory.db")
    store.learn("美妆选题要先看评论区再写标题", kind="procedure", lane="situational", topics=["美妆"])
    store.learn("美妆选题要先看评论区再写标题", kind="procedure", lane="situational", topics=["美妆"])
    # learn() 本身按 norm 去重，这里再塞一条几乎相同的行来触发巩固合并。
    store._save_learning(
        "lrn_near_dup",
        text="美妆选题要先看评论区再写标题啊",
        norm="different-on-purpose",
        kind="procedure",
        lane="situational",
        topics=["美妆"],
        confidence=0.4,
        pinned=False,
        status="active",
        source="dream",
        evidence="",
        user_id="local",
        created_at="2026-01-01T00:00:00+00:00",
        respect_forgotten=False,
    )
    result = store.consolidate_learnings()
    assert result["revision"] >= 1
    assert result["merged"] >= 1
    assert store.cursor()["revision"] >= 1
    active = [
        row
        for row in store.list_learnings(kind="procedure", status="active")
        if "评论区" in row["text"]
    ]
    assert len(active) == 1
