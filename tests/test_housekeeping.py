"""Tests for housekeeping TTL purging (optimization plan 10.1 / D7)."""

from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.agent.housekeeping import purge_old_runs


client = TestClient(app)


def test_purge_old_runs_removes_stale_keeps_summary(tmp_path):
    trace = tmp_path / "runs"
    trace.mkdir()
    old = trace / "old.json"
    fresh = trace / "fresh.json"
    old.write_text(
        json.dumps(
            {
                "run_id": "old",
                "goal": "g",
                "status": "completed",
                "result": {"results": [1]},
            }
        ),
        encoding="utf-8",
    )
    fresh.write_text(json.dumps({"run_id": "fresh"}), encoding="utf-8")

    now = datetime.now().timestamp()
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))
    os.utime(fresh, (now, now))

    result = purge_old_runs(retention_days=30, dirs=[trace])
    assert result["removed"] == 1
    assert result["kept_summaries"] == 1
    assert not (trace / "old.json").exists()
    assert (trace / "old.summary.json").exists()
    assert (trace / "fresh.json").exists()


def test_purge_fresh_files_untouched(tmp_path):
    trace = tmp_path / "runs"
    trace.mkdir()
    path = trace / "recent.json"
    path.write_text(json.dumps({"run_id": "recent"}), encoding="utf-8")
    now = datetime.now().timestamp()
    os.utime(path, (now, now))

    result = purge_old_runs(retention_days=30, dirs=[trace])
    assert result["removed"] == 0
    assert (trace / "recent.json").exists()


def test_purge_removes_stale_summary_and_tmp_files(tmp_path):
    """13.x: 冷区也必须有边界 —— summary.json 双倍保留期后清除,崩溃残留 .tmp
    达保留期即清;summary 永存/tmp 永存都违背 TTL 初衷。"""
    trace = tmp_path / "runs"
    trace.mkdir()
    now = datetime.now().timestamp()

    old_run = trace / "old.json"          # 40d run -> 清除,留 summary
    old_summary_src = trace / "ancient.summary.json"  # 70d summary (>2x30) -> 清除
    keep_summary_src = trace / "aged.summary.json"    # 45d summary (<2x30) -> 保留
    stale_tmp = trace / "crashed.json.tmp"            # 40d tmp -> 清除
    fresh_tmp = trace / "fresh.json.tmp"              # 0d tmp -> 保留
    for p in (old_run, old_summary_src, keep_summary_src, stale_tmp, fresh_tmp):
        p.write_text("{}", encoding="utf-8")
    for p, days in (
        (old_run, 40), (old_summary_src, 70), (keep_summary_src, 45),
        (stale_tmp, 40), (fresh_tmp, 0),
    ):
        os.utime(p, (now - days * 86400, now - days * 86400))

    result = purge_old_runs(retention_days=30, dirs=[trace])
    assert not old_run.exists()
    assert (trace / "old.summary.json").exists()   # run 清除后 summary 生成
    assert not old_summary_src.exists()            # 超双倍保留期的 summary 清除
    assert keep_summary_src.exists()               # 宽限期内 summary 保留
    assert not stale_tmp.exists()                  # 崩溃残留 tmp 清除
    assert fresh_tmp.exists()                      # 新 tmp 保留
    assert result["removed"] == 3


def test_housekeeping_endpoint_calls_purge(monkeypatch):
    captured = {}

    def fake_purge(**kwargs):
        captured.update(kwargs)
        return {"removed": 5, "kept_summaries": 5}

    monkeypatch.setattr("app.agent.housekeeping.purge_old_runs", fake_purge)
    response = client.post(
        "/agent/housekeeping",
        json={"retention_days": 60, "keep_summary": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["removed"] == 5
    assert captured["retention_days"] == 60
    assert captured["keep_summary"] is False