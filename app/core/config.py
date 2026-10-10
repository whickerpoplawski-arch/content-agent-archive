import json
import os
import re
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _load_codex_config() -> dict:
    """Load LLM settings (model / base_url / api_key) from Codex so this project
    stays consistent with the Codex agent the user already configured.
    Priority in Settings stays: env vars > .env > these Codex defaults.
    """
    out = {"openai_model": "", "openai_base_url": "", "openai_api_key": ""}
    try:
        text = (CODEX_HOME / "config.toml").read_text(encoding="utf-8")
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        if m:
            out["openai_model"] = m.group(1)
        mb = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', text, re.M)
        if mb:
            out["openai_base_url"] = mb.group(1)
    except Exception:
        pass
    try:
        auth = json.loads((CODEX_HOME / "auth.json").read_text(encoding="utf-8"))
        out["openai_api_key"] = auth.get("OPENAI_API_KEY", "")
    except Exception:
        pass
    return out


_CODEX = _load_codex_config()


class Settings(BaseSettings):
    app_name: str = "XHS Content Agent"
    app_version: str = "0.1.0"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000

    # LLM 配置：默认与 Codex 保持一致（config.toml + auth.json），env/.env 优先。
    openai_api_key: str = _CODEX.get("openai_api_key") or ""
    openai_model: str = _CODEX.get("openai_model") or "deepseek-v4-flash"
    openai_temperature: float = 0.7
    openai_base_url: Optional[str] = _CODEX.get("openai_base_url") or None

    # 文生图模型
    image_model: str = "gpt-image-1"
    image_size: str = "1024x1024"
    image_output_dir: str = "data/output/images"

    # 飞书多维表格
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_app_token: str = ""
    feishu_table_id: str = ""
    feishu_publish_table_id: str = ""

    # 小红书 MCP 服务地址
    xhs_mcp_url: str = "http://localhost:18060"
    xhs_mcp_endpoint: str = "http://localhost:18060/mcp"
    xhs_mcp_binary: str = ""

    # Agent 配置
    agent_llm_backend: str = "openai"   # openai | mock（mock 用于离线测试/演示）
    agent_max_retries: int = 3
    agent_max_steps: int = 50           # 跨 replan 的总执行步数预算；0=不限制
    agent_max_llm_calls: int = 0        # 0=不限制；超限自动停机
    agent_max_est_tokens: int = 0       # 0=不限制；估算 token 预算
    agent_max_replans: int = 0          # 0=不限制；连续 replan 次数上限（防振荡死循环）
    agent_collect_strategy: str = "best_n"  # all | last_round | best_n（产物收集策略）
    agent_top_n: int = 3                # best_n / last_round 时保留的产物上限

    # RAG 知识注入 v2（调研规划 M1）：小库 digest 直注，大库走检索；注入预算防 Context Rot
    knowledge_digest_max_docs: int = 30         # 文档数 <= 此值时全库 digest 直注入 planner
    knowledge_context_budget_chars: int = 800   # planner 知识注入字符预算（≈400 CJK token）

    # 爬虫（optimization plan 12.8）：爬取默认无头；登录仍用有头窗口
    crawl_headless: bool = True

    # 风控规避（docs/crawl-anti-detection.md）：请求节流与浏览器指纹
    crawl_delay_min: float = 3.0        # 详情页访问间隔下限(秒)
    crawl_delay_max: float = 6.0        # 详情页访问间隔上限(秒)
    crawl_search_delay_min: float = 4.0 # 搜索页间隔下限(秒)
    crawl_search_delay_max: float = 8.0 # 搜索页间隔上限(秒)
    crawl_keyword_cooldown_min: float = 8.0   # 多关键词之间冷却下限(秒)
    crawl_keyword_cooldown_max: float = 15.0  # 多关键词之间冷却上限(秒)
    crawl_burst_size: int = 8           # 连续访问 N 页后插入一次长休息
    crawl_burst_rest_min: float = 15.0  # 长休息下限(秒)
    crawl_burst_rest_max: float = 30.0  # 长休息上限(秒)

    # Housekeeping (plan 10.1): purge stale run artifacts
    agent_housekeeping_retention_days: int = 30
    agent_housekeeping_auto: bool = True        # 阶段5: 默认开启,每次 run 后必触发清理

    # Governance (plan 10.3): optional token required for management endpoints
    admin_token: str = ""               # 置空则本地不鉴权；设置后 stop/resume/housekeeping 需 X-Admin-Token

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("debug", "crawl_headless", mode="before")
    @classmethod
    def _coerce_bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "debug")
        return v


settings = Settings()
