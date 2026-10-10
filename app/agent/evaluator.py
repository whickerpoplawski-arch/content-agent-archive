"""Evaluator-Optimizer loop (Anthropic "Building Effective Agents" pattern).

One LLM call evaluates a generated draft against a rubric; if it falls below the
bar, a second call revises it, and the loop repeats. This is the within-run
self-improvement mechanism behind "自主学习 / 迭代改进": the Agent does not ship a
draft unchanged, it critiques and refines it until it passes the quality gate.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.agent.llm import LLMBackend, generate_structured
from app.agent.schemas import ContentEvaluation

logger = logging.getLogger("agent.evaluator")

RUBRIC = """小红书图文质量评分标准（每项 0-10）：
1. 标题吸引力：是否有数字/痛点/悬念，避免机翻堆词
2. 正文口语化：真实、自然，像真人分享而非官方文案
3. 结构清晰：有明确的段落与可读性
4. 字数控制：正文 150-300 字
5. 话题标签：3-6 个，不重复
6. CTA 引导：有明确互动引导
7. 配图建议：image_suggestion 具体可执行
8. 目标相关性：标题/正文命中 focus_keywords 至少 1 个，未跑题
"""

EVALUATE_TEMPLATE = """你是小红书内容质量评审。
{RUBRIC}

目标域关键词（标题/正文须命中至少一个；为空则忽略）：
{focus_keywords}

请评估下面这篇图文并给出：
- score: 0-10 总分
- verdict: "pass"(>=7) 或 "revise"(<7)
- issues: 具体、可执行的改进点列表（若 pass 可留空）

内容（JSON）：
{content}
"""

OPTIMIZE_TEMPLATE = """你是小红书资深运营，根据评审意见优化这篇图文，使其达到 7 分以上。
保持内容类型与主题不变，只改表达、结构、标签与配图建议。

评审意见：
{issues}

原文（JSON）：
{content}

返回优化后的内容 JSON，字段与原文一致：title, body, hashtags, cta, image_suggestion, content_type。
"""


class Evaluator:
    def __init__(self, backend: LLMBackend) -> None:
        self._backend = backend

    async def evaluate(
        self, content: dict[str, Any], focus_keywords: Optional[list[str]] = None
    ) -> ContentEvaluation:
        focus = ", ".join(focus_keywords or []) or "（无）"
        prompt = EVALUATE_TEMPLATE.format(
            RUBRIC=RUBRIC,
            focus_keywords=focus,
            content=json.dumps(content, ensure_ascii=False),
        )
        return await generate_structured(prompt, ContentEvaluation, backend=self._backend)


class Optimizer:
    def __init__(self, backend: LLMBackend) -> None:
        self._backend = backend

    async def optimize(
        self, content: dict[str, Any], issues: list[str]
    ) -> dict[str, Any]:
        from app.agent.schemas import ContentDraft

        prompt = OPTIMIZE_TEMPLATE.format(
            issues="\n- " + "\n- ".join(issues), content=json.dumps(content, ensure_ascii=False)
        )
        return (await generate_structured(prompt, ContentDraft, backend=self._backend)).model_dump()


class QualityGate:
    """Runs evaluator-optimizer until every draft passes or rounds are exhausted."""

    def __init__(
        self,
        evaluator: Evaluator,
        optimizer: Optimizer,
        *,
        min_score: int = 7,
        max_rounds: int = 2,
    ) -> None:
        self.evaluator = evaluator
        self.optimizer = optimizer
        self.min_score = min_score
        self.max_rounds = max_rounds

    async def run(
        self,
        contents: list[dict[str, Any]],
        focus_keywords: Optional[list[str]] = None,
    ) -> tuple[list[dict[str, Any]], dict]:
        revised: list[dict[str, Any]] = []
        report: dict[str, Any] = {"evaluated": 0, "optimized": 0, "passed": 0}
        for item in contents:
            current = dict(item)
            score, rounds = 0, 0
            while rounds < self.max_rounds:
                rounds += 1
                eval_res = await self.evaluator.evaluate(current, focus_keywords)
                report["evaluated"] += 1
                score = eval_res.score
                if eval_res.verdict == "pass" or score >= self.min_score:
                    break
                report["optimized"] += 1
                current = await self.optimizer.optimize(current, eval_res.issues)
            if score >= self.min_score:
                report["passed"] += 1
            # per-item score so downstream dedupe/rank/progress can compare items
            current["_score"] = score
            current["_verdict"] = "pass" if score >= self.min_score else "revise"
            revised.append(current)
        return revised, report
