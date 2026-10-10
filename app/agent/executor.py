"""Executor: runs the plan step by step, resolving data dependencies, retrying
failures, and reflecting between steps so the loop is genuinely adaptive.

Applies the Parallelization / Orchestrator-Workers pattern (Anthropic
"Building Effective Agents"): independent steps whose data dependencies are
already satisfied run concurrently (bounded by ``max_parallel``); dependent steps
stay sequential. Replan/stop decisions are surfaced to the outer loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.agent.focus import _extract_focus, _extract_target_count, _same_reason_streak
from app.agent.memory import AgentMemory
from app.agent.planner import Planner, Reflector
from app.agent.schemas import Plan, PlanStep, StepStatus
from app.agent.tools import ToolRegistry

logger = logging.getLogger("agent.executor")

MAX_TOTAL_STEPS = 50
MAX_REPLAN_SAME_REASON = 3
MAX_CRAWL_EMPTY_RETRIES = 1  # 13.1: crawl 返回 0 条最多允许 1 次重爬,超限熔断
MAX_IDENTICAL_ERRORS = 1     # 13.4: 同一工具同错误连续 2 次即停止无效重试
_REF_RE = re.compile(r"\$latest\.([a-z_]+)")

# 13.2: bridge between the Executor (owns stop_check + tracer) and tool handlers
# (only receive validated params). `_crawl` reads this to drive cooperative
# cancellation and per-note progress events while the crawl runs.
_crawl_ctx: ContextVar[Optional[dict]] = ContextVar("crawl_ctx", default=None)


async def resolve_params(params: dict[str, Any], memory: AgentMemory) -> dict[str, Any]:
    """Resolve $latest.<tool>[.<field>...] references against recorded outputs."""

    async def _resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            return _resolve_ref(value, memory)
        if isinstance(value, dict):
            return {k: await _resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [await _resolve(v) for v in value]
        return value

    return {k: await _resolve(v) for k, v in params.items()}


def _resolve_ref(ref: str, memory: AgentMemory) -> Any:
    parts = ref.lstrip("$").split(".")
    if not parts or parts[0] != "latest" or len(parts) < 2:
        raise KeyError(f"Invalid reference: {ref}")
    result = memory.latest(parts[1])
    if result is None:
        raise KeyError(f"No result yet for tool '{parts[1]}' (ref {ref})")
    return _best_effort_dig(result, parts[2:])


def _best_effort_dig(value: Any, path: list[str]) -> Any:
    """Descend along ``path`` but return the closest reachable ancestor on any
    miss. This makes planner references like ``$latest.load_notes.items``
    degrade gracefully to the full ``load_notes`` list instead of crashing or
    resolving to empty data."""
    current = value
    for segment in path:
        try:
            if isinstance(current, list):
                current = current[int(segment)]
            elif isinstance(current, dict):
                if segment not in current:
                    break
                current = current[segment]
            else:
                break
        except (ValueError, TypeError, IndexError, KeyError):
            break
    return current


def _summarize(value: Any, max_len: int = 120) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _deps(step: PlanStep) -> set[str]:
    """Tools this step depends on, inferred from $latest.<tool> references."""
    deps: set[str] = set()
    text = json.dumps(step.params, ensure_ascii=False, default=str)
    deps.update(_REF_RE.findall(text))
    return deps


def _step_focus(plan: Plan, action: str) -> list[str]:
    """Target-domain keywords carried by the first plan step of ``action``."""
    for step in plan.steps:
        if step.action == action:
            return list(step.params.get("focus_keywords") or [])
    return []


def _step_text_params(plan: Plan, action: str, keys: tuple[str, ...]) -> dict:
    """Scalar params carried by the first plan step of ``action``, as a dict for
    reuse (e.g. audience/tone inheritance on appended 补足 steps)."""
    for step in plan.steps:
        if step.action == action and isinstance(step.params, dict):
            return {k: step.params[k] for k in keys if step.params.get(k)}
    return {}


def _publish_text(params: dict[str, Any]) -> str:
    """Extract the full copy to be published (title/body/hashtags/cta) for the
    output guardrail tripwire (P0-2). Works for PublishParams and FeishuParams."""
    text = " ".join([
        str(params.get("content_title") or params.get("title") or ""),
        str(params.get("content_body") or params.get("content") or ""),
        " ".join(params.get("content_hashtags") or params.get("tags") or []),
        str(params.get("content_cta") or ""),
    ])
    return text


def _latest_quality_report(memory: AgentMemory) -> Any:
    latest = memory.latest("generate_contents")
    if isinstance(latest, dict):
        return latest.get("quality_report")
    return None


def _quality_passed_count(memory: AgentMemory) -> Optional[int]:
    """缺陷1: 汇总所有质量门实测报告的 passed 数,作为 on_target_passed 的权威
    来源。没有任何质量门报告时返回 None(回退到 _on_target 计数)。

    反思器不得自行估数 —— 质量门报告(逐轮 evaluated/passed)才是客观事实。
    """
    total = 0
    any_report = False
    for result in memory.results_for("generate_contents"):
        if not isinstance(result, dict):
            continue
        report = result.get("quality_report")
        if isinstance(report, dict):
            any_report = True
            total += int(report.get("passed", 0))
    return total if any_report else None


def _on_target(focus: list[str]) -> Any:
    """Predicate: a generated item counts as on-target only when it hits a focus
    keyword AND passed the quality gate (optimization plan 12.5)."""

    def pred(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        passed = item.get("_verdict") == "pass" or int(item.get("_score") or 0) >= 7
        if not focus:
            return passed
        text = "{} {}".format(item.get("title", ""), item.get("body", ""))
        return passed and any(kw and kw in text for kw in focus)

    return pred


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        reflector: Reflector,
        planner: Planner,
        *,
        max_retries_per_step: int = 3,
        max_total_steps: Optional[int] = None,
        max_parallel: int = 4,
        tracer=None,
        quality_gate=None,
        budget=None,
        checkpointer=None,
        base_usage=None,
        guardrails=None,
        get_usage=None,
        stop_check: Optional[Callable[[], bool]] = None,
        approval_handler: Optional[
            Callable[[str, dict[str, Any]], Awaitable[bool]]
        ] = None,
    ) -> None:
        self.registry = registry
        self.reflector = reflector
        self.planner = planner
        self.max_retries_per_step = max_retries_per_step
        self.max_total_steps = max_total_steps
        self.max_parallel = max_parallel
        self.tracer = tracer
        self.quality_gate = quality_gate
        self.budget = budget
        self.checkpointer = checkpointer
        self.base_usage = base_usage or {}
        self.guardrails = guardrails
        self.get_usage = get_usage
        self.stop_check = stop_check
        self.approval_handler = approval_handler
        self._replan_requested = False
        self._stop_requested = False
        self._graceful_stop = False
        self._replan_history: list[str] = []
        self._force_focus: Optional[list[str]] = None
        self._replan_count = 0
        self._need_more = False

    async def execute(self, goal: str, profile: str, plan: Plan, memory: AgentMemory) -> Plan:
        self._current_goal = goal
        retries = {i: 0 for i in range(len(plan.steps))}
        total = sum(
            1 for s in plan.steps if s.status == StepStatus.SUCCEEDED
        )

        max_total = self.max_total_steps or (
            self.budget.max_steps if self.budget and self.budget.max_steps else 0
        ) or MAX_TOTAL_STEPS

        self._replan_history = []
        self._force_focus = None
        self._replan_count = 0
        self._need_more = False
        self._graceful_stop = False
        self._crawl_empty_calls = 0  # 13.1: consecutive empty crawl_notes results
        self._last_error: dict[str, tuple[str, int]] = {}  # 13.4: per-tool identical error streak

        while total < max_total:
            if self._stop_requested or (self.stop_check and self.stop_check()):
                break
            if self._replan_requested:
                failed_actions = [
                    s.action for s in plan.steps if s.status == StepStatus.FAILED
                ]
                new_plan = await self.planner.replan(
                    goal, profile, plan.failed_step, focus_keywords=self._force_focus
                )
                # P1-4: replan 硬校验。存在 FAILED 步骤时,新计划必须包含对失败
                # 动作的恢复步骤(同 action),否则拒绝 replan 直接 stop —— 防止
                # LLM 一句「replan」却没有对应恢复动作的瞎重规划/死循环。
                if failed_actions:
                    new_actions = [s.action for s in new_plan.steps]
                    missing = sorted(set(failed_actions) - set(new_actions))
                    if missing:
                        logger.warning(
                            "replan rejected: no recovery step for %s", missing
                        )
                        if self.tracer:
                            self.tracer.event(
                                "replan_rejected",
                                missing=missing,
                                failed_actions=failed_actions,
                                new_actions=new_actions,
                            )
                        self._stop_requested = True
                        break
                plan = new_plan
                self._replan_count += 1
                retries = {i: 0 for i in range(len(plan.steps))}
                self._replan_requested = False
                logger.info("replanned to %d steps", len(plan.steps))
                continue

            ready = self._ready_indices(plan, memory)
            if not ready:
                # 12.5: plan exhausted but reflector still says "continue" and the
                # target count is unmet -> append a补足 generate_contents step.
                if self._need_more:
                    focus = _step_focus(plan, "generate_contents") or _extract_focus(goal)
                    target = _extract_target_count(goal)
                    passed = memory.count_results(
                        "generate_contents", _on_target(focus)
                    )
                    deficit = target - passed if target > 0 else 0
                    if deficit > 0:
                        # real-run 2026-08-12: inherit the original step's
                        # audience/tone so appended copies don't silently drift
                        # to hard-coded defaults.
                        inherit = _step_text_params(
                            plan, "generate_contents", ("audience", "tone")
                        )
                        extra = self.planner.append_steps(goal, deficit, **inherit)
                        plan.steps.extend(extra)
                        for i in range(len(plan.steps) - len(extra), len(plan.steps)):
                            retries[i] = 0
                        logger.info(
                            "appended %d generate_contents step(s), deficit=%d",
                            len(extra), deficit,
                        )
                        self._need_more = False
                        continue
                break
            batch = ready[: self.max_parallel]
            logger.info("executing batch: %s", [i for i in batch])
            await asyncio.gather(
                *[self._run_step(goal, profile, plan, memory, i, retries) for i in batch]
            )
            total += len(batch)

            usage = self._combined_usage()
            # P2-6: 每个 batch 后把当前用量快照挂到 trace,审计时可定位任意
            # 步骤的累计成本,而不只在 done 事件看最终值。
            if self.tracer:
                self.tracer.event("usage", usage=usage)
            if self.checkpointer and self.tracer:
                self.checkpointer.save(
                    self.tracer.run_id, plan, memory.steps, usage
                )
                self.tracer.flush()

            if self.budget and self.get_usage:
                if self.budget.exceeded(usage, total, replans=self._replan_count):
                    reason = self.budget.reason(usage, total, replans=self._replan_count)
                    logger.warning("agent budget exceeded -> stop: %s", reason)
                    if self.tracer:
                        self.tracer.event("budget_stop", reason=reason, usage=usage)
                    self._stop_requested = True
                    break

        # 缺陷(实测 2026-08-13): 反思主动 stop 是「目标达成收尾」,剩余未执行
        # 步骤(PENDING)标 SKIPPED,plan.is_complete 才会为 True -> 终态 completed。
        # 否则 crawl 5/5 + 生成全过,却因 analyze 还没轮到就被标 failed「Plan
        # incomplete」,属于终态误报。预算中断/失败路径不置 _graceful_stop,
        # 仍保持 failed(不虚报 completed)。
        if self._graceful_stop:
            skipped = 0
            for step in plan.steps:
                if step.status == StepStatus.PENDING:
                    step.status = StepStatus.SKIPPED
                    step.summary = "Skipped: reflector stopped the run"
                    skipped += 1
            if skipped and self.tracer:
                self.tracer.event(
                    "steps_skipped_on_stop",
                    count=skipped,
                    actions=[s.action for s in plan.steps if s.status == StepStatus.SKIPPED],
                )

        return plan

    def _combined_usage(self) -> dict[str, Any]:
        current = self.get_usage() if self.get_usage else {}
        merged = dict(current)
        for key, value in (self.base_usage or {}).items():
            merged[key] = merged.get(key, 0) + value
        return merged

    def _ready_indices(self, plan: Plan, memory: AgentMemory) -> list[int]:
        ready = []
        for i, step in enumerate(plan.steps):
            if step.status not in (StepStatus.PENDING, StepStatus.RUNNING):
                continue
            deps = _deps(step)
            # 缺陷8.2(实测 2026-08-13): planner 生成的 generate_contents 步骤
            # 可能不带 $latest 引用(硬编码 topic),导致与 crawl/analyze 同批并行、
            # 生成不消费爬取数据。确定性兜底:仅当 plan 中排在本步骤之前的
            # analyze/crawl 存在时强制依赖之(按索引,保证是"前置数据源"),
            # 让生成在真实数据就绪后执行;排在之后的步骤不构成依赖。
            if step.action == "generate_contents" and not deps:
                for producer in ("analyze_notes", "crawl_notes"):
                    if any(s.action == producer for s in plan.steps[:i]):
                        deps.add(producer)
            if all(memory.latest(t) is not None for t in deps):
                ready.append(i)
        return ready

    async def _run_step(
        self,
        goal: str,
        profile: str,
        plan: Plan,
        memory: AgentMemory,
        index: int,
        retries: dict[int, int],
    ) -> None:
        step = plan.steps[index]
        step.status = StepStatus.RUNNING
        try:
            params = await resolve_params(step.params, memory)
            if self.approval_handler and step.action in {
                "publish_to_xhs",
                "sync_to_feishu",
            }:
                approved = await self.approval_handler(step.action, params)
                if not approved:
                    step.status = StepStatus.SKIPPED
                    step.summary = "Approval rejected; tool was not executed"
                    if self.tracer:
                        self.tracer.event(
                            "approval_rejected", name=step.action, params=params
                        )
                    return
            if self.guardrails:
                guard = self.guardrails.check_tool(step.action)
                if not guard.allowed:
                    step.status = StepStatus.SKIPPED
                    step.summary = f"Guardrail blocked tool: {guard.reason}"
                    if self.tracer:
                        self.tracer.event(
                            "guardrail_blocked",
                            guard_type="tool",
                            name=step.action,
                            reason=guard.reason,
                            details=guard.details,
                        )
                    return
            # P0-2: 副作用工具(publish/sync)调用前的 output guardrail tripwire。
            # 命中非合规文案 -> 阻断执行(步骤 FAILED 并透出具体词),不再仅标 trace。
            # blocking 执行:在工具启动前串行完成,不发副作用调用。
            if (
                step.action in ("publish_to_xhs", "sync_to_feishu")
                and self.guardrails
            ):
                copy_text = _publish_text(params)
                out_guard = self.guardrails.check_output(copy_text)
                if not out_guard.allowed:
                    step.status = StepStatus.FAILED
                    step.error = "非合规文案已拦截: " + ", ".join(out_guard.details)
                    step.summary = step.error[:120]
                    if self.tracer:
                        self.tracer.event(
                            "guardrail_blocked",
                            guard_type="output",
                            name=step.action,
                            reason=out_guard.reason,
                            details=out_guard.details,
                        )
                    logger.warning(
                        "output guardrail blocked %s (non-compliant copy): %s",
                        step.action, out_guard.details,
                    )
                    return
            if self.tracer:
                self.tracer.event(
                    "tool_start", name=step.action, params=params, rationale=step.rationale
                )
            _t0 = time.time()
            if step.action == "crawl_notes":
                # 13.2: give `_crawl` access to the executor's stop_check + tracer
                # so an in-flight crawl can be cancelled and report progress.
                token = _crawl_ctx.set(
                    {
                        "stop_check": self.stop_check,
                        "sink": (
                            (lambda kind, **fields: self.tracer.event(kind, **fields))
                            if self.tracer
                            else None
                        ),
                    }
                )
                try:
                    result = await self.registry.call(step.action, params)
                finally:
                    _crawl_ctx.reset(token)
            else:
                result = await self.registry.call(step.action, params)
            _elapsed = (time.time() - _t0) * 1000
            step.status = StepStatus.SUCCEEDED
            step.summary = _summarize(result)
            if self.quality_gate and step.action == "generate_contents":
                # 缺陷1: 先跑质量门再入 memory,保证 quality_report/_verdict 一并
                # 记录,反思器才有权威实测数据可消费(此前 record 在前,memory 里的
                # 结果缺质量报告,导致反思只能靠 LLM 数数而振荡)。
                result = await self._refine_contents(result, step)
            memory.record(step.action, step.params, result, "succeeded")
            if self.tracer:
                self.tracer.tool_call(step.action, True, _elapsed, step.summary)
            if step.action == "crawl_notes":
                self._track_crawl_result(result)
                if self._stop_requested:
                    return
        except Exception as exc:  # noqa: BLE001
            step.status = StepStatus.FAILED
            step.error = str(exc)
            if self.tracer:
                self.tracer.tool_call(step.action, False, 0.0, str(exc))
            logger.warning("step %d (%s) failed: %s", index, step.action, exc)

            # 13.4: identical error twice in a row -> stop pointless retries early
            # instead of letting the LLM burn retries on an unsupported fix.
            prev = self._last_error.get(step.action)
            if prev and prev[0] == str(exc):
                self._last_error[step.action] = (str(exc), prev[1] + 1)
            else:
                self._last_error[step.action] = (str(exc), 1)
            if prev and prev[0] == str(exc) and prev[1] >= MAX_IDENTICAL_ERRORS:
                logger.warning(
                    "tool %s failed %d times with identical error; stopping retries",
                    step.action, prev[1] + 1,
                )
                if self.tracer:
                    self.tracer.event(
                        "tool_stall", name=step.action, error=str(exc),
                        repeats=prev[1] + 1,
                    )
                self._stop_requested = True
                return

            # 13.4: attach the supported param list so reflection stops inventing
            # unsupported arguments from the error text alone.
            try:
                tool = self.registry.get(step.action)
                supported = ", ".join(tool.params_schema.model_fields)
                hint = f" [工具 {step.action} 可用参数]: {supported}"
            except Exception:  # noqa: BLE001
                hint = ""
            recovery = await self.reflector.recover(goal, step, str(exc) + hint)
            if recovery.next_action == "retry":
                retries[index] += 1
                if retries[index] > self.max_retries_per_step:
                    logger.error("giving up on step %d after retries", index)
                    return
                step.params = recovery.adjusted_params or step.params
                step.status = StepStatus.PENDING
                return
            if recovery.next_action == "replan":
                self._replan_requested = True
                return
            # stop
            self._stop_requested = True
            return

        # Reflect after a successful step (early stop / adaptive replan).
        try:
            focus = _step_focus(plan, "generate_contents") or _extract_focus(goal)
            target = _extract_target_count(goal)
            # 缺陷1: 质量门实测报告为准,不依赖 LLM 自行数数。
            qpassed = _quality_passed_count(memory)
            counted = memory.count_results("generate_contents", _on_target(focus))
            passed = qpassed if qpassed is not None else counted
            progress = {
                "total": memory.count_results("generate_contents"),
                "on_target_passed": passed,
                "target": target,
            }
            verdict = await self.reflector.judge(
                goal, plan, memory,
                progress=progress,
                quality_report=_latest_quality_report(memory),
            )
            if self.tracer:
                self.tracer.event(
                    "reflection",
                    next_action=verdict.next_action,
                    should_stop=verdict.should_stop,
                    reason=verdict.reason,
                    progress=progress,
                )
            # 缺陷1/2: 质量门已全过(passed >= target)却仍要求 continue/replan
            # -> 以实测数据为准直接 stop,杜绝"3/3 全过还重规划"的振荡与虚报。
            self._graceful_stop = verdict.should_stop
            if (
                verdict.next_action in ("continue", "replan")
                and qpassed is not None
                and target
                and qpassed >= target
            ):
                logger.info(
                    "quality report passed=%d >= target=%d; overriding "
                    "reflector '%s' -> stop",
                    qpassed, target, verdict.next_action,
                )
                if self.tracer:
                    self.tracer.event(
                        "reflection_override",
                        from_action=verdict.next_action,
                        to_action="stop",
                        reason=f"quality_report passed={qpassed} >= target={target}",
                    )
                self._graceful_stop = True
                self._stop_requested = True
                return
            if verdict.should_stop:
                logger.info("reflector stopped the loop: %s", verdict.reason)
                self._stop_requested = True
                return
            if verdict.next_action == "replan":
                self._replan_history.append(verdict.reason)
                if _same_reason_streak(self._replan_history) >= MAX_REPLAN_SAME_REASON:
                    self._force_focus = _extract_focus(goal)
                    if self.tracer:
                        self.tracer.event(
                            "oscillation_break",
                            streak=MAX_REPLAN_SAME_REASON,
                            reason=verdict.reason,
                        )
                self._replan_requested = True
                return
            # continue
            self._need_more = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("reflection failed (continuing): %s", exc)

    def _persist_crawl_result(self, result: dict[str, Any]) -> None:
        """缺陷8: 把 crawl_notes 结果落盘 data/raw/,防止数据丢失并支持复用。"""
        try:
            items = result.get("items")
            if not items:
                return
            out_dir = Path("data/raw")
            out_dir.mkdir(parents=True, exist_ok=True)
            run_id = getattr(self.tracer, "run_id", None) or "unknown"
            path = out_dir / f"crawled_notes_{run_id}.json"
            path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("persisted %d crawled notes to %s", len(items), path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to persist crawl result: %s", exc)

    def _track_crawl_result(self, result: Any) -> None:
        """13.1/13.2: circuit-break empty crawl results and honour user cancel.

        First empty result is allowed to flow into reflection (which usually
        replans a re-crawl); a second consecutive empty result circuit-breaks the
        run instead of letting it oscillate for minutes per round.
        """
        if not isinstance(result, dict):
            return
        if result.get("cancelled"):
            logger.info("crawl_notes cancelled by stop; stopping run")
            self._stop_requested = True
            return
        count = int(result.get("count") or 0)
        if count > 0:
            self._crawl_empty_calls = 0
            # 缺陷8: 爬取成功立即落盘,避免后续步骤失败(如 analyze 报错)导致
            # 好不容易爬到的数据只存在于内存/事件流中被丢弃。
            self._persist_crawl_result(result)
            # R3 (memory-rag-selection): 爬取笔记逐条入 RAG 知识库, 支撑后续
            # 知识问答。doc_id 由 url(或内容)哈希 -> 重爬/重放幂等。失败不致命。
            try:
                import hashlib

                from app.agent.knowledge import get_knowledge_store

                store = get_knowledge_store()
                goal = getattr(self, "_current_goal", "") or ""
                for item in result.get("items", []) or []:
                    if not isinstance(item, dict) or not item.get("content"):
                        continue
                    text = " ".join(
                        str(p) for p in (
                            item.get("title", ""), item.get("content", ""),
                            " ".join(item.get("tags", []) or []),
                        ) if p
                    )
                    url = str(item.get("url") or "")
                    doc_id = hashlib.sha1(
                        (url or text[:200]).encode("utf-8")
                    ).hexdigest()[:12]
                    store.add(
                        text,
                        title=str(item.get("title") or "")[:60],
                        source="crawl",
                        metadata={"url": url, "goal": goal},
                        doc_id=doc_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("knowledge ingest for crawl skipped: %s", exc)
            return
        self._crawl_empty_calls += 1
        if self.tracer:
            self.tracer.event(
                "crawl_empty", empty_calls=self._crawl_empty_calls, tool="crawl_notes"
            )
        if self._crawl_empty_calls > MAX_CRAWL_EMPTY_RETRIES:
            logger.warning(
                "crawl_notes returned 0 items %d times; circuit-breaking the run",
                self._crawl_empty_calls,
            )
            if self.tracer:
                self.tracer.event("crawl_stalled", empty_calls=self._crawl_empty_calls)
            self._stop_requested = True

    async def _refine_contents(self, result: Any, step: PlanStep) -> Any:
        """Evaluator-Optimizer: refine generated drafts before accepting them."""
        if not isinstance(result, dict) or "contents" not in result:
            return result
        items = result.get("contents") or []
        if not items:
            return result
        logger.info("running quality gate on %d drafts", len(items))
        focus = list(step.params.get("focus_keywords") or [])
        revised, report = await self.quality_gate.run(items, focus_keywords=focus)
        result["contents"] = revised
        result["quality_report"] = report
        step.summary = _summarize(result)
        logger.info("quality gate report: %s", report)
        if self.tracer:
            self.tracer.event("quality", report=report)
        if self.guardrails:
            blocked = []
            for item in revised:
                text = " ".join(
                    [
                        str(item.get("title", "")),
                        str(item.get("body", "")),
                        " ".join(item.get("hashtags", []) or []),
                    ]
                )
                if not self.guardrails.check_output(text).allowed:
                    blocked.append(item)
            if blocked:
                result["guardrail_blocked"] = [
                    b.get("title") for b in blocked
                ]
                if self.tracer:
                    self.tracer.event(
                        "guardrail_blocked", guard_type="output", count=len(blocked)
                    )
        return result
