"""Guardrails: block unsafe goals, side-effect tools, and non-compliant output.

As the Agent becomes more autonomous it should be able to refuse harmful goals,
refuse risky side-effect tools, and catch non-compliant generated copy before it
is published (optimization plan P1-a / section 4). The existing human approval
flow for publish_to_xhs / sync_to_feishu is preserved; guardrails run in addition
to it. Blocked actions are recorded as `guardrail_blocked` trace events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GuardrailResult:
    allowed: bool
    reason: str = ""
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "details": self.details}


class Guardrails:
    # Goals an autonomous agent should never pursue.
    SENSITIVE_GOAL_KEYWORDS = [
        "黑稿",
        "贬低",
        "诋毁",
        "抄袭",
        "扒皮",
        "违法",
        "违规",
        "假货",
        "刷单",
        "赌博",
        "色情",
        "毒品",
        "代开发票",
    ]
    # Side-effect tools that additionally require human approval (unchanged).
    APPROVAL_TOOLS = {"publish_to_xhs", "sync_to_feishu"}
    # 广告法禁用词 / 绝对化用语 (simplified rule table).
    # P0-2: 不再用单字「最」(会误杀「最新/最近」等正常词),改为「最+程度形容词」
    # 的上下文正则 + 明确词组表。
    AD_LAW_BANNED = [
        "第一",
        "顶级",
        "国家级",
        "最佳",
        "唯一",
        "绝无仅有",
        "万能",
        "全网最低",
        "销量第一",
    ]
    # 「最X」绝对化:覆盖「最好/最大/最新/最便宜」等,但不匹配「最近/最早/最先」。
    AD_LAW_PATTERNS = [
        r"最(?:好|大|高|强|低|优|新|全|专业|权威|领先|优惠|便宜|实惠|受欢迎|具性价比|值得)",
    ]
    # 内容合规敏感词 (violates platform compliance).
    CONTENT_BLOCKLIST = ["赌博", "色情", "毒品", "代开发票", "刷单"]

    def __init__(
        self,
        sensitive_goal_keywords: Optional[list[str]] = None,
        blocked_tools: Optional[list[str]] = None,
        ad_law_banned: Optional[list[str]] = None,
        content_blocklist: Optional[list[str]] = None,
        ad_law_patterns: Optional[list[str]] = None,
    ) -> None:
        self.sensitive_goal_keywords = (
            sensitive_goal_keywords if sensitive_goal_keywords is not None
            else list(self.SENSITIVE_GOAL_KEYWORDS)
        )
        self.blocked_tools = set(blocked_tools or [])
        self.ad_law_banned = ad_law_banned if ad_law_banned is not None else list(self.AD_LAW_BANNED)
        self.content_blocklist = (
            content_blocklist if content_blocklist is not None
            else list(self.CONTENT_BLOCKLIST)
        )
        self.ad_law_patterns = (
            ad_law_patterns if ad_law_patterns is not None
            else list(self.AD_LAW_PATTERNS)
        )
        self._compiled = [re.compile(p) for p in self.ad_law_patterns]

    def check_goal(self, goal: str) -> GuardrailResult:
        hits = [kw for kw in self.sensitive_goal_keywords if kw and kw in goal]
        if hits:
            return GuardrailResult(False, "sensitive_goal", hits)
        return GuardrailResult(True)

    def check_tool(self, action: str) -> GuardrailResult:
        if action in self.blocked_tools:
            return GuardrailResult(False, "blocked_tool", [action])
        return GuardrailResult(True)

    def check_output(self, text: str) -> GuardrailResult:
        """Return blocked if the copy contains ad-law banned or compliance words.

        P0-2: 词组子串 + 「最+程度形容词」上下文正则。单字「最」不再误杀
        「最近/最早」等正常词;命中返回实际匹配词供 tripwire 阻断用。
        """
        if not text:
            return GuardrailResult(True)
        hits = [
            w
            for w in (self.ad_law_banned + self.content_blocklist)
            if w and w in text
        ]
        for pattern in self._compiled:
            hits.extend(m.group(0) for m in pattern.finditer(text))
        # 去重保序
        seen = set()
        unique = [h for h in hits if not (h in seen or seen.add(h))]
        if unique:
            return GuardrailResult(False, "non_compliant_output", unique)
        return GuardrailResult(True)