"""Tests for the memory-layer upgrade (docs/memory-upgrade-plan.md).

Covers:
- 阶段3a: 主题指纹去重(近似重复合并计数,不再各占位)
- 阶段3b: 倒排评分召回(语义近召回、主题不同过滤)
- 阶段4:  遗忘衰减(recency×hits×quality 加权,冷条目先淘汰)
- 阶段1:  反馈回路(_preferences_text 注入 + feedback API 写入通道)
- 阶段2:  自动事实提取(UserFacts schema + 按签名合并)
- 阶段5:  产物 TTL(housekeeping 清理 crawled_notes / images)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.agent.llm import MockLLMBackend
from app.agent.memory import LongTermMemory
from app.agent.mock_factories import build_mock_backend
from app.agent.schemas import UserFacts

client = TestClient(__import__("app.main", fromlist=["app"]).app)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- 阶段3a: 主题指纹去重 ----------


def test_near_duplicate_goals_merge_into_one_entry_with_hits():
    """4 条西电暑期实习近似重复 -> 合并为 1 条,hits 累计(不再各占 20 条位)。"""
    from app.agent.orchestrator import _dedup_by_goal

    goals = [
        "挖掘小红书上西电实习相关笔记生成3篇实习种草文案",
        "为西电学生生成3篇关于暑期实习的小红书分享文案",
        "为西电学生生成3篇暑期实习文案",
    ]
    entries = [
        {"goal": g, "signature": "", "results": 1, "updated_at": _now_iso(), "hits": 1}
        for g in goals
    ]
    out = _dedup_by_goal(entries, keep=20)
    assert len(out) == 1, out
    assert out[0]["hits"] == 3


def test_distinct_topics_stay_separate():
    from app.agent.orchestrator import _dedup_by_goal

    goals = ["生成关于苹果的文案", "生成关于汽车的文章", "生成关于美食的笔记"]
    entries = [
        {"goal": g, "signature": "", "results": 1, "updated_at": _now_iso(), "hits": 1}
        for g in goals
    ]
    out = _dedup_by_goal(entries, keep=20)
    assert len(out) == 3


# ---------- 阶段3b: 倒排评分召回 ----------


def test_recall_prefers_semantic_duplicates_and_filters_other_topic():
    from app.agent.orchestrator import _profile_text

    profile = {
        "audience": "西电学生",
        "episodic": [
            {"goal": "为西电学生生成3篇暑期实习文案", "steps_ok": True, "results": 3},
            {"goal": "挖掘小红书上西电实习相关笔记生成3篇实习种草文案", "steps_ok": True, "results": 2},
            {"goal": "为大学生生成护肤避雷文案", "steps_ok": True, "results": 1},
        ],
    }
    txt = _profile_text(profile, goal="为西电学生生成 3 篇暑期实习文案")
    assert "暑期实习" in txt          # 同主题近似措辞被召回
    assert "护肤" not in txt          # 主题不同被过滤


def test_recall_rejects_small_set_partial_overlap():
    """阶段3b+: 短 goal 小 token 集部分重叠不得召回。

    女生 与 研究生 去通用词后仅共享"生",overlap=0.5 曾越过 0.4 阈值误召回;
    小集合(<=2 token)必须完全覆盖才算相关。西电 vs 西电实习 仍应召回
    (完全覆盖,子集方向)。
    """
    from app.agent.orchestrator import _topic_overlap, _profile_text

    assert _topic_overlap("为女生生成文案", "为研究生写经验帖") < 0.4
    assert _topic_overlap("为西电学生写文案", "西电实习分享") >= 0.4

    profile = {
        "episodic": [
            {"goal": "为研究生写考研经验帖", "steps_ok": True, "results": 2},
        ],
    }
    txt = _profile_text(profile, goal="为女生生成3篇种草文案")
    assert "研究生" not in txt


def test_topic_overlap_ignores_single_char_on_large_sets():
    """CJK unigrams that do not form a shared bigram must not clear 0.4."""
    from app.agent.orchestrator import _topic_overlap

    assert _topic_overlap("甲乙丙丁戊己", "甲庚丙辛戊壬") == 0.0
    assert _topic_overlap("为西电学生写文案", "西电实习分享") >= 0.4


# ---------- 阶段4: 遗忘衰减 ----------


def test_trim_keeps_hot_fresh_entries_drops_cold_ones():
    """超出 keep 上限时,冷/低权重条目先淘汰,而非只留最新 keep 条。"""
    from app.agent.orchestrator import _dedup_by_goal

    cold = {
        "goal": "生成关于苹果的文案",
        "results": 1, "hits": 1,
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    hot = {
        "goal": "生成关于汽车的文章",
        "results": 1, "hits": 5,
        "updated_at": _now_iso(),
    }
    fresh = {
        "goal": "生成关于美食的笔记",
        "results": 1, "hits": 1,
        "updated_at": _now_iso(),
    }
    out = _dedup_by_goal([cold, hot, fresh], keep=2)
    assert len(out) == 2
    assert all("苹果" not in e["goal"] for e in out)  # 最冷条目被淘汰


# ---------- 阶段1: 反馈回路 ----------


def test_preferences_text_aggregates_feedback():
    from app.agent.orchestrator import _preferences_text

    profile = {
        "user_preferences": [
            {"goal_signature": "西电实习", "liked": True, "score": 5},
            {"goal_signature": "西电实习", "liked": True, "score": 4},
            {"goal_signature": "护肤", "liked": False, "score": 1},
        ]
    }
    txt = _preferences_text(profile)
    assert "User preferences (from their feedback on generated content)" in txt
    assert "liked=2" in txt and "avg_score=4.5" in txt


def test_feedback_endpoint_writes_profile(tmp_path, monkeypatch):
    """POST /agent/runs/{id}/feedback 写入 profile.user_preferences(反馈回路闭环)。"""
    from app.agent import audit
    from app.agent.memory import LongTermMemory
    from app.agent.run_manager import run_manager

    monkeypatch.setattr(audit, "AUDIT_LOG", tmp_path / "audit.log")
    # 端点内部 from app.agent.memory import LongTermMemory 在调用时解析 -> 换 tmp 实现。
    monkeypatch.setattr(
        "app.agent.memory.LongTermMemory",
        lambda *a, **k: LongTermMemory(tmp_path / "profile.json"),
    )
    monkeypatch.setattr(
        run_manager, "get", lambda run_id: {"goal": "为西电学生生成暑期实习文案"}
    )

    resp = client.post(
        "/agent/runs/fake123/feedback",
        json={"content_id": "西电实习文案标题", "liked": True, "score": 5, "note": "要这个风格"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    stored = LongTermMemory(tmp_path / "profile.json")
    prefs = stored.get("user_preferences")
    assert prefs and prefs[-1]["liked"] is True
    assert prefs[-1]["score"] == 5
    assert prefs[-1]["goal_signature"]  # 由 run 的 goal 归一化而来


# ---------- 阶段2: 自动事实提取 ----------


def test_extract_user_facts_with_mock_backend():
    from app.agent.orchestrator import _extract_user_facts

    backend = build_mock_backend()
    facts = asyncio.run(
        _extract_user_facts(backend, "为西电学生生成实习文案", {"results": []})
    )
    assert facts is not None
    assert isinstance(facts, UserFacts)
    assert facts.target_domains


def test_store_user_facts_merges_by_signature(tmp_path):
    from app.agent.orchestrator import _store_user_facts

    memory = LongTermMemory(tmp_path / "profile.json")
    _store_user_facts(
        memory,
        "为西电学生生成暑期实习文案",
        UserFacts(target_domains=["西电实习"], preferred_tone="真实克制",
                  preferred_structure="", avoid_topics=["夸大"]),
    )
    # 措辞不同但归一化签名相同(空格/标点不影响签名)-> 应合并到同一 key。
    _store_user_facts(
        memory,
        "为西电学生生成  暑期实习 文案",
        UserFacts(target_domains=["考研经验"], preferred_tone="",
                  preferred_structure="清单式", avoid_topics=[]),
    )
    facts = memory.get("user_facts")
    assert len(facts) == 1  # 同签名合并
    entry = next(iter(facts.values()))
    assert "西电实习" in entry["target_domains"]  # 历史事实不丢
    assert "考研经验" in entry["target_domains"]  # 新事实叠加
    assert entry["preferred_tone"] == "真实克制"   # 非空优先
    assert entry["preferred_structure"] == "清单式"


def test_profile_text_includes_user_facts():
    from app.agent.orchestrator import _profile_text

    profile = {
        "audience": "西电学生",
        "user_facts": {
            "sig": {"target_domains": ["西电实习"], "preferred_tone": "真实克制",
                    "preferred_structure": "", "avoid_topics": ["夸大"]}
        },
    }
    txt = _profile_text(profile)
    assert "Known facts about the user (extracted from past runs)" in txt
    assert "真实克制" in txt


# ---------- 阶段5: 产物 TTL ----------


def test_purge_artifact_files_removes_stale_crawled_notes(tmp_path):
    from app.agent.housekeeping import _purge_artifact_files

    d = tmp_path / "raw"
    d.mkdir()
    old = d / "crawled_notes_old.json"
    fresh = d / "crawled_notes_fresh.json"
    state = d / "xhs_state.json"  # 凭证/配置文件绝不能被清
    for p in (old, fresh, state):
        p.write_text("{}", encoding="utf-8")
    now = datetime.now().timestamp()
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))

    removed = _purge_artifact_files(d, "crawled_notes_*.json", retention_days=30)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert state.exists()  # 非 crawled_notes 不动


def test_purge_artifact_files_removes_stale_images(tmp_path):
    from app.agent.housekeeping import _purge_artifact_files

    d = tmp_path / "images"
    d.mkdir()
    old = d / "old.png"
    fresh = d / "fresh.jpg"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    now = datetime.now().timestamp()
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))

    removed = _purge_artifact_files(d, "*", retention_days=30)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
