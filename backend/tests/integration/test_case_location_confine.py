"""Batch 2 (SEC-06): a caller-supplied case ``location`` / import ``dest_root`` must be confined to the
cases root, so a same-origin script can't write a case DB (or extract a bundle) to an arbitrary
process-writable path (e.g. a Windows Startup folder = persistence foothold)."""

from __future__ import annotations

import pytest

from backend.app.services import cases


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.delenv("BIH_CASE_DB", raising=False)
    cases.clear_active_case()
    yield
    cases.clear_active_case()


def test_new_case_rejects_location_outside_cases_root(tmp_path):
    outside = tmp_path / "startup"  # a sibling of cases_root, NOT inside it
    outside.mkdir()
    with pytest.raises(ValueError):
        cases.new_case("Evil", location=str(outside))


def test_new_case_allows_default_and_inside_location(tmp_path):
    # Default (None) -> cases_root: fine.
    res = cases.new_case("Ordinary")
    assert res["created"]
    # An explicit location INSIDE the cases root is allowed.
    inside = tmp_path / "cases_root" / "sub"
    res2 = cases.new_case("Nested", location=str(inside))
    assert res2["created"]


def test_import_rejects_dest_root_outside_cases_root(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    # A bogus path is fine — confinement is checked before the file is even read.
    with pytest.raises(ValueError):
        cases.import_casefile(tmp_path / "whatever.casefile", dest_root=str(outside))
