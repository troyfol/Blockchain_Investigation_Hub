"""Case templates (P26/FN-22): a declarative preset pre-seeds a NEW case's methodology + scenario
connectors — settings/metadata ONLY, never a fabricated fact (Invariants #1/#3). A from-scratch case is
entirely unaffected. Isolated from the real app-data dir / cases root via env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.db import get_connection
from backend.app.services import cases, settings_store
from backend.app.services.case_templates import get_template, list_templates


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.delenv("BIH_CASE_DB", raising=False)
    cases.clear_active_case()
    cases.register_native_window(None)
    yield
    cases.clear_active_case()
    cases.register_native_window(None)


def test_new_from_template_preseeds():
    res = cases.new_case("Lazarus sanctions", template="sanctions-tracing")
    tmpl = get_template("sanctions-tracing")

    conn = get_connection(Path(res["path"]))
    try:
        # The per-case methodology stub landed in the case's own case_meta.description.
        assert conn.execute("SELECT description FROM case_meta").fetchone()[0] == tmpl["description"]
        # A template pre-seeds SETTINGS/metadata ONLY — it fabricates NO facts (Invariants #1/#3): a fresh
        # templated case has zero addresses/transactions/transfers and no source_query.
        for table in ("address", "transaction_", "transfer", "source_query"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()

    # The scenario connector was enabled app-wide (a settings toggle, not a fact).
    assert settings_store.paid_enabled_override("chainalysis") is True
    # The first-ingest bound hint is echoed back to the caller (UI pre-fill; not persisted).
    assert res["template"]["id"] == "sanctions-tracing"
    assert res["template"]["default_bounds"] == tmpl["default_bounds"]


def test_from_scratch_case_is_unaffected():
    res = cases.new_case("Plain case")                       # no template
    conn = get_connection(Path(res["path"]))
    try:
        assert conn.execute("SELECT description FROM case_meta").fetchone()[0] is None
    finally:
        conn.close()
    assert "template" not in res
    assert settings_store.paid_enabled_override("chainalysis") is None   # nothing enabled


def test_unknown_template_is_rejected():
    with pytest.raises(ValueError, match="unknown case template"):
        cases.new_case("Bad", template="does-not-exist")


def test_templates_are_declarative_and_extensible():
    templates = list_templates()
    assert len(templates) >= 2
    for t in templates:
        assert {"id", "name", "description", "connectors", "default_bounds"} <= set(t)
    assert get_template("sanctions-tracing") is not None and get_template("nope") is None
    # list_templates / get_template return COPIES — mutating one must not corrupt the registry.
    list_templates()[0]["name"] = "MUTATED"
    assert get_template(templates[0]["id"])["name"] != "MUTATED"
