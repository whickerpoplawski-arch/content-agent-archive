"""Agent-internal schemas for planning, execution, and reflection."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    """A single executable step: an action bound to a registered tool."""

    action: str = Field(..., description="Name of the registered tool to call")
    params: dict[str, Any] = Field(default_factory=dict, description="Tool parameters")
    rationale: str = Field("", description="Why this step is needed")
    status: StepStatus = StepStatus.PENDING
    summary: str = ""
    error: Optional[str] = None


class Plan(BaseModel):
    """An ordered list of steps to achieve a goal."""

    goal: str = ""
    steps: list[PlanStep] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return all(s.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED) for s in self.steps)

    @property
    def failed_step(self) -> Optional[PlanStep]:
        for s in self.steps:
            if s.status == StepStatus.FAILED:
                return s
        return None


class Reflection(BaseModel):
    """Model output deciding how the Agent should continue after a step."""

    should_stop: bool = Field(False, description="True if the goal is met and we should stop")
    next_action: str = Field("continue", description="continue | replan | retry | stop")
    reason: str = Field("", description="Short justification")


class Recovery(BaseModel):
    """Model output proposing how to recover from a failed step."""

    next_action: str = Field("replan", description="retry | replan | stop")
    adjusted_params: dict[str, Any] = Field(default_factory=dict, description="New params for retry")
    reason: str = Field("", description="Short justification")


class ToolSpec(BaseModel):
    """Description of a registered tool, used to build the planner prompt."""

    name: str
    description: str
    params_schema_name: str
    params_schema_json: str = "{}"



class ContentEvaluation(BaseModel):
    """LLM quality evaluation of a generated content item (Evaluator-Optimizer)."""
    score: int = Field(..., ge=0, le=10, description="0-10 quality score")
    verdict: str = Field("pass", description="pass | revise")
    issues: list[str] = Field(default_factory=list, description="Specific improvement points")



class ContentDraft(BaseModel):
    """A draft content item used by the Evaluator-Optimizer loop."""
    title: str
    body: str
    hashtags: list[str]
    cta: str
    image_suggestion: str
    content_type: str


class UserFacts(BaseModel):
    """阶段2: 从一轮 run 的 goal + 产物中抽取的用户事实(自动事实提取)."""
    target_domains: list[str] = Field(
        default_factory=list, description="用户的目标领域/主题/受众"
    )
    preferred_tone: str = Field("", description="用户偏好的语气/风格")
    preferred_structure: str = Field("", description="用户偏好的内容结构")
    avoid_topics: list[str] = Field(
        default_factory=list, description="用户想避免的主题/表达"
    )
