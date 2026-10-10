"""Governance tests: admin-token auth on management endpoints, audit log,
and secret scanning of traces (optimization plan 10.3 / P2-c)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_housekeeping_requires_admin_token_when_configured(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.agent import audit

    monkeypatch.setattr(settings, "admin_token", "s3cret")
    monkeypatch.setattr(audit, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(
        "app.agent.housekeeping.purge_old_runs",
        lambda **kwargs: {"removed": 0, "kept_summaries": 0},
    )

    assert client.post("/agent/housekeeping", json={}).status_code == 403
    assert (
        client.post("/agent/housekeeping", json={}, headers={"X-Admin-Token": "nope"}).status_code
        == 403
    )
    ok = client.post(
        "/agent/housekeeping", json={}, headers={"X-Admin-Token": "s3cret"}
    )
    assert ok.status_code == 200


def test_stop_requires_admin_token_when_configured(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_token", "s3cret")
    # 403 is raised before hitting run_manager, so no real run is needed.
    assert client.post("/agent/runs/whatever/stop").status_code == 403
    assert client.post("/agent/runs/whatever/resume").status_code == 403


def test_management_endpoints_open_when_no_token_configured():
    # default admin_token is empty -> no auth enforced
    assert client.post("/agent/runs/doesnotexist/stop").status_code == 404


def test_audit_log_appended_on_management_action(tmp_path, monkeypatch):
    from app.agent import audit
    from app.agent.run_manager import AgentRunManager

    log = tmp_path / "audit.log"
    monkeypatch.setattr(audit, "AUDIT_LOG", log)
    audit.audit("stop", run_id="abc", detail="test")
    assert log.exists()
    assert "stop" in log.read_text(encoding="utf-8")
    assert "abc" in log.read_text(encoding="utf-8")


def test_scan_for_secrets_detects_leaks(tmp_path):
    from app.agent.secrets import scan_for_secrets

    d = tmp_path / "runs"
    d.mkdir()
    (d / "a.json").write_text('{"run_id": "a"}', encoding="utf-8")
    (d / "b.json").write_text('{"result": {"openai_api_key": "sk-leak"}}', encoding="utf-8")

    hits = scan_for_secrets([d], ["sk-leak"])
    assert len(hits) == 1
    assert hits[0].endswith("b.json")

    nested = d / "sub"
    nested.mkdir()
    (nested / "c.json").write_text('{"key": "sk-leak"}', encoding="utf-8")
    hits = scan_for_secrets([d], ["sk-leak"])
    assert len(hits) == 2


def test_no_secret_in_real_traces():
    """Best-effort: real trace dirs must not contain the configured API key."""
    from app.core.config import settings
    from app.agent.secrets import scan_for_secrets
    from app.agent.tracing import RUNS_DIR
    from app.agent.housekeeping import CONSOLE_RUNS_DIR, CHECKPOINT_DIR

    key = settings.openai_api_key
    if key:
        hits = scan_for_secrets([RUNS_DIR, CONSOLE_RUNS_DIR, CHECKPOINT_DIR], [key])
        assert hits == []