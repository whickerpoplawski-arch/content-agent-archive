"""Planner and Reflector: the decision-making brain of the Agent.

The Planner turns a goal + available tools into an ordered plan. The Reflector
observes each step result and decides whether to continue, replan, or stop, and
recovers from failures. Both are LLM-backed but kept behind the unified layer so
the same loop runs with a real backend or the deterministic mock.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from app.agent.focus import _extract_focus
from app.agent.llm import LLMBackend, generate_structured
from app.agent.memory import AgentMemory
from app.core.config import settings
from app.agent.schemas import (
    Plan,
    PlanStep,
    Recovery,
    Reflection,
    StepStatus,
    ToolSpec,
)

logger = logging.getLogger("agent.planner")

PLANNER_TEMPLATE = """You are the planner of an autonomous Xiaohongshu (XHS) content agent.
Given a goal and a set of available tools, produce an ordered plan to achieve it.

Available tools (name: params schema - description):
{tools}

Goal:
{goal}

User profile:
{profile}

Rules:
- Only reference the tools listed above.
- Each step: action = a tool name; params = its parameters; rationale = why it helps.
- To consume an earlier step's output, use references like:
  $latest.<tool>            -> the full latest result of that tool
  $latest.<tool>.<field>    -> a field of it (list index allowed, e.g. .topics.0)
- Use the "params:" JSON schema of each tool to set correct parameter names/types.
- For content-generation goals, PREFER chaining sample data:
  load_notes -> analyze_notes -> generate_topics -> generate_contents.
  Only use crawl_notes when the goal explicitly asks for fresh/live crawling
  (crawl_notes is interactive and needs a logged-in browser).
- Keep the plan minimal and goal-focused.
- Do NOT split one goal into multiple steps of the same tool: a single
  generate_contents step with params.count = the number of copies needed is
  better than several generate_contents steps each with count=1.
- If the goal names an explicit topic domain (e.g. "暑期实习" / "考研"), you MUST
  list that domain in params.focus_keywords of generate_topics / generate_contents
  steps; topics and copy must hit at least one of them.
- Adaptive retrieval: knowledge_search is read-only and nearly free — call it
  FIRST (before load_notes / crawl_notes) whenever the goal names a topic
  domain or audience the knowledge base may already cover (research,
  comparison, trend, or content-generation goals alike). Ground later steps in
  its hits; only skip it when the goal is trivial with no named domain. If its
  hits are empty or off-topic and the goal needs fresh data, fall back to
  crawl_notes (interactive, needs a logged-in browser) or load_notes sample data.
- When the profile contains knowledge lines tagged [K1]..[Kn], treat them as
  retrieved evidence: ground your step params in the relevant Kx and you MUST
  name the Kx you rely on in that step's rationale (read what you retrieve).
  Never invent K tags that were not provided.
"""

REFLECT_TEMPLATE = """You are observing an agent executing a plan.
Goal: {goal}
Plan so far:
{plan}
Latest observations:
{observations}
Round quality report (objective, authoritative -- do not contradict it):
{quality_report}
Objective progress (count actual results, never self-report counts):
- produced copy count: {progress_total}
- on-target & passing count: {progress_passed}
- target count: {progress_target}
Decide whether to continue, replan, or stop.
- should_stop: true only when the goal is satisfied (on-target & passing reaches the target count).
- next_action: "continue" (keep going), "replan" (change the plan), or "stop".
- reason: one short sentence.
"""

RECOVER_TEMPLATE = """A step of the agent's plan failed.
Goal: {goal}
Failed step:
{step}
Error: {error}
Propose how to recover.
- next_action: "retry" (retry same tool with adjusted_params), "replan" (change plan), or "stop".
- adjusted_params: new parameters for the retry (empty if not retrying).
- reason: one short sentence.
"""

# Stable system block used by ContextBuilder. The legacy templates stay the
# flag-off prompts so existing runs keep the same wording.
PLANNER_SYSTEM = """You are the planner of an autonomous Xiaohongshu (XHS) content agent.
Given a goal and a set of available tools, produce an ordered plan to achieve it.

Rules:
- Only reference the tools listed in the tools block.
- Each step: action = a tool name; params = its parameters; rationale = why it helps.
- To consume an earlier step's output, use references like:
  $latest.<tool>            -> the full latest result of that tool
  $latest.<tool>.<field>    -> a field of it (list index allowed, e.g. .topics.0)
  If a result was offloaded, $latest.<tool> is an artifact handle. Read fields with
  read_artifact instead of expecting the full payload to be inlined.
- Use the "params:" JSON schema of each tool to set correct parameter names/types.
- For content-generation goals, PREFER chaining sample data:
  load_notes -> analyze_notes -> generate_topics -> generate_contents.
  Only use crawl_notes when the goal explicitly asks for fresh/live crawling
  (crawl_notes is interactive and needs a logged-in browser).
- Keep the plan minimal and goal-focused.
- Do NOT split one goal into multiple steps of the same tool: a single
  generate_contents step with params.count = the number of copies needed is
  better than several generate_contents steps each with count=1.
- If the goal names an explicit topic domain (e.g. "暑期实习" / "考研"), you MUST
  list that domain in params.focus_keywords of generate_topics / generate_contents
  steps; topics and copy must hit at least one of them.
- Adaptive retrieval: knowledge_search is read-only and nearly free — call it
  FIRST (before load_notes / crawl_notes) whenever the goal names a topic
  domain or audience the knowledge base may already cover.
- When the profile contains knowledge lines tagged [K1]..[Kn], treat them as
  retrieved evidence and name the Kx you rely on in that step's rationale.
  Never invent K tags that were not provided.
"""

REFLECT_SYSTEM = """You are observing an agent executing a plan.
Decide whether to continue, replan, or stop.
- should_stop: true only when the goal is satisfied (on-target & passing reaches the target count).
- next_action: "continue" (keep going), "replan" (change the plan), or "stop".
- reason: one short sentence.
The round quality report is objective and authoritative — do not contradict it.
Count actual results; never self-report counts.
"""

RECOVER_SYSTEM = """A step of the agent's plan failed. Propose how to recover.
- next_action: "retry" (retry same tool with adjusted_params), "replan" (change plan), or "stop".
- adjusted_params: new parameters for the retry (empty if not retrying).
- reason: one short sentence.
"""


class Planner:
    def __init__(self, backend: LLMBackend, tools: list[ToolSpec]) -> None:
        self._backend = backend
        self._tools = tools

    def _tools_text(self) -> str:
        lines = []
        for t in self._tools:
            lines.append(f"- {t.name}({t.params_schema_name}): {t.description}")
            lines.append(f"    params: {t.params_schema_json}")
        return "\n".join(lines)

    def _tools_concise(self) -> str:
        return "\n".join(f"- {t.name}: {t.description}" for t in self._tools)

    def _assemble(self, role: str, parts, tracer, run_id: str, **kwargs):
        return _assemble_role(role, parts, tracer, run_id, **kwargs)

    async def plan(
        self,
        goal: str,
        profile: str,
        *,
        context_parts=None,
        tracer=None,
        run_id: str = "",
    ) -> Plan:
        if settings.agent_context_builder_enabled:
            parts = context_parts or self._fallback_planner_parts(goal, profile)
            prompt = self._assemble("planner", parts, tracer, run_id).text
        else:
            prompt = PLANNER_TEMPLATE.format(
                tools=self._tools_text(), goal=goal, profile=profile
            )
        plan = await generate_structured(prompt, Plan, backend=self._backend)
        plan.goal = goal
        if not plan.steps:
            raise RuntimeError("Planner returned an empty plan")
        _inject_focus_steps(plan, _extract_focus(goal))
        return plan

    def _fallback_planner_parts(self, goal: str, profile: str, extra: str = ""):
        from app.agent.context_builder import ContextPart

        parts = [
            ContextPart(key="system", text=PLANNER_SYSTEM, priority=1, required=True),
            ContextPart(key="goal", text=f"Goal:\n{goal}", priority=2, required=True),
            ContextPart(
                key="tools",
                text=f"Available tools (name: params schema - description):\n{self._tools_text()}",
                concise_text=f"Available tools (name: description):\n{self._tools_concise()}",
                priority=3,
                required=True,
            ),
        ]
        if extra.strip():
            parts.append(ContextPart(key="plan_progress", text=extra.strip(), priority=5, required=True))
        if (profile or "").strip():
            parts.append(ContextPart(key="semantic", text=f"User profile:\n{profile}", priority=4))
        if settings.agent_skills_enabled:
            from app.agent.skills_loader import skill_index_text

            index = skill_index_text(goal)
            if index:
                parts.append(ContextPart(key="skill_index", text=index, priority=7))
        return parts

    async def replan(
        self,
        goal: str,
        profile: str,
        failed_step: Optional[PlanStep],
        focus_keywords: Optional[list[str]] = None,
        *,
        context_parts=None,
        tracer=None,
        run_id: str = "",
    ) -> Plan:
        focus = list(focus_keywords) if focus_keywords else _extract_focus(goal)
        focus_note = ""
        if focus:
            focus_note = (
                "\nIMPORTANT: the previous plan drifted off the target domain. "
                "Keep every topic/copy inside the domain "
                f"{focus}. Set params.focus_keywords={focus} on generate_topics / "
                "generate_contents steps and do NOT repeat the same off-topic plan shape.\n"
            )
        replan_note = (
            f"Re-plan to achieve this goal. The previous plan failed at: "
            f"{failed_step.action if failed_step else 'unknown'} "
            f"with error: {failed_step.error if failed_step else 'unknown'}.\n"
            + focus_note
        )
        if settings.agent_context_builder_enabled:
            from app.agent.context_builder import ContextPart

            parts = list(context_parts) if context_parts else self._fallback_planner_parts(
                goal, profile, extra=replan_note
            )
            if context_parts:
                parts.append(
                    ContextPart(key="replan_note", text=replan_note.strip(), priority=5, required=True)
                )
            prompt = self._assemble("planner", parts, tracer, run_id).text
        else:
            prompt = (
                replan_note
                + "\n"
                + PLANNER_TEMPLATE.format(tools=self._tools_text(), goal=goal, profile=profile)
            )
        plan = await generate_structured(prompt, Plan, backend=self._backend)
        plan.goal = goal
        _inject_focus_steps(plan, focus)
        return plan

    def append_steps(
        self,
        goal: str,
        deficit: int,
        audience: Optional[str] = None,
        tone: Optional[str] = None,
    ) -> list[PlanStep]:
        """Append a 补足 generate_contents step when the plan is exhausted but
        the target count is not met yet (optimization plan 12.5). Deterministic
        so it works offline; the step reuses the latest analysis/topics.

        audience/tone are inherited from the original plan step (real-run
        2026-08-12: hard-coding the defaults silently drifted 西电研究生/真诚校园风
        into 大学生女生/真实分享 on appended steps).
        """
        focus = _extract_focus(goal)
        step = PlanStep(
            action="generate_contents",
            params={
                "topic": "$latest.generate_topics.topics.0.title",
                "reason": "$latest.generate_topics.topics.0.reason",
                "audience": audience or "大学生女生",
                "tone": tone or "真实分享",
                "count": max(1, deficit),
                "focus_keywords": focus,
            },
            rationale="计划已满但目标未达标，追加生成以补足缺额",
        )
        return [step]



class Reflector:
    def __init__(self, backend: LLMBackend) -> None:
        self._backend = backend

    async def judge(
        self,
        goal: str,
        plan: Plan,
        memory: AgentMemory,
        *,
        progress: Optional[dict] = None,
        quality_report: Optional[Any] = None,
        tracer=None,
        run_id: str = "",
    ) -> Reflection:
        progress = progress or {}
        quality_text = (
            json.dumps(quality_report, ensure_ascii=False, default=str)
            if quality_report
            else "(no quality gate run yet)"
        )
        if settings.agent_obs_compress_enabled or settings.agent_context_builder_enabled:
            prompt = await self._judge_prompt(
                goal,
                plan,
                memory,
                progress,
                quality_text,
                tracer=tracer,
                run_id=run_id,
            )
        else:
            prompt = REFLECT_TEMPLATE.format(
                goal=goal,
                plan=_plan_text(plan),
                observations=memory.observations(),
                quality_report=quality_text,
                progress_total=progress.get("total", 0),
                progress_passed=progress.get("on_target_passed", 0),
                progress_target=progress.get("target", 0),
            )
        return await generate_structured(prompt, Reflection, backend=self._backend)

    def _reflect_parts(self, goal, plan, memory, progress, quality_text, *, full_obs: bool):
        from app.agent.context_builder import ContextPart

        observations = memory.observations(full=full_obs)
        progress_text = (
            f"Objective progress:\n"
            f"- produced copy count: {progress.get('total', 0)}\n"
            f"- on-target & passing count: {progress.get('on_target_passed', 0)}\n"
            f"- target count: {progress.get('target', 0)}"
        )
        parts = [
            ContextPart(key="system", text=REFLECT_SYSTEM, priority=1, required=True),
            ContextPart(key="goal", text=f"Goal: {goal}", priority=2, required=True),
            ContextPart(
                key="plan_progress",
                text=f"Plan so far:\n{_plan_text(plan)}\n\n{progress_text}",
                priority=3,
                required=True,
            ),
            ContextPart(key="quality_report", text=f"Round quality report:\n{quality_text}", priority=4),
            ContextPart(key="recent_obs", text=f"Latest observations:\n{observations}", priority=5),
        ]
        return parts

    async def _judge_prompt(self, goal, plan, memory, progress, quality_text, *, tracer, run_id: str) -> str:
        from app.agent.compress import compress_observations, compression_needed
        from app.agent.context_builder import budgets_for

        soft, _hard = budgets_for("reflector")
        compressed = False
        before_total = None
        if settings.agent_obs_compress_enabled and compression_needed(memory, soft):
            if settings.agent_context_builder_enabled:
                before = _assemble_role(
                    "reflector",
                    self._reflect_parts(goal, plan, memory, progress, quality_text, full_obs=True),
                    tracer,
                    run_id,
                    compressed=False,
                    stamp_before=True,
                )
                before_total = before.tokens
            info = await compress_observations(memory, self._backend, goal=goal, tracer=tracer)
            compressed = bool(info.get("compressed"))
        if settings.agent_context_builder_enabled:
            assembled = _assemble_role(
                "reflector",
                self._reflect_parts(goal, plan, memory, progress, quality_text, full_obs=False),
                tracer,
                run_id,
                compressed=compressed,
                before_total=before_total,
            )
            return assembled.text
        return REFLECT_TEMPLATE.format(
            goal=goal,
            plan=_plan_text(plan),
            observations=memory.observations(),
            quality_report=quality_text,
            progress_total=progress.get("total", 0),
            progress_passed=progress.get("on_target_passed", 0),
            progress_target=progress.get("target", 0),
        )

    async def recover(
        self,
        goal: str,
        step: PlanStep,
        error: str,
        *,
        tracer=None,
        run_id: str = "",
    ) -> Recovery:
        if settings.agent_context_builder_enabled:
            from app.agent.context_builder import ContextPart

            parts = [
                ContextPart(key="system", text=RECOVER_SYSTEM, priority=1, required=True),
                ContextPart(key="goal", text=f"Goal: {goal}", priority=2, required=True),
                ContextPart(
                    key="plan_progress",
                    text=f"Failed step:\n{step.model_dump_json()}\nError: {error}",
                    priority=3,
                    required=True,
                ),
            ]
            prompt = _assemble_role("recover", parts, tracer, run_id).text
        else:
            prompt = RECOVER_TEMPLATE.format(goal=goal, step=step.model_dump_json(), error=error)
        return await generate_structured(prompt, Recovery, backend=self._backend)


def _assemble_role(role: str, parts, tracer, run_id: str, **kwargs):
    from app.agent.context_builder import assemble, budgets_for

    soft, hard = budgets_for(role)
    return assemble(
        parts,
        role=role,
        budget=soft,
        hard=hard,
        window=settings.agent_context_window_tokens,
        run_id=run_id or getattr(tracer, "run_id", "") or "",
        tracer=tracer,
        **kwargs,
    )


def _inject_focus_steps(plan: Plan, focus: list[str]) -> None:
    """Ensure generate_topics / generate_contents carry the target-domain keywords,
    so replan/execution never loses the goal domain (optimization plan 12.1)."""
    if not focus:
        return
    for step in plan.steps:
        if step.action in ("generate_topics", "generate_contents"):
            if not step.params.get("focus_keywords"):
                step.params["focus_keywords"] = list(focus)


def _plan_text(plan: Plan) -> str:
    return json.dumps(
        [
            {"action": s.action, "status": s.status.value, "summary": s.summary}
            for s in plan.steps
        ],
        ensure_ascii=False,
    )
