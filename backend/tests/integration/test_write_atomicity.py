"""Batch 11 (RES-03 / RES-04): provenance-file + export write atomicity.

- RES-03: `write_with_provenance` promotes the raw file BEFORE the DB commit (fsync'd rename), so a
  committed `source_query` never references a not-yet-promoted file; a sweep clears `.tmp` stragglers and a
  read-only check reports any `raw_response_ref` with no on-disk file.
- RES-04: `export_case` writes the `.casefile` (and `manifest.json`) via a temp path + atomic rename, so a
  crash mid-zip never leaves a truncated bundle at the expected name.
"""

from __future__ import annotations

import pytest

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxOutput
from backend.app.provenance import atomic
from backend.app.provenance.atomic import (RAW_SUBDIR, orphan_raw_refs, sweep_stale_raw_tmp,
                                           write_with_provenance)
from backend.app.services import export
from backend.tests.integration._helpers import new_case


def _sq():
    return SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                       params={"address": "probe", "bounds": "default"},
                       requested_at="2026-01-01T00:00:00Z", status="ok")


def test_res03_success_promotes_file_no_tmp(tmp_path):
    conn, db = new_case(tmp_path)

    def w(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)

    sqid, _ = write_with_provenance(conn, _sq(), w, raw_response={"txs": []})
    raw = tmp_path / RAW_SUBDIR / f"{sqid}.json"
    assert raw.exists(), "raw file not promoted on success (RES-03)"
    assert not list((tmp_path / RAW_SUBDIR).glob("*.tmp")), "a .tmp straggler was left behind"
    assert orphan_raw_refs(conn) == []  # the committed ref has its file
    conn.close()


def test_res03_failure_leaves_no_row_and_no_file(tmp_path):
    conn, db = new_case(tmp_path)

    def boom(c, sqid):
        raise RuntimeError("write failed")

    with pytest.raises(RuntimeError):
        write_with_provenance(conn, _sq(), boom, raw_response={"txs": []})
    # No committed source_query, and neither the staged .tmp nor a promoted file remains.
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 0
    raw_dir = tmp_path / RAW_SUBDIR
    assert not raw_dir.exists() or not any(raw_dir.iterdir()), "a raw file survived a rolled-back write"
    conn.close()


def test_res03_sweep_and_orphan_report(tmp_path):
    conn, db = new_case(tmp_path)
    raw_dir = tmp_path / RAW_SUBDIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "stale.json.tmp").write_bytes(b"{}")
    assert sweep_stale_raw_tmp(tmp_path) == 1
    assert not list(raw_dir.glob("*.tmp"))

    # A committed source_query whose file is missing is reported as an orphan.
    def w(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)

    sqid, _ = write_with_provenance(conn, _sq(), w, raw_response={"x": 1})
    (raw_dir / f"{sqid}.json").unlink()  # simulate a torn write: row committed, file gone
    assert orphan_raw_refs(conn) == [f"{RAW_SUBDIR}/{sqid}.json"]
    conn.close()


def _seed_exportable(conn):
    def w(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        tx = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="a" * 64, block_height=1,
                                     confirmations=50, finality_status="final"), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display="1S"), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx, address_id=a, amount="100", output_index=0), sqid)

    write_with_provenance(conn, _sq(), w, raw_response={"txs": [{"txid": "a" * 64}]})


def test_res04_export_is_atomic_on_success(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    conn, db = new_case(case_dir)  # creates case.db under case_dir
    _seed_exportable(conn)
    conn.close()
    out = export.export_case(case_dir)
    assert out.exists() and out.suffix == ".casefile"
    assert not list(case_dir.parent.glob("*.casefile.tmp")), "an export .tmp leaked on success (RES-04)"
    assert not list(case_dir.glob("*.json.tmp")), "a manifest .tmp leaked on success (RES-04)"


def test_res04_export_leaves_no_truncated_bundle_on_failure(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    conn, db = new_case(case_dir)
    _seed_exportable(conn)
    conn.close()

    # Fail DURING the zip (the 2nd use of _iter_case_files; the 1st is build_manifest).
    real = export._iter_case_files
    calls = {"n": 0}

    def flaky(cd):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated crash mid-zip")
        yield from real(cd)

    monkeypatch.setattr(export, "_iter_case_files", flaky)
    out_path = case_dir.parent / f"{case_dir.name}.casefile"
    with pytest.raises(RuntimeError):
        export.export_case(case_dir)
    assert not out_path.exists(), "a truncated .casefile was left at the final name (RES-04)"
    assert not list(case_dir.parent.glob("*.casefile.tmp")), "the export .tmp was not cleaned up"
