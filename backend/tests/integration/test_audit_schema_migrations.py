"""BASE-02 (docs/review/FINDINGS.md): cross-run audit baselines must survive forward-only schema
migrations without false tamper alarms.

The R0 review baseline caught ``cases/live`` failing ``final-immutability`` on ALL 2,362 final
transfers: migration 0007 added ``transfer.occurrence`` AFTER the baseline was recorded, and the
row hash covers ``SELECT t.*`` — so a legitimate schema migration changed every hash,
indistinguishable from tampering, with no sanctioned recovery path.

These tests pin the schema-aware baseline (format 2):

* a forward-only migration that changes an audited table **re-establishes** the baseline loudly
  (cross-schema row hashes are not comparable — pretending otherwise is the false alarm);
* an audited-table schema change **without** a new migration still FAILS (possible tampering);
* a legacy (pre-format-2) baseline still compares rows, upgrades in place when clean, and on a
  mismatch names the explicit ``--rebaseline`` escape hatch instead of leaving a dead end;
* genuine row tampering is still caught after every transition (the guarantee the check exists for).
"""

from __future__ import annotations

import json

import pytest

from backend.app.audits.baselines import default_baseline_dir
from backend.app.audits.runner import run_audits
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, Attribution, SourceQuery, Transaction, Transfer
from backend.app.normalization.finality import finality_for
from backend.app.provenance.atomic import write_with_provenance

ETH_FROM = "0x52908400098527886E0F7030069857D2E4169EE7"
ETH_TO = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"


@pytest.fixture
def case(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Schema Migration Audit Case")
    yield conn, db
    conn.close()


def seed_final_transfer(conn) -> dict:
    sq = SourceQuery(
        connector="etherscan", capability="get_transactions", endpoint="account/txlist",
        params={"address": ETH_TO, "bounds": "default"},
        requested_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z", status="ok",
    )

    def write(c, sqid):
        asset_id = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        from_id = repo.upsert_address(c, Address(chain="ethereum", address_display=ETH_FROM), sqid)
        to_id = repo.upsert_address(c, Address(chain="ethereum", address_display=ETH_TO), sqid)
        conf, status = finality_for(tip_height=1000, block_height=900, threshold=64)
        tx_id = repo.upsert_transaction(c, Transaction(
            chain="ethereum", tx_hash="0xevmtx1", block_height=900,
            block_ts="2026-01-01T00:00:00Z", fee="210000000000000", status="1",
            confirmations=conf, finality_status=status), sqid)
        transfer_id = repo.upsert_transfer(c, Transfer(
            transaction_id=tx_id, chain="ethereum", from_address_id=from_id, to_address_id=to_id,
            asset_id=asset_id, amount="1000000000000000000", transfer_type="native", position=0), sqid)
        return {"to_id": to_id, "tx_id": tx_id, "transfer_id": transfer_id}

    _, ids = write_with_provenance(conn, sq, write,
                                   raw_response={"status": "1", "result": [{"hash": "0xevmtx1"}]})
    return ids


def seed_attribution(conn, address_id) -> str:
    sq = SourceQuery(connector="arkham-import", capability="get_attributions", endpoint="import",
                     params={"file": "arkham_export.csv", "bounds": "default"},
                     requested_at="2026-01-03T00:00:00Z", status="ok")

    def write(c, sqid):
        return repo.insert_attribution(c, Attribution(
            address_id=address_id, label="Binance", category="exchange", source="arkham",
            confidence=0.9, retrieved_at="2026-01-03T00:00:00Z"), sqid)

    _, aid = write_with_provenance(conn, sq, write, raw_response="raw,csv,bytes")
    return aid


def _result(results, name):
    return next(r for r in results if r.name == name)


def _apply_fake_migration(conn, migration_id: str, ddl: str) -> None:
    """Simulate a FUTURE forward-only migration: the DDL plus the yoyo bookkeeping row, exactly the
    state a real ``make migrate`` leaves behind."""
    conn.execute(ddl)
    conn.execute(
        "INSERT INTO _yoyo_migration (migration_hash, migration_id, applied_at_utc) VALUES (?,?,?)",
        (f"testhash-{migration_id}", migration_id, "2026-07-01T00:00:00"),
    )
    conn.commit()


def _rewrite_sidecar_as_legacy(db) -> None:
    """Rewrite a check's sidecar to the pre-format-2 layout (bare rows, no schema metadata) — the
    exact on-disk state every pre-fix case DB carries."""
    sidecar = default_baseline_dir(db) / "final-immutability.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    rows = data["rows"] if isinstance(data, dict) and data.get("format") == 2 else data
    sidecar.write_text(json.dumps(rows), encoding="utf-8")


# --------------------------------------------------------------------------- final-immutability


def test_migration_on_audited_table_rebaselines_instead_of_false_alarm(case):
    conn, db = case
    seed_final_transfer(conn)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed  # baseline recorded

    _apply_fake_migration(conn, "9998_widen_transfer",
                          "ALTER TABLE transfer ADD COLUMN test_col TEXT")

    fi = _result(run_audits(db_path=str(db)), "final-immutability")
    assert fi.passed, f"schema migration wrongly flagged as tampering: {fi.offending[:3]}"
    assert "re-established" in fi.detail

    # The next plain run compares normally again — and still catches REAL tampering.
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed
    conn.execute("UPDATE transfer SET amount='666'")
    conn.commit()
    fi2 = _result(run_audits(db_path=str(db)), "final-immutability")
    assert not fi2.passed
    assert any("modified" in str(o) for o in fi2.offending)


def test_schema_change_without_migration_still_fails(case):
    conn, db = case
    seed_final_transfer(conn)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed

    conn.execute("ALTER TABLE transfer ADD COLUMN sneaky TEXT")  # NO _yoyo_migration row
    conn.commit()

    fi = _result(run_audits(db_path=str(db)), "final-immutability")
    assert not fi.passed
    blob = fi.detail + json.dumps(fi.offending, default=str)
    assert "schema" in blob and "migration" in blob


def test_legacy_rows_only_baseline_upgrades_when_rows_match(case):
    conn, db = case
    seed_final_transfer(conn)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed
    _rewrite_sidecar_as_legacy(db)

    fi = _result(run_audits(db_path=str(db)), "final-immutability")
    assert fi.passed

    sidecar = default_baseline_dir(db) / "final-immutability.json"
    upgraded = json.loads(sidecar.read_text(encoding="utf-8"))
    assert upgraded.get("format") == 2  # advanced to the schema-aware format on the passing run


def test_legacy_baseline_after_schema_migration_fails_and_names_the_escape_hatch(case):
    # A pre-format-2 baseline can't be adjudicated across a schema change — it must still FAIL
    # (never silently forgive), but the failure must point the operator at the explicit,
    # verify-first --rebaseline path rather than leave a dead end.
    conn, db = case
    seed_final_transfer(conn)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed
    _rewrite_sidecar_as_legacy(db)
    _apply_fake_migration(conn, "9998_widen_transfer",
                          "ALTER TABLE transfer ADD COLUMN test_col TEXT")

    fi = _result(run_audits(db_path=str(db)), "final-immutability")
    assert not fi.passed
    assert "--rebaseline" in fi.detail


def test_runner_rebaseline_flag_reestablishes_explicitly(case):
    conn, db = case
    seed_final_transfer(conn)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed
    _rewrite_sidecar_as_legacy(db)
    _apply_fake_migration(conn, "9998_widen_transfer",
                          "ALTER TABLE transfer ADD COLUMN test_col TEXT")
    assert not _result(run_audits(db_path=str(db)), "final-immutability").passed

    # Operator verified row content out-of-band → explicit, targeted re-baseline. The row content
    # genuinely changed (the fake migration widened `transfer`), so P27 reports it as an operator
    # re-baseline that advances the in-DB anchor to the re-verified state (not a first-time record).
    fi = _result(run_audits(db_path=str(db), rebaseline=["final-immutability"]),
                 "final-immutability")
    assert fi.passed and "re-baseline" in fi.detail
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed  # green thereafter

    # And the re-established baseline still catches real tampering.
    conn.execute("UPDATE transfer SET amount='666'")
    conn.commit()
    assert not _result(run_audits(db_path=str(db)), "final-immutability").passed


# --------------------------------------------------------------------------- append-only-claims


def test_claims_baseline_survives_migration_and_still_catches_deletion(case):
    conn, db = case
    ids = seed_final_transfer(conn)
    aid = seed_attribution(conn, ids["to_id"])
    assert _result(run_audits(db_path=str(db)), "append-only-claims").passed  # baseline

    _apply_fake_migration(conn, "9997_widen_attribution",
                          "ALTER TABLE attribution ADD COLUMN test_col TEXT")

    ao = _result(run_audits(db_path=str(db)), "append-only-claims")
    assert ao.passed, f"schema migration wrongly flagged as claim rewrite: {ao.offending[:3]}"
    assert "re-established" in ao.detail

    # The invariant the check exists for still holds after the re-baseline.
    conn.execute("DELETE FROM attribution WHERE id=?", (aid,))
    conn.commit()
    ao2 = _result(run_audits(db_path=str(db)), "append-only-claims")
    assert not ao2.passed
    assert any("deleted" in str(o) for o in ao2.offending)
