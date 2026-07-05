"""Case-management entry UI — backend tests (P4).

Covers the runtime active-case lifecycle + registry + verify-before-open import:
  * new -> open -> import round-trip (the imported copy becomes active),
  * switching the active case checkpoint-releases the PRIOR case's WAL (no leaked handle),
  * a tampered .casefile is reported failed and NOT opened,
  * the Recent list prunes a path that's gone + ``forget`` drops a case from the list without deleting
    it on disk,
  * the native file-dialog kind->FileDialog mapping (confirmed pywebview 6.2.1 shape) + the dialog
    endpoint reporting 501 in browser/dev mode.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.audits.runner import run_audits
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.main import app
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.export import export_case


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the real per-user app-data dir / cases root, never inherit a BIH_CASE_DB, and reset
    the in-process active case + native-window between tests."""
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.delenv("BIH_CASE_DB", raising=False)
    from backend.app.services import cases

    cases.clear_active_case()
    cases.register_native_window(None)
    yield
    cases.clear_active_case()
    cases.register_native_window(None)


@pytest.fixture
def client():
    return TestClient(app)


def _seed_case(conn) -> None:
    """A small but real case: a final BTC tx + output, a raw-response provenance file, so export +
    re-verify exercise the self-contained bundle path."""
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="a" * 64, block_height=800000, block_ts="2026-01-01T00:00:00Z",
            confirmations=20, finality_status="final"), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display="1Seed"), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx, address_id=a, amount="100", output_index=0), sqid)

    write_with_provenance(conn, sq, write, raw_response={"txs": [{"txid": "a" * 64}]})


def _export_source_case(case_dir: Path, *, title: str = "Source Case") -> Path:
    """Build, seed, audit-baseline, and export a case to a ``.casefile``. Returns the bundle path."""
    db = case_dir / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title=title)
    _seed_case(conn)
    run_audits(db_path=str(db))  # establish the .audit_baselines sidecar (shipped + re-verified)
    conn.close()
    return export_case(case_dir)


def _tamper_bundle(bundle: Path, out: Path) -> Path:
    """Rewrite a ``.casefile`` with one byte of ``case.db`` flipped but the manifest left intact —
    exactly the tamper verify_manifest must catch (hash mismatch)."""
    with zipfile.ZipFile(bundle) as z:
        names = z.namelist()
        data = {n: z.read(n) for n in names}
    blob = bytearray(data["case.db"])
    blob[-1] ^= 0xFF
    data["case.db"] = bytes(blob)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, data[n])
    return out


# --------------------------------------------------------------------------- new / open / import

@pytest.mark.smoke
def test_new_open_import_roundtrip(client, tmp_path):
    # New (via API) -> active.
    created = client.post("/api/cases/new", json={"title": "Fresh Case"})
    assert created.status_code == 200, created.text
    assert created.json()["active"]["title"] == "Fresh Case"
    active = client.get("/api/cases/active").json()["active"]
    assert active and active["title"] == "Fresh Case"
    new_path = active["path"]
    assert Path(new_path).exists()

    # Import a verified bundle -> opens the imported COPY (a different path) and becomes active.
    bundle = _export_source_case(tmp_path / "source")
    imp = client.post("/api/cases/import", json={"path": str(bundle)}).json()
    assert imp["ok"] and imp["opened"] and imp["trusted"]
    assert imp["verification"]["ok"]
    assert imp["active"]["title"] == "Source Case"
    assert Path(imp["active"]["path"]) != Path(new_path)  # the imported copy, not the new case

    # Open the first case back by path.
    opened = client.post("/api/cases/open", json={"path": new_path}).json()
    assert opened["ok"] and opened["active"]["title"] == "Fresh Case"

    # Recent lists both known cases, most-recent first (the just-opened "Fresh Case" atop).
    cases = client.get("/api/cases").json()["cases"]
    titles = [c.get("title") for c in cases]
    assert "Fresh Case" in titles and "Source Case" in titles
    assert titles[0] == "Fresh Case"


def test_import_upload_browser_fallback(client, tmp_path):
    """The dev/browser path: a .casefile uploaded as the raw request body imports the same way."""
    bundle = _export_source_case(tmp_path / "source")
    data = bundle.read_bytes()
    r = client.post("/api/cases/import-upload?filename=source.casefile", content=data)
    body = r.json()
    assert body["opened"] and body["verification"]["ok"]
    assert body["active"]["title"] == "Source Case"


@pytest.mark.smoke
def test_tampered_casefile_is_reported_and_not_opened(client, tmp_path):
    bundle = _export_source_case(tmp_path / "source")
    tampered = _tamper_bundle(bundle, tmp_path / "tampered.casefile")

    imp = client.post("/api/cases/import", json={"path": str(tampered)}).json()
    assert imp["ok"] is False and imp["opened"] is False     # verification gate held
    assert imp["manifest_ok"] is False                       # a hash mismatch -> TAMPER (altered bytes)
    assert imp["verification"]["ok"] is False
    assert "case.db" in imp["verification"]["manifest"]["mismatched"]
    assert imp["active"] is None
    # No case was opened -> still no active case (the picker stays).
    assert client.get("/api/cases/active").json()["active"] is None


def test_import_audit_drift_is_authentic_not_tamper(client, tmp_path):
    """An authentic bundle (every hash matches) whose ONLY problem is an invariant-audit failure (a
    post-finality drift, e.g. the known cases/live 0007 drift) must report manifest_ok=True /
    audits_passed=False — an integrity warning, NOT tampering. The two claims are kept distinct."""
    from backend.tests.integration.test_seeded_case import seed_evm_transfer

    db = tmp_path / "drift" / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Drift Case")
    seed_evm_transfer(conn, final=True)                      # a FINAL evm transfer
    run_audits(db_path=str(db))                              # baseline records the final rows (passes)
    conn.execute("UPDATE transfer SET amount='999999999'")   # mutate a FINAL row AFTER the baseline
    conn.commit()
    conn.close()
    bundle = export_case(db.parent)                          # ships the drifted DB + the pre-drift baseline

    imp = client.post("/api/cases/import", json={"path": str(bundle)}).json()
    assert imp["manifest_ok"] is True                        # hashes all match -> AUTHENTIC, not tampered
    assert imp["self_contained_ok"] is True                  # structurally self-contained
    assert imp["audits_passed"] is False                     # ...but a final-immutability invariant warning
    assert imp["opened"] is False                            # gated behind open-anyway like any non-clean bundle
    assert "final-immutability" in imp["verification"]["self_contained"]["failed_audits"]

    # Open-anyway lands it as authentic-but-untrusted (the loud explicit override).
    imp2 = client.post("/api/cases/import",
                       json={"path": str(bundle), "allow_untrusted": True}).json()
    assert imp2["opened"] is True and imp2["trusted"] is False


def test_open_anyway_imports_untrusted(client, tmp_path):
    """A failed bundle is openable only behind the explicit untrusted flag, and lands as trusted=False."""
    bundle = _export_source_case(tmp_path / "source")
    tampered = _tamper_bundle(bundle, tmp_path / "tampered.casefile")

    imp = client.post("/api/cases/import",
                      json={"path": str(tampered), "allow_untrusted": True}).json()
    assert imp["opened"] is True and imp["trusted"] is False
    # The Recent entry is flagged untrusted so the UI can mark it.
    entry = next(c for c in client.get("/api/cases").json()["cases"]
                 if Path(c["path"]) == Path(imp["active"]["path"]))
    assert entry["trusted"] is False


def test_open_rejects_nonexistent_and_non_bih_db(client, tmp_path):
    assert client.post("/api/cases/open", json={"path": str(tmp_path / "nope" / "case.db")}
                       ).status_code == 404
    junk = tmp_path / "random.db"
    sqlite3.connect(junk).close()  # a valid sqlite file with no case_meta
    assert client.post("/api/cases/open", json={"path": str(junk)}).status_code == 400


# --------------------------------------------------------------------------- active-case switch / WAL

def test_switch_releases_prior_case(monkeypatch):
    """Switching the active case checkpoint-releases the PRIOR case exactly once; re-activating the same
    case does not. (Connections are per-request, so this is the 'no leaked handle' guarantee.)"""
    from backend.app.services import cases

    released: list[str] = []
    real = cases._checkpoint_release
    monkeypatch.setattr(cases, "_checkpoint_release",
                        lambda p: (released.append(str(Path(p).resolve())), real(p))[-1])

    a = cases.new_case("Alpha")["path"]
    cases.set_active_case(a)                 # re-activate the SAME case -> no release
    assert released == []
    b = cases.new_case("Beta")["path"]       # switch A -> B -> release A once
    assert released == [a]
    assert cases.active_case_path() == b


def test_switch_checkpoints_prior_wal(monkeypatch, tmp_path):
    """The prior case's ``-wal`` is flushed+truncated on switch even while another connection is still
    open to it (so SQLite would NOT auto-clear it) — proving the explicit checkpoint runs."""
    from backend.app.services import cases

    a = cases.new_case("Alpha")["path"]
    wal = Path(a + "-wal")
    hold = get_connection(a)  # keep a connection open so SQLite can't auto-checkpoint on last-close
    try:
        hold.execute("UPDATE case_meta SET description=?", ("x" * 4000,))  # grow the WAL
        assert wal.exists() and wal.stat().st_size > 0
        cases.new_case("Beta")  # switch -> checkpoint(TRUNCATE) on Alpha
        assert wal.stat().st_size == 0  # flushed despite `hold` still open
    finally:
        hold.close()


# --------------------------------------------------------------------------- registry: prune / forget

def test_recent_list_prunes_missing_path(client, tmp_path):
    import shutil

    from backend.app.services import cases

    a = cases.new_case("Keep")["path"]
    b = cases.new_case("Gone")["path"]
    assert {Path(a), Path(b)} <= {Path(c["path"]) for c in client.get("/api/cases").json()["cases"]}

    shutil.rmtree(Path(b).parent)  # the case folder vanishes (moved/deleted)
    paths = {Path(c["path"]) for c in client.get("/api/cases").json()["cases"]}
    assert Path(a) in paths and Path(b) not in paths  # pruned on read


def test_forget_removes_from_list_but_not_disk(client):
    from backend.app.services import cases

    a = cases.new_case("Forgettable")["path"]
    assert any(Path(c["path"]) == Path(a) for c in client.get("/api/cases").json()["cases"])

    resp = client.post("/api/cases/forget", json={"path": a}).json()
    assert resp["removed"] is True
    assert all(Path(c["path"]) != Path(a) for c in resp["cases"])
    assert Path(a).exists()  # 'remove from list' is NOT 'delete the case on disk'


# --------------------------------------------------------------------------- native file dialog

def test_pick_path_maps_kinds_and_normalizes_result():
    webview = pytest.importorskip("webview")  # native dialogs need pywebview (the 'app' extra) — skip without it

    from backend.app.services import dialogs

    calls: list = []

    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            calls.append((dialog_type, kwargs))
            return ("C:/cases/x.casefile",)

    w = FakeWindow()
    assert dialogs.pick_path(w, "casefile") == ["C:/cases/x.casefile"]
    assert calls[-1][0] == webview.FileDialog.OPEN and "file_types" in calls[-1][1]
    dialogs.pick_path(w, "folder")
    assert calls[-1][0] == webview.FileDialog.FOLDER

    class CancelWindow:
        def create_file_dialog(self, *a, **k):
            return None  # user cancelled

    assert dialogs.pick_path(CancelWindow(), "casedb") == []
    with pytest.raises(ValueError):
        dialogs.pick_path(w, "bogus")


def test_dialog_endpoint_501_in_browser_mode(client):
    # No native window registered (dev/browser) -> the endpoint reports unavailable, UI falls back.
    r = client.post("/api/dialog/pick", json={"kind": "casefile"})
    assert r.status_code == 501


def test_dialog_endpoint_runs_registered_window(client):
    pytest.importorskip("webview")  # the endpoint calls dialogs.pick_path -> import webview (pywebview absent in CI)
    from backend.app.services import cases

    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            return ("C:/picked/case.casefile",)

    cases.register_native_window(FakeWindow())
    r = client.post("/api/dialog/pick", json={"kind": "casefile"})
    assert r.status_code == 200 and r.json()["paths"] == ["C:/picked/case.casefile"]
