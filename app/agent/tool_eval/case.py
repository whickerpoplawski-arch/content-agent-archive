"""Pydantic cases for the frozen tool-calling eval set.

Loads ``data/evals/tool_calling_v0.jsonl`` and checks its sha256 sidecar.
Does not score traces or call tools.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.tools import DEMO_ALLOWLIST

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVAL_PATH = REPO_ROOT / "data" / "evals" / "tool_calling_v0.jsonl"
DEFAULT_SHA_PATH = REPO_ROOT / "data" / "evals" / "tool_calling_v0.sha256"
DEFAULT_SNAPSHOT_PATH = REPO_ROOT / "data" / "interim" / "kb_snapshot_v0.json"

_ID_RE = re.compile(r"^tc_\d{3}$")
_ALLOWLIST = set(DEMO_ALLOWLIST)


class Bucket(str, Enum):
    must_retrieve = "must_retrieve"
    no_retrieve_ok = "no_retrieve_ok"
    tool_empty = "tool_empty"
    refuse = "refuse"
    clarify = "clarify"


class ExpectStatus(str, Enum):
    ok = "ok"
    degraded = "degraded"
    refuse = "refuse"
    clarify = "clarify"


class ParamCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    field: str
    contains_any: Optional[list[str]] = None
    forbid_any: Optional[list[str]] = None
    equals: Optional[Any] = None
    intersects: Optional[list[str]] = None
    intersects_theme: Optional[bool] = None

    @model_validator(mode="after")
    def _has_operator(self) -> "ParamCheck":
        if not any(
            value is not None
            for value in (
                self.contains_any,
                self.forbid_any,
                self.equals,
                self.intersects,
                self.intersects_theme,
            )
        ):
            raise ValueError("param check needs contains_any, forbid_any, equals, intersects, or intersects_theme")
        if self.tool not in _ALLOWLIST:
            raise ValueError(f"param check tool {self.tool!r} is outside the demo allowlist")
        return self


class ExpectedTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    must_call: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    order_soft: list[str] = Field(default_factory=list)
    max_tool_calls: Optional[int] = None
    param_checks: list[ParamCheck] = Field(default_factory=list)
    grounding: Optional[str] = None
    honesty: Optional[str] = None
    expect_no_tools: bool = False

    @field_validator("grounding")
    @classmethod
    def _grounding(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in {"each_angle_grounded", "none_required"}:
            raise ValueError("grounding must be each_angle_grounded or none_required")
        return value

    @field_validator("honesty")
    @classmethod
    def _honesty(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value != "no_fake_metrics":
            raise ValueError("honesty must be no_fake_metrics")
        return value

    @model_validator(mode="after")
    def _call_rule(self) -> "ExpectedTrace":
        if self.expect_no_tools and self.must_call:
            raise ValueError("expect_no_tools requires an empty must_call")
        if not self.expect_no_tools and not self.must_call:
            raise ValueError("must_call is required unless expect_no_tools is true")
        unknown = [name for name in self.must_call if name not in _ALLOWLIST]
        if unknown:
            raise ValueError(f"must_call outside the demo allowlist: {unknown}")
        return self


class ExpectAngles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=0)
    max: int = Field(ge=0)
    require_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _range(self) -> "ExpectAngles":
        if self.min > self.max:
            raise ValueError("expect_angles.min cannot exceed max")
        allowed = {"angle", "title", "why", "audience", "grounded_from"}
        unknown = [name for name in self.require_fields if name not in allowed]
        if unknown:
            raise ValueError(f"unknown angle fields: {unknown}")
        return self


class GroundSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    span: str

    @field_validator("doc_id", "span")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("doc_id and span must be non-empty")
        return value


class ToolEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bucket: Bucket
    theme: str
    audience: Optional[str] = None
    count: Optional[int] = None
    allow_tools: Optional[list[str]] = None
    kb: str
    expect_status: ExpectStatus
    expected_trace: ExpectedTrace
    expect_angles: ExpectAngles
    must_ground_in: Optional[list[GroundSpan]] = None
    forbid_regex: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("id must look like tc_001")
        return value

    @field_validator("kb")
    @classmethod
    def _kb(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("kb is required")
        return value

    @model_validator(mode="after")
    def _bucket_rules(self) -> "ToolEvalCase":
        if self.allow_tools:
            bad = [name for name in self.allow_tools if name not in _ALLOWLIST]
            if bad:
                raise ValueError(f"allow_tools outside the demo allowlist: {bad}")
        trace = self.expected_trace
        if self.bucket in {Bucket.refuse, Bucket.clarify}:
            if not trace.expect_no_tools:
                raise ValueError(f"{self.id} {self.bucket.value} must set expect_no_tools")
            if self.expect_status.value != self.bucket.value:
                raise ValueError(f"{self.id} expect_status must match {self.bucket.value}")
        if self.bucket == Bucket.must_retrieve:
            missing = {"knowledge_search", "generate_topics"} - set(trace.must_call)
            if missing:
                raise ValueError(f"{self.id} must_retrieve missing {sorted(missing)}")
            if self.expect_status != ExpectStatus.ok:
                raise ValueError(f"{self.id} must_retrieve expect_status must be ok")
            if trace.grounding != "each_angle_grounded":
                raise ValueError(f"{self.id} must_retrieve grounding must be each_angle_grounded")
        if self.bucket == Bucket.no_retrieve_ok:
            if self.allow_tools != ["generate_topics"]:
                raise ValueError(f"{self.id} no_retrieve_ok allow_tools must be [generate_topics]")
            if "generate_topics" not in trace.must_call:
                raise ValueError(f"{self.id} no_retrieve_ok must call generate_topics")
            if "knowledge_search" not in trace.must_not_call:
                raise ValueError(f"{self.id} no_retrieve_ok must forbid knowledge_search")
        if self.bucket == Bucket.tool_empty:
            if "knowledge_search" not in trace.must_call:
                raise ValueError(f"{self.id} tool_empty must call knowledge_search")
            if trace.grounding != "none_required":
                raise ValueError(f"{self.id} tool_empty grounding must be none_required")
            if trace.honesty != "no_fake_metrics":
                raise ValueError(f"{self.id} tool_empty honesty must be no_fake_metrics")
            if self.expect_status not in {ExpectStatus.ok, ExpectStatus.degraded}:
                raise ValueError(f"{self.id} tool_empty expect_status must be ok or degraded")
        return self


def load_cases(path: Path | str = DEFAULT_EVAL_PATH) -> list[ToolEvalCase]:
    """Load every JSONL row. Raises if a row fails the schema."""
    cases: list[ToolEvalCase] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(ToolEvalCase.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case id")
    return cases


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def sha_sidecar_path(eval_path: Path | str) -> Path:
    path = Path(eval_path)
    return path.with_suffix(".sha256")


def read_sha256(sha_path: Path | str) -> str:
    token = Path(sha_path).read_text(encoding="utf-8").strip().split()
    if not token:
        raise ValueError(f"empty sha file: {sha_path}")
    return token[0]


def verify_sha256(eval_path: Path | str = DEFAULT_EVAL_PATH, sha_path: Path | str | None = None) -> str:
    """Return the digest when the sidecar matches. Raise ValueError otherwise."""
    eval_file = Path(eval_path)
    sidecar = Path(sha_path) if sha_path is not None else sha_sidecar_path(eval_file)
    expected = read_sha256(sidecar)
    actual = file_sha256(eval_file)
    if expected != actual:
        raise ValueError(f"sha256 mismatch for {eval_file.name}: expected {expected} got {actual}")
    return actual


def load_snapshot(path: Path | str = DEFAULT_SNAPSHOT_PATH) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("docs"), list):
        raise ValueError(f"snapshot missing docs: {path}")
    return payload
