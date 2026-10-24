"""XHSAgent: the top-level orchestration object.

It wires a Planner, Reflector, Executor, ToolRegistry, Memory, and Tracer into a
single runnable Agent. Given a goal it plans, executes, reflects, and returns a
verifiable result (plan, outputs, tool-call counts, trace file).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.agent.executor import Executor, _step_focus
from app.agent.focus import _extract_focus
from app.agent.evaluator import Evaluator, Optimizer, QualityGate
from app.agent.guardrails import Guardrails
from app.agent.budget import Budget
from app.agent.checkpoint import StepCheckpointer
from app.agent.consolidation import (
    _insights_text,
    consolidate as consolidate_quietly,
    submit_consolidation,
)
from app.agent.llm import LLMBackend, get_backend
from app.agent.memory import AgentMemory, LongTermMemory
from app.agent.planner import Planner, Reflector
from app.agent.schemas import Plan, PlanStep, StepStatus, UserFacts
from app.agent.tools import ToolRegistry, build_default_registry
from app.agent.tracing import AgentTracer
from app.core.config import settings

logger = logging.getLogger("agent.orchestrator")

MAX_LESSONS = 20
MAX_PROCEDURAL = 20
PROFILE_RECALL_TOP_K = 5
RECALL_MIN_SCORE = 0.4        # 阶段3b: 召回的最低主题相关度(overlap coefficient)
FP_MIN_OVERLAP = 0.85         # 阶段3a: 近似重复去重的指纹重合阈值(高重合才并)
FP_MIN_LEN = 3                # 阶段3a: 指纹过短不参与近似合并(防泛化误并)
MAX_PREFERENCES = 200         # 阶段1: user_preferences 有界上限
# 知识注入 v2 (调研规划 M1): planner 注入预算(约 400 CJK token, Context Rot:
# 聚焦上下文优于全量塞入)与查询变体上限(免 LLM 组合式扩展, CompCQR 思想)。
KNOWLEDGE_CONTEXT_BUDGET_CHARS = 800
KNOWLEDGE_MAX_VARIANTS = 3


@dataclass
class AgentResult:
    run_id: str
    goal: str
    plan: Plan
    outputs: dict[str, Any]
    tool_calls: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    stopped_reason: str = ""
    trace_path: str = ""


class XHSAgent:
    def __init__(
        self,
        backend: Optional[LLMBackend] = None,
        registry: Optional[ToolRegistry] = None,
        guardrails: Optional[Guardrails] = None,
    ) -> None:
        self.backend = backend or get_backend()
        self.registry = registry or build_default_registry()
        self.planner = Planner(self.backend, self.registry.specs())
        self.reflector = Reflector(self.backend)
        self.guardrails = guardrails

    async def run(
        self,
        goal: str,
        *,
        audience: Optional[str] = None,
        tone: Optional[str] = None,
        memory: Optional[AgentMemory] = None,
        run_id: Optional[str] = None,
        event_sink: Optional[Callable[[str, dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        approval_handler: Optional[
            Callable[[str, dict[str, Any]], Awaitable[bool]]
        ] = None,
        checkpointer=None,
        guardrails: Optional[Guardrails] = None,
        consolidation_pool=None,
    ) -> AgentResult:
        tracer = AgentTracer.new(run_id=run_id, event_sink=event_sink)
        tracer.event("goal", goal=goal)
        memory = memory or AgentMemory(LongTermMemory())
        store_token = _bind_run_store(memory)
        try:
            return await self._run_bound(
                goal,
                audience=audience,
                tone=tone,
                memory=memory,
                tracer=tracer,
                stop_check=stop_check,
                approval_handler=approval_handler,
                checkpointer=checkpointer,
                guardrails=guardrails,
                consolidation_pool=consolidation_pool,
            )
        finally:
            _reset_run_store(store_token)

    async def _run_bound(
        self,
        goal: str,
        *,
        audience: Optional[str] = None,
        tone: Optional[str] = None,
        memory: AgentMemory,
        tracer: AgentTracer,
        stop_check: Optional[Callable[[], bool]] = None,
        approval_handler: Optional[
            Callable[[str, dict[str, Any]], Awaitable[bool]]
        ] = None,
        checkpointer=None,
        guardrails: Optional[Guardrails] = None,
        consolidation_pool=None,
    ) -> AgentResult:
        # Long-term memory: persist the learned audience/tone profile.
        if audience:
            memory.long_term.update({"audience": audience})
        if tone:
            memory.long_term.update({"tone": tone})
        profile = memory.long_term.data

        guard = guardrails or self.guardrails
        if guard:
            goal_check = guard.check_goal(goal)
            if not goal_check.allowed:
                raise ValueError(
                    "Goal blocked by guardrails: " + goal_check.reason
                )

        executor = Executor(
            self.registry,
            self.reflector,
            self.planner,
            max_retries_per_step=settings.agent_max_retries,
            tracer=tracer,
            quality_gate=QualityGate(Evaluator(self.backend), Optimizer(self.backend)),
            budget=Budget.from_settings(settings),
            get_usage=lambda: self.backend.usage(),
            stop_check=stop_check,
            approval_handler=approval_handler,
            checkpointer=checkpointer,
            guardrails=guard,
        )

        base_usage: dict[str, Any] = {}
        resumed = False
        if checkpointer:
            saved = checkpointer.load(tracer.run_id)
            if saved:
                plan = Plan.model_validate(saved["plan"])
                memory.restore(saved.get("memory", {}).get("steps", []))
                saved_meta = saved.get("memory", {}).get("meta")
                if isinstance(saved_meta, dict):
                    memory.meta = saved_meta
                base_usage = saved.get("usage") or {}
                tracer.event(
                    "resume_from_checkpoint",
                    steps=[s.action for s in plan.steps],
                    done=sum(
                        1 for s in plan.steps if s.status == StepStatus.SUCCEEDED
                    ),
                )
                resumed = True
        planner_text, execute_profile, knowledge_text, pack_meta = _plan_memory(
            profile, goal, memory.long_term
        )
        if pack_meta:
            # Metadata only. The pack body is ephemeral prompt context.
            tracer.event("memory_pack", **pack_meta)
        if not resumed:
            if settings.agent_context_builder_enabled:
                from app.agent.context_parts import build_planner_parts
                from app.agent.planner import PLANNER_SYSTEM

                parts = build_planner_parts(
                    profile,
                    goal,
                    tools_full=self.planner._tools_text(),
                    tools_concise=self.planner._tools_concise(),
                    system_text=PLANNER_SYSTEM,
                    store=getattr(memory.long_term, "store", None),
                )
                part_knowledge = next((p.text for p in parts if p.key == "knowledge"), "")
                if part_knowledge:
                    knowledge_text = part_knowledge
                plan = await self.planner.plan(
                    goal,
                    execute_profile,
                    context_parts=parts,
                    tracer=tracer,
                    run_id=tracer.run_id,
                )
            else:
                plan = await self.planner.plan(goal, planner_text)
            if knowledge_text:
                tracer.event("knowledge_context", chars=len(knowledge_text))
            # M2 结构化强制(Read-Gate 闭环): prompt 措辞对抗已证不可靠(两轮实战
            # knowledge_search 零调用), 改为确定性注入——调用级(链首插检索步)
            # + 消费级(生成步骤 params.knowledge_hints)。mock 后端跳过, 保持
            # 离线测试的确定性 fixture 不被改写(与 R3 mock 不写库同一先例)。
            if getattr(self.backend, "name", "") != "mock":
                injected = _inject_knowledge_steps(plan, knowledge_text, goal)
                if injected:
                    tracer.event("knowledge_injected", **injected)
            tracer.event("plan", steps=[s.action for s in plan.steps])
        executor.base_usage = base_usage
        executor.profile_dict = profile
        plan = await executor.execute(goal, execute_profile, plan, memory)

        if plan.is_complete and checkpointer:
            checkpointer.clear(tracer.run_id)

        outputs = _collect_outputs(
            memory,
            focus_keywords=_step_focus(plan, "generate_contents") or _extract_focus(goal),
            strategy=settings.agent_collect_strategy,
            top_n=settings.agent_top_n,
        )
        _remember_lesson(memory, goal, plan, outputs, run_id=tracer.run_id)
        # R3 (memory-rag-selection): run 产物自动入 RAG 知识库 — 分析报告 +
        # 生成文案 + 爬取笔记(工具侧已落盘, 由 _persist_crawl_result 完成)。
        # mock 后端(测试/离线演示)不写入真实知识库; 失败不致命。
        if getattr(self.backend, "name", "") != "mock":
            try:
                _ingest_outputs_to_knowledge(goal, outputs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("knowledge ingest skipped (non-fatal): %s", exc)
        # 阶三(dreaming): run 结束后投递低优先级后台巩固任务,提炼可迁移
        # procedural 洞察;失败不致命,不阻塞返回。consolidation_pool 由调用方
        # (run_manager 的工作线程池)提供;CLI demo 不传则同步跑一次并容忍失败。
        succeeded_actions = [
            s.action for s in plan.steps if s.status == StepStatus.SUCCEEDED
        ]
        if consolidation_pool is not None:
            # 巩固在别的线程+loop 跑, 不能共享主 run 的 openai 客户端(跨 loop 复用
            # 连接 + 主 run 结束即 aclose 会打断它): 真实后端时给它一份全新的。
            consol_backend = self.backend
            if getattr(self.backend, "name", "") in ("openai", "openrouter"):
                consol_backend = get_backend()
            submit_consolidation(
                consolidation_pool,
                consol_backend,
                memory.long_term,
                goal,
                succeeded_actions,
                outputs,
                tracer=tracer,
            )
        else:
            await consolidate_quietly(
                self.backend,
                memory.long_term,
                goal,
                succeeded_actions,
                outputs,
                tracer=tracer,
            )
        # 阶段2: run 尾自动抽取用户事实(一次 LLM 调用,失败不致命)。
        _store_user_facts(
            memory.long_term,
            goal,
            await _extract_user_facts(self.backend, goal, outputs),
        )
        usage = self.backend.usage()
        tracer.event("done", status="ok" if plan.is_complete else "incomplete", usage=usage)
        stopped = _budget_stop_reason(tracer)
        if stopped:
            outputs["stopped_reason"] = stopped
        trace_path = tracer.save(outputs, usage=usage, stopped_reason=stopped)

        return AgentResult(
            run_id=tracer.run_id,
            goal=goal,
            plan=plan,
            outputs=outputs,
            tool_calls=self.registry.total_calls(),
            usage=usage,
            stopped_reason=stopped,
            trace_path=trace_path,
        )


def _budget_stop_reason(tracer) -> str:
    for e in tracer.events:
        if e.get("kind") == "budget_stop":
            return e.get("reason", "budget exceeded")
    return ""


def _bind_run_store(memory: AgentMemory):
    """Point memory tools at this run's SQLite store. Failure leaves the run usable."""
    try:
        from app.agent.memory_store import bind_store

        return bind_store(memory.long_term.store)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory store bind skipped: %s", exc)
        return None


def _reset_run_store(token) -> None:
    if token is None:
        return
    try:
        from app.agent.memory_store import reset_store

        reset_store(token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory store reset skipped: %s", exc)


def _plan_memory(profile: dict[str, Any], goal: str, long_term) -> tuple[str, str, str, dict[str, Any]]:
    """Return ``(planner_prompt, execute_profile, knowledge_text, pack_meta)``.

    The pack is ephemeral. ``pack_meta`` never includes the pack body, and the
    execute/reflector profile is the pack without the knowledge lane so a
    document digest cannot crowd out veto lines or land in a checkpoint.
    """
    knowledge = _knowledge_context(goal)
    legacy = _profile_text(profile, goal)
    meta: dict[str, Any] = {
        "enabled": bool(settings.agent_memory_pack_enabled),
        "unavailable": False,
        "revision": 0,
        "chars": 0,
        "dropped": 0,
    }
    if not settings.agent_memory_pack_enabled:
        meta["chars"] = len(legacy)
        return legacy + (knowledge or ""), legacy, knowledge, meta
    try:
        from app.agent.memory_pack import build_pack

        pack = build_pack(
            goal,
            store=long_term.store,
            profile=profile,
            knowledge_text="",
        )
        meta.update(
            unavailable=bool(pack.unavailable),
            revision=int(pack.revision),
            chars=len(pack.text),
            dropped=len(pack.dropped),
        )
        return pack.text + (knowledge or ""), pack.text, knowledge, meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory pack failed: %s", exc)
        meta["unavailable"] = True
        banner = (
            '<memory-context user_id="local" ephemeral="true" unavailable="true">\n'
            "Memory pack unavailable this turn. Do not claim you remember nothing; "
            "the profile below still applies.\n"
            f"{legacy}\n</memory-context>"
        )
        meta["chars"] = len(banner)
        return banner + (knowledge or ""), banner, knowledge, meta


def _query_variants(goal: str) -> list[str]:
    """确定性查询变体(免 LLM, CompCQR 组合式改写思想): goal 原文 + focus
    关键词组合。Q2D-Web(arXiv:2609.08887)实证 Agent 改写查询分布异于人类,
    单一 goal 查询漏检高; 变体经 search_multi RRF 融合互补, 失败零成本。
    """
    variants = [(goal or "").strip()]
    focus = [f for f in _extract_focus(goal) if f]
    if focus:
        joined = " ".join(focus)
        if joined and joined not in variants:
            variants.append(joined)
    return variants[:KNOWLEDGE_MAX_VARIANTS]


def _knowledge_context(goal: str, top_k: int = 4) -> str:
    """R4 v2: 规划前知识注入 —— 规模判别 + 组合式多查询检索 + 注入预算。

    - 小库(count <= knowledge_digest_max_docs): 全库 digest 直注(Anthropic
      官方结论: 知识库 <20 万 token 时整库入 prompt 优于检索)。
    - 大库: _query_variants 多变体 RRF 检索。
    - 条目统一 K1..Kn 编号(Read-Gate: 让"检索了"可验证为"读了"), 总量受
      KNOWLEDGE_CONTEXT_BUDGET_CHARS 预算约束。
    无知识库/检索失败时返回空串(不致命, 规划照常)。
    """
    try:
        from app.agent.knowledge import get_knowledge_store

        store = get_knowledge_store()
        if store.count() <= settings.knowledge_digest_max_docs:
            text = _knowledge_digest(store)
        else:
            text = _knowledge_retrieval_text(store, _query_variants(goal), top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge context skipped (non-fatal): %s", exc)
        return ""
    return text


def _knowledge_digest(store) -> str:
    """小库整库 digest: 全部文档紧凑列表, K 编号 + 预算封顶。空库返回空串。"""
    docs = store.list_documents()
    if not docs:
        return ""
    budget = settings.knowledge_context_budget_chars
    lines = ["Knowledge base digest (cite as K1..Kn when used):"]
    used = 0
    for i, doc in enumerate(store.list_documents(), start=1):
        line = (
            f"- [K{i}|{doc.get('source')}] {doc.get('title', '')}: "
            f"{doc.get('snippet', '')}"
        )
        if used + len(line) > budget and len(lines) > 1:
            break
        lines.append(line)
        used += len(line)
    return "\n" + "\n".join(lines)


def _knowledge_retrieval_text(store, variants: list[str], top_k: int) -> str:
    """大库路径: 多变体 RRF 检索 -> K 编号预算化注入。"""
    hits = store.search_multi(variants, top_k=top_k)
    if not hits:
        return ""
    budget = settings.knowledge_context_budget_chars
    lines = ["Relevant knowledge from past runs (cite as K1..Kn when used):"]
    used = 0
    for i, h in enumerate(hits, start=1):
        text = h.get("text", "")[:220]
        line = f"- [K{i}|{h.get('source')}] {h.get('title', '')}: {text}"
        if used + len(line) > budget and len(lines) > 1:
            break
        lines.append(line)
        used += len(line)
    return "\n" + "\n".join(lines)


def knowledge_for_tokens(goal: str, max_tokens: int) -> str:
    """Knowledge block sized to the tokens still left in the planner budget.

    The legacy ``_knowledge_context`` char budget is unchanged. This path is
    only used when ContextBuilder is assembling a planner prompt.
    """
    if max_tokens < 40:
        return ""
    try:
        from app.agent.context_builder import count_tokens
        from app.agent.knowledge import get_knowledge_store

        store = get_knowledge_store()
        if store.count() <= settings.knowledge_digest_max_docs:
            header = "Knowledge base digest (cite as K1..Kn when used):"
            docs = list(store.list_documents())
            rows = []
            for index, doc in enumerate(docs, start=1):
                rows.append(
                    f"- [K{index}|{doc.get('source')}] {doc.get('title', '')}: "
                    f"{doc.get('snippet', '')}"
                )
        else:
            header = "Relevant knowledge from past runs (cite as K1..Kn when used):"
            hits = store.search_multi(_query_variants(goal), top_k=8)
            rows = []
            for index, hit in enumerate(hits, start=1):
                rows.append(
                    f"- [K{index}|{hit.get('source')}] {hit.get('title', '')}: "
                    f"{str(hit.get('text', ''))[:220]}"
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge context skipped (non-fatal): %s", exc)
        return ""
    if not rows:
        return ""
    lines = [header]
    used = count_tokens(header)
    for row in rows:
        cost = count_tokens(row)
        if used + cost > max_tokens and len(lines) > 1:
            break
        lines.append(row)
        used += cost
    if len(lines) == 1:
        return ""
    return "\n" + "\n".join(lines)


def _knowledge_hints(knowledge_text: str) -> list[str]:
    """从 _knowledge_context 产出文本解析 K 线索行('- [K1|src] title: text')。"""
    hints = []
    for line in (knowledge_text or "").splitlines():
        line = line.strip()
        if line.startswith("- [K") and "|" in line:
            hints.append(line[2:200].strip())
    return hints[:4]


def _inject_knowledge_steps(plan: Plan, knowledge_text: str, goal: str) -> dict:
    """结构化强制注入(仅真实后端; mock fixture 不改写)。返回注入统计。

    - 调用级: K 知识可用但计划无 knowledge_search 时, 链首插入检索步, 让
      "检索"成为可观测/可评估的真实工具调用(此前两轮实战 planner 均未自发调用)。
    - 消费级: generate_topics / generate_contents 步骤 params 注入
      knowledge_hints, 服务层 prompt 直接扎根 K 线索(Read-Gate 的"读了")。
    幂等: 已有 knowledge_search 步 / 已带 hints 的步骤不重复注入。
    """
    hints = _knowledge_hints(knowledge_text)
    if not hints:
        return {}
    injected = {"search_step": 0, "hint_steps": 0}
    if "knowledge_search" not in [s.action for s in plan.steps]:
        plan.steps.insert(
            0,
            PlanStep(
                action="knowledge_search",
                params={"query": goal, "top_k": 4},
                rationale=(
                    "Auto-inserted (Read-Gate): the profile carries K-tagged "
                    "knowledge (K1..Kn); retrieve fresh hits so later steps can "
                    "ground in $latest.knowledge_search."
                ),
            ),
        )
        injected["search_step"] = 1
    for step in plan.steps:
        if step.action in ("generate_topics", "generate_contents"):
            if not step.params.get("knowledge_hints"):
                step.params["knowledge_hints"] = list(hints)
                injected["hint_steps"] += 1
    return injected


def _ingest_outputs_to_knowledge(goal: str, outputs: dict) -> None:
    """R3: 把本次 run 的分析摘要与生成文案写入 RAG 知识库, 支撑后续知识问答。

    doc_id 由 goal+内容哈希得出 —— checkpoint resume 重放/重复 run 幂等不重复入库。
    """
    import hashlib

    from app.agent.knowledge import get_knowledge_store

    store = get_knowledge_store()
    analysis = outputs.get("analysis_summary")
    if analysis:
        text = str(analysis)
        store.add(
            text,
            title=f"分析摘要: {goal[:40]}",
            source="analysis",
            metadata={"goal": goal},
            doc_id=hashlib.sha1(f"analysis:{goal}:{text[:200]}".encode("utf-8")).hexdigest()[:12],
        )
    for item in outputs.get("results", []) or []:
        if isinstance(item, dict) and item.get("body"):
            body = " ".join(
                str(part)
                for part in (
                    item.get("title", ""),
                    item.get("body", ""),
                    " ".join(item.get("hashtags", []) or []),
                )
                if part
            )
            store.add(
                body,
                title=str(item.get("title") or "")[:60],
                source="output",
                metadata={"goal": goal},
                doc_id=hashlib.sha1(f"output:{goal}:{body[:200]}".encode("utf-8")).hexdigest()[:12],
            )


def _remember_lesson(
    memory: AgentMemory,
    goal: str,
    plan: Plan,
    outputs: dict,
    run_id: str = "",
) -> None:
    """Persist what this run learned so future plans can reuse successful patterns.

    plan 12.1: dedup lessons by a normalized goal signature, split long-term memory
    into episodic / procedural buckets (plus the existing semantic audience/tone),
    and recall by keyword instead of a naive sliding window.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    lesson = {
        "goal": goal,
        "signature": _goal_signature(goal),
        "steps_ok": plan.is_complete,
        "results": len(outputs.get("results", [])),
        "quality": outputs.get("quality_report"),
        "updated_at": now_iso,
        "hits": 1,
    }
    episodic = _dedup_by_goal(
        (memory.long_term.get("episodic", []) or []) + [lesson]
    )
    writes: dict[str, Any] = {
        "episodic": episodic,
        # Mirror to legacy `lessons` key so older readers still work.
        "lessons": episodic,
    }

    if plan.is_complete:
        actions = [s.action for s in plan.steps if s.status == StepStatus.SUCCEEDED]
        proc = {
            "goal": goal,
            "signature": _goal_signature(goal),
            "actions": actions,
            "results": len(outputs.get("results", [])),
            "updated_at": now_iso,
            "hits": 1,
        }
        procedural = _dedup_by_goal(
            (memory.long_term.get("procedural", []) or []) + [proc]
        )
        writes["procedural"] = procedural[-MAX_PROCEDURAL:]

    # 单次落盘:episodic/lessons/procedural 一批写,不再每键一次全量重写。
    memory.long_term.set_many(writes)
    _index_episodic_run(memory, goal, plan, outputs, run_id, episodic)


def _episode_assets(outputs: dict) -> tuple[list[str], list[str], str]:
    """Titles, topics, and a short snippet from this run's content assets."""
    titles: list[str] = []
    topics: list[str] = []
    snippet = ""
    for item in outputs.get("results") or []:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            if title:
                titles.append(title[:120])
            topic = item.get("topic") or item.get("topic_title")
            if topic:
                topics.append(str(topic)[:80])
            body = str(item.get("body") or item.get("content") or "")
            if body and not snippet:
                snippet = body[:180]
        elif isinstance(item, str) and item.strip() and not snippet:
            snippet = item.strip()[:180]
    for key in ("top_keywords", "top_tags", "title_patterns"):
        for value in outputs.get(key) or []:
            if isinstance(value, str) and value.strip():
                topics.append(value.strip()[:40])
    deduped_topics: list[str] = []
    for topic in topics:
        if topic not in deduped_topics:
            deduped_topics.append(topic)
    return titles[:12], deduped_topics[:16], snippet


def _index_episodic_run(
    memory: AgentMemory,
    goal: str,
    plan: Plan,
    outputs: dict,
    run_id: str,
    episodic: list[dict[str, Any]],
) -> None:
    """Index this run's goal plus any titles/topics. Failure does not fail the run."""
    try:
        signature = _goal_signature(goal)
        stored = next(
            (
                entry
                for entry in reversed(episodic)
                if _goal_signature(entry.get("goal", "")) == signature
            ),
            None,
        )
        titles, topics, snippet = _episode_assets(outputs)
        memory.long_term.store.upsert_episodic(
            run_id=run_id or "",
            goal=goal,
            signature=signature,
            status="ok" if plan.is_complete else "incomplete",
            titles=titles,
            topics=topics,
            snippet=snippet,
            result_count=len(outputs.get("results") or []),
            quality=outputs.get("quality_report"),
            hits=int((stored or {}).get("hits") or 1),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("episodic index skipped: %s", exc)


def _profile_sections(profile: dict[str, Any], goal: str = "") -> dict[str, str]:
    """Named blocks joined by ``_profile_text`` (legacy order preserved)."""
    if not profile:
        return {"_empty": "(no profile yet - ask for or infer the target user)"}
    sections: dict[str, str] = {}
    kv = []
    for key, value in profile.items():
        if key in (
            "lessons",
            "episodic",
            "procedural",
            "semantic",
            "user_facts",
            "user_preferences",
            "procedural_insights",
        ):
            continue
        kv.append(f"{key}: {value}")
    if kv:
        sections["kv"] = "\n".join(kv)

    pref_text = _preferences_text(profile)
    if pref_text:
        sections["prefs"] = pref_text
    facts_text = _facts_text(profile)
    if facts_text:
        sections["facts"] = facts_text

    insights_text = _insights_text(profile)
    if insights_text:
        sections["insights"] = insights_text

    episodic = profile.get("episodic") or profile.get("lessons") or []
    recalled = _profile_recall(goal, episodic)
    if recalled:
        lines = ["Relevant past experiences (reuse what worked, avoid what failed):"]
        for lesson in recalled:
            quality = lesson.get("quality")
            qs = f" quality={quality}" if quality else ""
            hits = lesson.get("hits")
            hs = f" hits={hits}" if hits else ""
            lines.append(
                f"- goal='{lesson.get('goal','')}' ok={lesson.get('steps_ok')} "
                f"results={lesson.get('results')}{hs}{qs}"
            )
        sections["episodic"] = "\n".join(lines)

    procedural = profile.get("procedural") or []
    if procedural:
        rel_proc = [
            item
            for item in procedural
            if not goal or _topic_overlap(goal, item.get("goal", "")) >= RECALL_MIN_SCORE
        ]
        if rel_proc:
            plines = ["Successful tool chains to follow:"]
            for item in rel_proc[-3:]:
                plines.append(
                    f"- goal='{item.get('goal','')}' steps={item.get('actions', [])}"
                )
            sections["procedural"] = "\n".join(plines)
    return sections


def _profile_text(profile: dict[str, Any], goal: str = "") -> str:
    sections = _profile_sections(profile, goal)
    if not profile:
        return sections.get("_empty", "(no profile yet - ask for or infer the target user)")
    parts = [
        sections[key]
        for key in ("kv", "prefs", "facts", "insights", "episodic", "procedural")
        if sections.get(key)
    ]
    return "\n".join(parts) or "(no profile yet - ask for or infer the target user)"


def _goal_signature(goal: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", (goal or "").lower())


def _tokens(text: str) -> set[str]:
    """召回 token：ASCII 词 + CJK 双字。单字不再单独计分，避免大集合单字虚高。"""
    text = (text or "").lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            grams.add(run)
        else:
            grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return words | grams


def _strip_generic(goal: str) -> str:
    """\u53bb\u6389 goal \u4e2d\u7684\u901a\u7528\u52a8\u4f5c/\u91cf\u8bcd/\u5e73\u53f0\u8bcd,\u7559\u4e0b\u4e3b\u9898\u57df\u5b57\u7b26\u3002"""
    sig = _goal_signature(goal)
    if not sig:
        return ""
    for term in _GENERIC_GOAL_TERMS:
        sig = sig.replace(term, "")
    return sig


def _topic_overlap(a: str, b: str) -> float:
    """overlap coefficient |A∩B| / min(|A|,|B|) on bigram tokens.

    去通用词后计分，避免「为/生成/文案」虚高。CJK 用双字而不是单字：
    大集合只共享「生」「教」这类单字时不再越过 0.4。小集合（<=2 个 token）
    仍要求完全覆盖，保住「女生 ≠ 研究生」。
    """
    sa, sb = _tokens(_strip_generic(a)), _tokens(_strip_generic(b))
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    # Lone CJK characters are not evidence unless the smaller side is covered
    # entirely by them (a 1-character topic). Shared bigrams/words count.
    strong = {token for token in inter if len(token) >= 2 or token.isascii()}
    if not strong:
        smaller = sa if len(sa) <= len(sb) else sb
        if smaller and smaller <= inter and min(len(sa), len(sb)) <= 2:
            return 1.0
        return 0.0
    overlap = len(strong) / min(len(sa), len(sb))
    if min(len(sa), len(sb)) <= 2:
        return overlap if overlap >= 1.0 else 0.0
    return overlap


def _profile_recall(
    goal: str, episodic: list[dict[str, Any]], top_k: int = PROFILE_RECALL_TOP_K
) -> list[dict[str, Any]]:
    """\u9636\u6bb53b: \u5012\u6392\u8bc4\u5206\u53ec\u56de(\u4e3b\u9898\u76f8\u5173\u5ea6),\u8fc7\u6ee4\u4f4e\u76f8\u5173,\u53d6 top-k\u3002

    \u65e0 goal \u65f6\u9000\u5316\u4e3a\u5168\u91cf(\u4f9b\u65e0\u76ee\u6807\u4e0a\u4e0b\u6587\u4f7f\u7528)\u3002
    """
    if not goal:
        return list(episodic)
    scored = []
    for lesson in episodic:
        s = _topic_overlap(goal, lesson.get("goal", ""))
        if s >= RECALL_MIN_SCORE:
            scored.append((s, lesson))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [lesson for _, lesson in scored[:top_k]]


_GENERIC_GOAL_TERMS = (
    "\u751f\u6210", "\u521b\u4f5c", "\u5236\u4f5c", "\u64b0\u5199", "\u5199", "\u505a", "\u5206\u6790", "\u722c", "\u91c7\u96c6", "\u6316\u6398",
    "\u7814\u7a76", "\u8c03\u7814", "\u63a2\u7d22", "\u5c0f\u7ea2\u4e66", "\u7b14\u8bb0", "\u6587\u6848", "\u5185\u5bb9", "\u79cd\u8349", "\u5206\u4eab",
    "\u53d1\u5e03", "\u63a8\u8350", "\u4e3a", "\u7ed9", "\u5e2e", "\u7bc7", "\u6761", "\u4e2a", "\u4efd", "\u5f20", "\u7684", "\u4e86",
    "\u548c", "\u4e0e", "\u53ca", "\u5e76", "\u5173\u4e8e", "\u76f8\u5173", "\u9488\u5bf9", "\u9762\u5411", "\u7528\u6237", "\u5b66\u751f",
    "\u7c89\u4e1d", "\u8bfb\u8005", "\u89c6\u9891", "\u56fe\u6587", "\u6a21\u677f", "\u793a\u4f8b", "\u8981\u6c42", "\u9700\u8981",
    "\u4e0a", "\u4e2d", "\u4e0b", "\u91cc", "\u5185",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
)


def _topic_fingerprint(goal: str) -> str:
    """\u9636\u6bb53a: \u53bb\u901a\u7528\u8bcd\u540e\u7684\u6392\u5e8f\u53bb\u91cd\u5b57\u7b26\u96c6,\u8fd1\u4f3c\u91cd\u590d\u7684\u5f52\u4e00\u5316 key\u3002"""
    return "".join(sorted(set(_strip_generic(goal))))


def _fingerprint_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / min(len(sa), len(sb))


def _near_duplicate(fp_a: str, fp_b: str) -> bool:
    """阶段3a: 近似重复判定——一方字符集是另一方子集,或高重合。

    子集=近重复以插入/删减词方式差异(如 西电实习 vs 西电暑期实习);
    高重合=同主题措辞差异。避免 女生≠研究生 这类换词型误并。
    """
    if len(fp_a) < FP_MIN_LEN or len(fp_b) < FP_MIN_LEN:
        return False
    sa, sb = set(fp_a), set(fp_b)
    return (sa <= sb or sb <= sa) or _fingerprint_overlap(fp_a, fp_b) >= FP_MIN_OVERLAP


def _lesson_weight(entry: dict[str, Any], now: Optional[float] = None) -> float:
    """\u9636\u6bb54: recency \u00d7 (1+log1p(hits)) \u00d7 quality \u52a0\u6743,\u51b7\u6761\u76ee\u81ea\u7136\u964d\u6743\u3002"""
    now = now if now is not None else time.time()
    recency = 1.0
    ts = entry.get("updated_at") or ""
    if ts:
        try:
            age_days = (now - datetime.fromisoformat(ts).timestamp()) / 86400.0
            recency = math.exp(-max(0.0, age_days) / 90.0)
        except (ValueError, TypeError, OSError):
            recency = 1.0
    hits = int(entry.get("hits") or 1)
    quality = entry.get("quality")
    q = 1.0
    if isinstance(quality, list) and quality:
        ratios = []
        for rep in quality:
            if isinstance(rep, dict):
                ev = rep.get("evaluated")
                if ev:
                    ratios.append((rep.get("passed") or 0) / ev)
        if ratios:
            q = sum(ratios) / len(ratios)
    elif isinstance(quality, dict):
        ev = quality.get("evaluated")
        if ev:
            q = (quality.get("passed") or 0) / ev
    return recency * (1.0 + math.log1p(max(1, hits))) * q


def _dedup_by_goal(entries: list[dict[str, Any]], keep: int = MAX_LESSONS) -> list[dict[str, Any]]:
    """Merge entries sharing a signature (newest wins) or a near-duplicate topic
    fingerprint (\u9636\u6bb53a: \u8fd1\u4f3c\u91cd\u590d\u5408\u5e76\u8ba1\u6570); trim to ``keep`` by weight (\u9636\u6bb54:
    recency\u00d7hits\u00d7quality,\u51b7\u6761\u76ee\u5148\u6dd8\u6c70,\u4e0d\u518d\u53ea\u7559\u6700\u65b0 keep \u6761)\u3002"""
    out: list[dict[str, Any]] = []
    for entry in entries:
        sig = _goal_signature(entry.get("goal", ""))
        if not sig:
            continue
        replaced = False
        for i, existing in enumerate(out):
            if _goal_signature(existing.get("goal", "")) == sig:
                entry = {**entry, "hits": int(existing.get("hits", 1)) + 1}
                out[i] = entry
                replaced = True
                break
        if replaced:
            continue
        fp = _topic_fingerprint(entry.get("goal", ""))
        merged = False
        if fp:
            for i, existing in enumerate(out):
                efp = _topic_fingerprint(existing.get("goal", ""))
                if efp and _near_duplicate(fp, efp):
                    existing["hits"] = int(existing.get("hits", 1)) + 1
                    existing["goal"] = entry.get("goal", existing.get("goal", ""))
                    existing["signature"] = sig
                    existing["steps_ok"] = entry.get(
                        "steps_ok", existing.get("steps_ok")
                    )
                    existing["results"] = entry.get(
                        "results", existing.get("results")
                    )
                    existing["quality"] = entry.get("quality") or existing.get("quality")
                    existing["updated_at"] = (
                        entry.get("updated_at") or existing.get("updated_at", "")
                    )
                    merged = True
                    break
        if not merged:
            out.append(entry)

    if len(out) > keep:
        now = time.time()
        weighted = sorted(
            ((_lesson_weight(e, now), i, e) for i, e in enumerate(out)),
            key=lambda x: (x[0], x[1]),
        )
        drop = {id(e) for _, _, e in weighted[: len(out) - keep]}
        out = [e for e in out if id(e) not in drop]
    return out


def _preferences_text(profile: dict[str, Any]) -> str:
    """\u9636\u6bb51: \u628a\u7528\u6237\u53cd\u9988(user_preferences)\u805a\u5408\u4e3a\u53ef\u6ce8\u5165\u504f\u597d\u6458\u8981\u3002"""
    prefs = profile.get("user_preferences") or []
    if not prefs:
        return ""
    agg: dict[str, dict[str, float | int]] = {}
    for p in prefs:
        sig = p.get("goal_signature") or p.get("goal") or "_"
        g = agg.setdefault(sig, {"count": 0, "liked": 0, "score": 0})
        g["count"] = int(g["count"]) + 1
        if p.get("liked"):
            g["liked"] = int(g["liked"]) + 1
        g["score"] = int(g["score"]) + int(p.get("score") or 0)
    lines = ["User preferences (from their feedback on generated content):"]
    for sig, g in sorted(agg.items(), key=lambda x: x[1]["count"], reverse=True):
        avg = g["score"] / g["count"] if g["count"] else 0.0
        lines.append(
            f"- domain='{sig}' feedback={g['count']} liked={g['liked']} "
            f"avg_score={avg:.1f}"
        )
    return "\n".join(lines)


def _facts_text(profile: dict[str, Any]) -> str:
    """\u9636\u6bb52: \u628a\u81ea\u52a8\u62bd\u53d6\u7684 user_facts \u683c\u5f0f\u5316\u4e3a\u53ef\u6ce8\u5165\u6587\u672c\u3002"""
    facts = profile.get("user_facts") or {}
    if not facts:
        return ""
    lines = ["Known facts about the user (extracted from past runs):"]
    for sig, f in facts.items():
        domains = f.get("target_domains") or []
        avoids = f.get("avoid_topics") or []
        tone = f.get("preferred_tone") or ""
        struct = f.get("preferred_structure") or ""
        bits = [f"domains={domains}"] if domains else ["domains=?"]
        if tone:
            bits.append(f"tone={tone}")
        if struct:
            bits.append(f"structure={struct}")
        if avoids:
            bits.append(f"avoid={avoids}")
        lines.append(f"- {sig}: " + "; ".join(bits))
    return "\n".join(lines)


async def _extract_user_facts(
    backend, goal: str, outputs: dict[str, Any]
) -> Optional[UserFacts]:
    """\u9636\u6bb52: \u4e00\u6b21\u7ed3\u6784\u5316 LLM \u8c03\u7528,\u4ece goal + \u4ea7\u7269\u62bd\u7528\u6237\u4e8b\u5b9e\u3002\u5931\u8d25\u4e0d\u81f4\u547d\u3002"""
    results = outputs.get("results") or []
    sample = json.dumps(results[:3], ensure_ascii=False)[:1200] or "(no outputs)"
    prompt = (
        "From this content-generation goal and the content produced, extract facts "
        "about the end user (who they create content for, what they like/prefer).\n"
        f"Goal: {goal}\n"
        f"Produced content sample: {sample}\n"
        "Return target_domains (topics/audiences), preferred_tone, "
        "preferred_structure, avoid_topics."
    )
    try:
        return await backend.generate(prompt, UserFacts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("user-facts extraction failed (skipped): %s", exc)
        return None


def _union(*lists) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for item in lst or []:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _store_user_facts(
    long_term, goal: str, facts: Optional[UserFacts]
) -> None:
    """\u9636\u6bb52: \u6309 goal \u7b7e\u540d\u5408\u5e76 user_facts,\u65b0\u4e8b\u5b9e\u53e0\u52a0\u4e0d\u8986\u76d6\u5386\u53f2\u3002"""
    if facts is None:
        return
    sig = _goal_signature(goal)
    existing = dict(long_term.get("user_facts") or {})
    prev = existing.get(sig) or {}
    existing[sig] = {
        "target_domains": _union(prev.get("target_domains"), facts.target_domains),
        "avoid_topics": _union(prev.get("avoid_topics"), facts.avoid_topics),
        "preferred_tone": facts.preferred_tone or prev.get("preferred_tone", ""),
        "preferred_structure": (
            facts.preferred_structure or prev.get("preferred_structure", "")
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    long_term.set("user_facts", existing)


def _collect_outputs(
    memory: AgentMemory,
    focus_keywords: Optional[list[str]] = None,
    strategy: str = "best_n",
    top_n: int = 3,
) -> dict[str, Any]:
    """Assemble the user-facing outputs from recorded tool results.

    12.3: dedupe by title and rank by relevance to the goal domain so cross-round
    accumulation no longer mixes in off-topic / duplicated drafts.
    """
    from app.agent.artifacts import materialize_result

    outputs: dict[str, Any] = {}

    analysis = materialize_result(memory.latest("analyze_notes"))
    if analysis:
        outputs["analysis_summary"] = analysis.get("summary", "")
        outputs["top_keywords"] = analysis.get("top_keywords", [])
        outputs["top_tags"] = analysis.get("top_tags", [])
        outputs["title_patterns"] = analysis.get("title_patterns", [])
        outputs["insight_points"] = analysis.get("insight_points", [])

    contents = [materialize_result(item) for item in memory.results_for("generate_contents")]
    if contents:
        results: list[dict[str, Any]] = []
        quality_reports: list[dict[str, Any]] = []
        for c in contents:
            if isinstance(c, dict):
                results.extend(c.get("contents", []) or [])
                if c.get("quality_report"):
                    quality_reports.append(c["quality_report"])

        if strategy == "last_round":
            results = results[-top_n:]
        elif strategy == "all":
            results = _dedupe_by_title(results)
        else:  # best_n (default)
            results = _dedupe_by_title(results)
            if focus_keywords:
                results = _rank_by_relevance(results, focus_keywords)
            results = results[:top_n]

        outputs["results"] = [_strip_internal(item) for item in results]
        if quality_reports:
            outputs["quality_report"] = quality_reports

    published = [materialize_result(item) for item in memory.results_for("publish_to_xhs")]
    if published:
        outputs["publish_results"] = published

    return outputs


def _strip_internal(item: dict[str, Any]) -> dict[str, Any]:
    """Remove internal temp fields (_score/_verdict) before exposing to the user."""
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _dedupe_by_title(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge items sharing title + body[:60]; keep the higher _score."""
    seen: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for item in items:
        key = (str(item.get("title", "")), str(item.get("body", ""))[:60])
        if key in seen:
            if int(item.get("_score") or 0) > int(seen[key].get("_score") or 0):
                seen[key] = item
        else:
            seen[key] = item
            order.append(key)
    return [seen[k] for k in order]


def _rank_by_relevance(
    items: list[dict[str, Any]], focus_keywords: list[str]
) -> list[dict[str, Any]]:
    """Sort by number of focus-keyword hits (desc), then by score (desc)."""
    focus = [kw for kw in (focus_keywords or []) if kw]

    def _hits(item: dict[str, Any]) -> int:
        text = "{} {}".format(item.get("title", ""), item.get("body", ""))
        return sum(1 for kw in focus if kw in text)

    return sorted(
        items,
        key=lambda item: (_hits(item), int(item.get("_score") or 0)),
        reverse=True,
    )
