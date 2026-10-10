"""End-to-end demo of the upgraded memory layer against a TEMP profile.

Shows, with the deterministic mock backend, how the five upgrade phases interact:
  阶段1 反馈回路 -> user_preferences 写入与注入
  阶段2 事实提取 -> run 尾自动抽取 user_facts
  阶段3a 去重    -> 近似重复 goal 合并计数(hits),不再各占 20 条位
  阶段3b 召回    -> 同主题措辞异被召回,主题不同被过滤
  阶段4 衰减     -> 冷条目降权(权重随 age 衰减)

Uses a temp profile so the real data/agent/profile.json is never touched.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from app.agent.memory import AgentMemory, LongTermMemory
from app.agent.mock_factories import build_mock_backend, build_mock_registry
from app.agent.orchestrator import XHSAgent, _goal_signature, _profile_text, _remember_lesson
from app.agent.schemas import UserFacts

GOAL_A = "挖掘小红书上西电实习相关笔记,为西电学生生成3篇实习种草文案"
GOAL_B = "为西电学生生成 3 篇暑期实习文案"  # 近似重复:不同措辞,同主题
GOAL_C = "为西电研究生生成3篇考研避坑文案"  # 不同主题(考研 vs 实习)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="memory_demo_")) / "profile.json"
    memory = AgentMemory(LongTermMemory(tmp))
    agent = XHSAgent(backend=build_mock_backend(), registry=build_mock_registry())

    print("=== run 1: 西电实习 goal ===")
    r1 = await agent.run(GOAL_A, memory=memory)
    print(f"  status={r1.plan.is_complete} results={len(r1.outputs.get('results', []))}")

    print("=== run 2: 近似重复 goal(不同措辞,同主题)===")
    r2 = await agent.run(GOAL_B, memory=memory)
    episodic = memory.long_term.get("episodic") or []
    print(f"  episodic entries={len(episodic)} (期望 1:近似重复合并计数)")
    if episodic:
        print(f"  hits={episodic[0].get('hits')} goal='{episodic[0].get('goal','')[:20]}...'")

    print("=== run 3: 不同主题 goal(考研)应分开 ===")
    await agent.run(GOAL_C, memory=memory)
    episodic = memory.long_term.get("episodic") or []
    print(f"  episodic entries={len(episodic)} (期望 2:实习1条 + 考研1条)")

    print("=== 阶段2 事实提取: user_facts ===")
    facts = memory.long_term.get("user_facts") or {}
    for sig, f in facts.items():
        print(f"  sig={sig[:16]}... domains={f.get('target_domains')}")

    print("=== 阶段1 反馈回路: 写入偏好并注入 ===")
    from app.agent.orchestrator import _store_user_facts
    _store_user_facts(
        memory.long_term, GOAL_B,
        UserFacts(target_domains=["西电实习"], preferred_tone="真实克制",
                  preferred_structure="清单式", avoid_topics=["夸大"]),
    )
    prefs = list(memory.long_term.get("user_preferences") or [])
    prefs.append({
        "content_id": "暑期实习避坑清单",
        "goal_signature": _goal_signature(GOAL_B),
        "liked": True, "score": 5, "note": "要避坑清单式", "ts": "now",
    })
    memory.long_term.set("user_preferences", prefs)

    print("=== 阶段3b 召回 + 注入: 下一次 planner 会看到什么 ===")
    txt = _profile_text(memory.long_term.data, GOAL_B)
    print(txt)


if __name__ == "__main__":
    asyncio.run(main())
