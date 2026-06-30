"""Unit tests for the audit harness: baseline store + runner context plumbing (phase_00).

The harness ships with zero real checks in Phase 0, so these tests exercise the framework
itself with a synthetic check to prove Phase 1 can plug checks in cleanly.
"""

from __future__ import annotations

from backend.app.audits import AuditContext, AuditResult, audit_check
from backend.app.audits.baselines import BaselineStore, default_baseline_dir
from backend.app.audits import runner as runner_mod


def test_baseline_store_round_trip(tmp_path):
    store = BaselineStore(tmp_path / ".audit_baselines")
    assert store.read("final-immutability") is None
    assert store.exists("final-immutability") is False
    store.write("final-immutability", {"checksum": "abc", "rows": 3})
    assert store.exists("final-immutability") is True
    assert store.read("final-immutability") == {"checksum": "abc", "rows": 3}
    # Overwrite is atomic + last-writer-wins.
    store.write("final-immutability", {"checksum": "def", "rows": 4})
    assert store.read("final-immutability")["checksum"] == "def"


def test_default_baseline_dir_is_sidecar(tmp_path):
    db = tmp_path / "case.db"
    assert default_baseline_dir(db) == (tmp_path / ".audit_baselines").resolve()


def test_runner_no_checks_is_green_noop(monkeypatch):
    # With NO checks registered the runner is a green no-op regardless of db arg. (Phase 1
    # registers real checks, so force an empty discovery to test the framework behaviour.)
    monkeypatch.setattr(runner_mod, "discover_checks", lambda: [])
    assert runner_mod.run_audits(db_path=None) == []


def test_runner_passes_context_and_aggregates(tmp_path, monkeypatch):
    db = tmp_path / "case.db"
    db.touch()
    seen = {}

    @audit_check("synthetic-pass")
    def _passing(ctx: AuditContext) -> AuditResult:
        seen["conn"] = ctx.conn
        seen["db_path"] = ctx.db_path
        # The check can persist a baseline through the context.
        ctx.baselines.write("synthetic-pass", {"ok": True})
        return AuditResult(name="synthetic-pass", passed=True)

    @audit_check("synthetic-fail")
    def _failing(ctx: AuditContext) -> AuditResult:
        return AuditResult(name="synthetic-fail", passed=False, offending=[{"row": 1}])

    # Inject synthetic checks (deterministically sorted by name in the runner).
    monkeypatch.setattr(runner_mod, "discover_checks", lambda: [_failing, _passing])

    results = runner_mod.run_audits(db_path=str(db))
    by_name = {r.name: r for r in results}
    assert by_name["synthetic-pass"].passed is True
    assert by_name["synthetic-fail"].passed is False
    assert by_name["synthetic-fail"].offending == [{"row": 1}]
    # Context was wired: db_path threaded through and baseline persisted to the sidecar.
    assert seen["db_path"] == db
    assert (tmp_path / ".audit_baselines" / "synthetic-pass.json").exists()
    # Report returns False (a failure present) -> non-zero exit path.
    assert runner_mod._print_report(results) is False


def test_runner_treats_raised_check_as_failure(tmp_path, monkeypatch):
    db = tmp_path / "case.db"
    db.touch()

    @audit_check("boom")
    def _boom(ctx: AuditContext) -> AuditResult:
        raise ValueError("kaboom")

    monkeypatch.setattr(runner_mod, "discover_checks", lambda: [_boom])
    results = runner_mod.run_audits(db_path=str(db))
    assert results[0].passed is False
    assert "kaboom" in results[0].detail
