"""Session-wide test isolation.

Tests that drive mock Agent runs call LongTermMemory() with the default path
(data/agent/profile.json). Without isolation every test run pollutes the real
user profile (audience/tone overwritten, episodic hits inflated, user_facts from
test goals). Redirect the default profile path to a session tmp file instead.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_profile(tmp_path_factory):
    from app.agent.memory import LongTermMemory
    from app.agent import memory_store

    tmp_dir = tmp_path_factory.mktemp("profile")
    tmp = tmp_dir / "profile.json"
    # LongTermMemory() 的默认 path 在类定义时绑定 PROFILE_FILE,改 __defaults__ 兜住。
    defaults = list(LongTermMemory.__init__.__defaults__)
    defaults[0] = tmp
    LongTermMemory.__init__.__defaults__ = tuple(defaults)
    # 工具/HTTP 未绑 run store 时也落到这份临时库，避免写到 data/agent/memory.db。
    memory_store.DEFAULT_DB_PATH = tmp_dir / "memory.db"
    yield tmp
