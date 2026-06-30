"""Phase 1 seeded-case integration tests (phase_01 acceptance).

Hand-seed a case via the provenance writer + repository, then prove the invariant audits hold:
provenance completeness, no dangling FKs, idempotency, final-immutability, append-only claims,
cache-provenance carried. Also proves the failure paths (a fact cannot be written without a
source_query; a mutated final row / a deleted claim are caught).
"""

from __future__ import annotations

import hashlib

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.db.migrate import CURRENT_SCHEMA_VERSION, read_schema_version
from backend.app.db.shared_cache import copy_address_claim_into_case, migrate_cache
from backend.app.models import (
    Address,
    Asset,
    Attribution,
    SourceQuery,
    Transaction,
    Transfer,
    TxInput,
    TxOutput,
)
from backend.app.normalization.finality import finality_for
from backend.app.provenance.atomic import write_with_provenance

# Valid EIP-55 checksummed EVM addresses and real Bitcoin address encodings.
ETH_FROM = "0x52908400098527886E0F7030069857D2E4169EE7"
ETH_TO = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"
BTC_IN = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BTC_OUT_BECH32 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
BTC_OUT_P2SH = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"

FACT_TABLES = ["asset", "address", "transaction_", "transfer", "tx_output", "tx_input"]


@pytest.fixture
def case(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Seeded Test Case")
    yield conn, db
    conn.close()


# --------------------------------------------------------------------------- seed helpers

def seed_evm_transfer(conn, *, final: bool = True) -> dict:
    sq = SourceQuery(
        connector="etherscan", capability="get_transactions", endpoint="account/txlist",
        params={"address": ETH_TO, "bounds": "default"},
        requested_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z", status="ok",
    )
    block = 900 if final else 999

    def write(c, sqid):
        asset_id = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        from_id = repo.upsert_address(c, Address(chain="ethereum", address_display=ETH_FROM), sqid)
        to_id = repo.upsert_address(c, Address(chain="ethereum", address_display=ETH_TO), sqid)
        conf, status = finality_for(tip_height=1000, block_height=block, threshold=64)
        tx_id = repo.upsert_transaction(c, Transaction(
            chain="ethereum", tx_hash="0xevmtx1", block_height=block,
            block_ts="2026-01-01T00:00:00Z", fee="210000000000000", status="1",
            confirmations=conf, finality_status=status), sqid)
        transfer_id = repo.upsert_transfer(c, Transfer(
            transaction_id=tx_id, chain="ethereum", from_address_id=from_id, to_address_id=to_id,
            asset_id=asset_id, amount="1000000000000000000", transfer_type="native", position=0), sqid)
        return {"asset_id": asset_id, "from_id": from_id, "to_id": to_id,
                "tx_id": tx_id, "transfer_id": transfer_id}

    _, ids = write_with_provenance(conn, sq, write,
                                   raw_response={"status": "1", "result": [{"hash": "0xevmtx1"}]})
    return ids


def seed_btc_tx(conn, *, final: bool = True) -> dict:
    sq = SourceQuery(
        connector="esplora", capability="get_transactions", endpoint="tx",
        params={"address": BTC_OUT_BECH32, "bounds": "default"},
        requested_at="2026-01-02T00:00:00Z", status="ok",
    )
    block = 799990 if final else 799999

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        in_id = repo.upsert_address(c, Address(chain="bitcoin", address_display=BTC_IN), sqid)
        out1 = repo.upsert_address(c, Address(chain="bitcoin", address_display=BTC_OUT_BECH32), sqid)
        out2 = repo.upsert_address(c, Address(chain="bitcoin", address_display=BTC_OUT_P2SH), sqid)
        conf, status = finality_for(tip_height=800000, block_height=block, threshold=6)
        tx_id = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="btctx1", block_height=block,
            block_ts="2026-01-02T00:00:00Z", fee="1000",
            confirmations=conf, finality_status=status), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx_id, address_id=in_id,
                                        amount="150000", input_index=0), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx_id, address_id=in_id,
                                        amount="50000", input_index=1), sqid)
        o1 = repo.upsert_tx_output(c, TxOutput(transaction_id=tx_id, address_id=out1,
                                               amount="120000", output_index=0), sqid)
        o2 = repo.upsert_tx_output(c, TxOutput(transaction_id=tx_id, address_id=out2,
                                               amount="79000", output_index=1), sqid)
        return {"tx_id": tx_id, "in_id": in_id, "out1": out1, "out2": out2, "o1": o1, "o2": o2}

    _, ids = write_with_provenance(conn, sq, write, raw_response={"txid": "btctx1"})
    return ids


def seed_attribution(conn, address_id, *, source="arkham") -> str:
    sq = SourceQuery(connector="arkham-import", capability="get_attributions", endpoint="import",
                     params={"file": "arkham_export.csv", "bounds": "default"},
                     requested_at="2026-01-03T00:00:00Z", status="ok")

    def write(c, sqid):
        return repo.insert_attribution(c, Attribution(
            address_id=address_id, label="Binance", category="exchange", source=source,
            confidence=0.9, retrieved_at="2026-01-03T00:00:00Z"), sqid)

    _, aid = write_with_provenance(conn, sq, write, raw_response="raw,csv,bytes")
    return aid


def _counts(conn, tables) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _result(results, name):
    return next(r for r in results if r.name == name)


# --------------------------------------------------------------------------- tests

@pytest.mark.smoke
def test_seeded_case_all_audits_green(case):
    conn, db = case
    assert read_schema_version(db) == CURRENT_SCHEMA_VERSION  # init_case set it to 1

    ev = seed_evm_transfer(conn)
    seed_btc_tx(conn)
    seed_attribution(conn, ev["to_id"])

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    results = run_audits(db_path=str(db))
    failed = [(r.name, r.offending) for r in results if not r.passed]
    assert failed == [], f"audits failed: {failed}"
    assert {r.name for r in results} == {
        "provenance-completeness", "cache-provenance-carried", "no-dangling-fk",
        "idempotency", "final-immutability", "append-only-claims", "bounds-recorded",
        "no-fabricated-utxo-edge", "valuation-subject-validity", "entity-resolution-sanity",
    }


@pytest.mark.smoke
def test_value_movement_view_shapes(case):
    conn, _ = case
    seed_evm_transfer(conn)
    seed_btc_tx(conn)
    # EVM rows carry a src; UTXO rows MUST have NULL src (Invariant #5; the audit in Phase 3).
    utxo_with_src = conn.execute(
        "SELECT COUNT(*) FROM v_value_movement WHERE paradigm='utxo' AND src_address_id IS NOT NULL"
    ).fetchone()[0]
    assert utxo_with_src == 0
    evm = conn.execute("SELECT COUNT(*) FROM v_value_movement WHERE paradigm='evm'").fetchone()[0]
    utxo = conn.execute("SELECT COUNT(*) FROM v_value_movement WHERE paradigm='utxo'").fetchone()[0]
    assert evm == 1 and utxo == 2  # one transfer; two outputs


def test_idempotent_reingest(case):
    conn, db = case
    seed_evm_transfer(conn)
    before = _counts(conn, FACT_TABLES)
    seed_evm_transfer(conn)  # identical facts, a new source_query call
    after = _counts(conn, FACT_TABLES)
    assert before == after  # upsert on natural keys — no duplicates
    results = run_audits(db_path=str(db))
    assert _result(results, "idempotency").passed


def test_fact_cannot_be_written_without_source_query(case):
    conn, _ = case
    with pytest.raises(ValueError):
        repo.upsert_asset(conn, Asset(chain="ethereum", symbol="ETH", decimals=18), None)
    with pytest.raises(ValueError):
        repo.upsert_address(conn, Address(chain="ethereum", address_display=ETH_FROM), None)


def test_final_transaction_refetch_is_a_noop(case):
    conn, _ = case
    ids = seed_evm_transfer(conn, final=True)
    # Re-ingest with a DIFFERENT fee — the repo guard must keep the final row frozen.
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="account/txlist",
                     params={"bounds": "default"}, requested_at="2026-01-01T01:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_transaction(c, Transaction(
            chain="ethereum", tx_hash="0xevmtx1", block_height=900, block_ts="2026-01-01T00:00:00Z",
            fee="999", status="0", confirmations=200, finality_status="final"), sqid)

    write_with_provenance(conn, sq, write, raw_response={})
    fee = conn.execute("SELECT fee, status FROM transaction_ WHERE id=?", (ids["tx_id"],)).fetchone()
    assert fee["fee"] == "210000000000000" and fee["status"] == "1"  # unchanged


def test_final_immutability_detects_tampering(case):
    conn, db = case
    ids = seed_evm_transfer(conn, final=True)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed  # records baseline
    # Tamper directly, bypassing the repo guard.
    conn.execute("UPDATE transaction_ SET fee='666' WHERE id=?", (ids["tx_id"],))
    fi = _result(run_audits(db_path=str(db)), "final-immutability")
    assert not fi.passed
    assert any("modified" in str(o) for o in fi.offending)


def test_append_only_detects_claim_deletion(case):
    conn, db = case
    ev = seed_evm_transfer(conn)
    aid = seed_attribution(conn, ev["to_id"])
    assert _result(run_audits(db_path=str(db)), "append-only-claims").passed  # baseline
    conn.execute("DELETE FROM attribution WHERE id=?", (aid,))
    ao = _result(run_audits(db_path=str(db)), "append-only-claims")
    assert not ao.passed


def test_append_only_detects_claim_rewrite(case):
    # Invariant #4: claims are never rewritten in place — an UPDATE must be caught, not just a DELETE.
    conn, db = case
    ev = seed_evm_transfer(conn)
    aid = seed_attribution(conn, ev["to_id"])
    assert _result(run_audits(db_path=str(db)), "append-only-claims").passed  # baseline
    conn.execute("UPDATE attribution SET label='Tampered' WHERE id=?", (aid,))
    ao = _result(run_audits(db_path=str(db)), "append-only-claims")
    assert not ao.passed
    assert any("rewritten" in str(o) for o in ao.offending)


def test_final_immutability_allows_tx_input_linkage_refresh(case):
    # docs/schema.md §4: tx_input upsert refreshes prev_output linkage — a legitimate post-final
    # update that must NOT trip the immutability audit (mirrors tx_output.spent exclusion).
    conn, db = case
    btc = seed_btc_tx(conn, final=True)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed  # baseline
    inp = conn.execute(
        "SELECT id FROM tx_input WHERE transaction_id=? AND input_index=0", (btc["tx_id"],)
    ).fetchone()
    conn.execute("UPDATE tx_input SET prev_output_id=?, address_id=? WHERE id=?",
                 (btc["o1"], btc["out1"], inp["id"]))
    fi = _result(run_audits(db_path=str(db)), "final-immutability")
    assert fi.passed, f"linkage refresh wrongly flagged as tampering: {fi.offending}"


def test_cache_copy_carries_provenance(case, tmp_path):
    conn, db = case
    seed_evm_transfer(conn)  # case now contains the ETH_TO address

    cache_path = tmp_path / "library_cache.db"
    migrate_cache(cache_path)
    cache = get_connection(cache_path)
    csq = SourceQuery(connector="arkham", capability="get_attributions", endpoint="import",
                      params={"bounds": "default"}, requested_at="2025-12-01T00:00:00Z", status="ok")

    def cwrite(c, sqid):
        addr_id = repo.upsert_address(c, Address(chain="ethereum", address_display=ETH_TO), sqid)
        return repo.insert_attribution(c, Attribution(
            address_id=addr_id, label="Kraken", category="exchange", source="arkham",
            confidence=0.8, retrieved_at="2025-12-01T00:00:00Z"), sqid)

    _, cache_attr_id = write_with_provenance(cache, csq, cwrite, raw_response="cache raw")
    new_id = copy_address_claim_into_case(conn, cache, claim_table="attribution",
                                          claim_id=cache_attr_id)
    cache.close()

    # The originating source_query was carried in; the claim resolves.
    assert conn.execute("SELECT 1 FROM source_query WHERE id=?", (csq.id,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM attribution WHERE id=?", (new_id,)).fetchone() is not None
    results = run_audits(db_path=str(db))
    assert _result(results, "cache-provenance-carried").passed
    assert all(r.passed for r in results)


def test_cache_copy_is_idempotent(case, tmp_path):
    conn, _ = case
    seed_evm_transfer(conn)
    cache_path = tmp_path / "library_cache.db"
    migrate_cache(cache_path)
    cache = get_connection(cache_path)
    csq = SourceQuery(connector="arkham", capability="get_attributions", endpoint="import",
                      params={"bounds": "default"}, requested_at="2025-12-01T00:00:00Z", status="ok")

    def cwrite(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=ETH_TO), sqid)
        return repo.insert_attribution(c, Attribution(
            address_id=aid, label="Kraken", source="arkham", retrieved_at="2025-12-01T00:00:00Z"), sqid)

    _, cache_attr_id = write_with_provenance(cache, csq, cwrite, raw_response="x")
    id1 = copy_address_claim_into_case(conn, cache, claim_table="attribution", claim_id=cache_attr_id)
    id2 = copy_address_claim_into_case(conn, cache, claim_table="attribution", claim_id=cache_attr_id)
    cache.close()
    assert id1 == id2 == cache_attr_id
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 1  # no duplicate


def test_cache_copy_rejects_unsupported_claim(case, tmp_path):
    # balance_snapshot / valuation need extra FK remapping — out of v1 cache scope.
    conn, _ = case
    cache_path = tmp_path / "library_cache.db"
    migrate_cache(cache_path)
    cache = get_connection(cache_path)
    with pytest.raises(ValueError):
        copy_address_claim_into_case(conn, cache, claim_table="balance_snapshot", claim_id="x")
    cache.close()


# --------------------------------------------------------------------------- atomic writer

def test_provenance_rollback_leaves_no_orphan(case):
    conn, db = case
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="x",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def boom(c, sqid):
        repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        write_with_provenance(conn, sq, boom, raw_response={"k": "v"})

    assert conn.execute("SELECT COUNT(*) FROM source_query WHERE id=?", (sq.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0
    raw = db.parent / "raw_responses" / f"{sq.id}.json"
    tmp = db.parent / "raw_responses" / f"{sq.id}.json.tmp"
    assert not raw.exists() and not tmp.exists()  # staged temp removed, nothing promoted


def test_provenance_writes_raw_file_with_matching_hash(case):
    conn, db = case
    seed_evm_transfer(conn)
    row = conn.execute(
        "SELECT raw_response_ref, raw_response_hash FROM source_query WHERE connector='etherscan'"
    ).fetchone()
    raw_path = db.parent / row["raw_response_ref"]
    assert raw_path.exists()
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == row["raw_response_hash"]


def test_write_with_provenance_is_nesting_safe(case):
    conn, db = case
    outer = SourceQuery(connector="c-outer", capability="cap", endpoint="e", params={},
                        requested_at="2026-01-01T00:00:00Z", status="ok")
    inner = SourceQuery(connector="c-inner", capability="cap", endpoint="e", params={},
                        requested_at="2026-01-01T00:00:00Z", status="ok")

    def outer_write(c, outer_sqid):
        repo.upsert_address(c, Address(chain="ethereum", address_display=ETH_FROM), outer_sqid)

        def inner_write(c2, inner_sqid):
            repo.upsert_address(c2, Address(chain="ethereum", address_display=ETH_TO), inner_sqid)

        write_with_provenance(c, inner, inner_write)  # nested provenance write

    write_with_provenance(conn, outer, outer_write)
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM address").fetchone()[0] == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))
