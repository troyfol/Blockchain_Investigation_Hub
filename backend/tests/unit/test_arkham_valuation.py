"""P7 / FN-18 — Arkham `historicalUSD` becomes a SECOND sourced valuation.

Arkham's transfer-log export carries a per-transfer `historicalUSD` (the source's stated USD value-at-
time). P7 routes it into a `valuation` claim `source='arkham'` on that transfer — a SECOND valuation
alongside DeFiLlama, never merged (Invariant #4); it drives `value_contested` and is rendered side-by-
side by P6. No price / no block timestamp / zero amount → no row (honest gap, never a fabricated zero).
Re-ingesting the same export does not duplicate (Invariant #7). The DeFiLlama pass still values a
movement Arkham already priced (the two coexist) — guarded here on `_unvalued_movements`.
"""

from __future__ import annotations

import csv
from decimal import Decimal

from backend.app.audits.runner import run_audits
from backend.app.connectors.imports.arkham import ArkhamImporter
from backend.app.db import repository as repo
from backend.app.models import SourceQuery, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.valuation import _unvalued_movements
from backend.app.services.valuation_display import movement_valuations
from backend.tests.integration._helpers import new_case

_HEADER = ["transactionHash", "fromAddress", "fromLabel", "fromIsContract", "toAddress", "toLabel",
           "toIsContract", "tokenAddress", "type", "blockTimestamp", "blockNumber", "blockHash",
           "tokenName", "tokenSymbol", "tokenDecimals", "unitValue", "tokenId", "historicalUSD", "chain"]

_TS = "2022-06-06T21:48:21Z"


def _row(**kw):
    # A native ETH transfer of 2 ETH the source values at $5000 (→ derived unit_price 2500).
    base = {"transactionHash": "0x" + "a" * 64, "chain": "ethereum", "tokenAddress": "",
            "tokenSymbol": "ETH", "tokenDecimals": "18", "unitValue": "2", "tokenId": "ethereum",
            "historicalUSD": "5000", "blockTimestamp": _TS, "blockNumber": "14917217",
            "fromAddress": "0x" + "1" * 40, "toAddress": "0x" + "2" * 40, "type": ""}
    base.update(kw)
    return base


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in _HEADER})
    return path


def _defillama_valuation(conn, transfer_id, value):
    """Add a DeFiLlama valuation on the same movement (a second, differently-sourced price)."""
    sq = SourceQuery(connector="defillama", capability="get_price", endpoint="prices/historical",
                     params={"coins": "coingecko:ethereum", "timestamp": 1}, requested_at=_TS, status="ok")

    def w(c, sqid):
        repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=transfer_id, currency="USD",
            unit_price=value, value=value, price_timestamp=_TS, source="defillama",
            confidence=0.99, retrieved_at=_TS), sqid)

    write_with_provenance(conn, sq, w)


def test_historical_usd_becomes_second_valuation(tmp_path):
    conn, db = new_case(tmp_path, title="Arkham valuation")
    res = ArkhamImporter().get_transactions(conn, _write_csv(tmp_path / "v.csv", [_row()]))
    assert res["transfers"] == 1 and res["valuations"] == 1

    transfer_id = conn.execute("SELECT id FROM transfer").fetchone()["id"]
    val = conn.execute("SELECT * FROM valuation WHERE source='arkham'").fetchone()
    # value is Arkham's stated total, stored VERBATIM; unit_price is DERIVED (5000 / 2 ETH = 2500).
    assert val["value"] == "5000" and Decimal(val["unit_price"]) == Decimal("2500")
    assert val["subject_type"] == "transfer" and val["subject_id"] == transfer_id
    assert val["price_timestamp"] == _TS and val["confidence"] is None
    # provenance: the valuation references the IMPORT's source_query (the CSV is its hashed raw_response).
    sq = conn.execute("SELECT connector FROM source_query WHERE id=?", (val["source_query_id"],)).fetchone()
    assert sq["connector"] == "arkham-import"

    # A DeFiLlama price on the SAME movement coexists — two sourced valuations, never merged (Invariant #4).
    _defillama_valuation(conn, transfer_id, "4800")
    mv = movement_valuations(conn, transfer_id)
    assert mv["contested"] is True
    assert set(mv["valuations_by_source"]) == {"arkham", "defillama"}
    assert mv["valuations_by_source"]["arkham"][0]["value"] == "5000"
    assert mv["valuations_by_source"]["defillama"][0]["value"] == "4800"
    assert set(mv) == {"subject_id", "valuations_by_source", "contested"}  # no merged/averaged key
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_missing_historical_usd_writes_no_valuation(tmp_path):
    conn, db = new_case(tmp_path, title="Arkham no price")
    res = ArkhamImporter().get_transactions(conn, _write_csv(tmp_path / "n.csv", [_row(historicalUSD="")]))
    assert res["transfers"] == 1 and res["valuations"] == 0
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0  # honest gap, no fabricated 0
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_reingest_does_not_duplicate_arkham_valuation(tmp_path):
    conn, db = new_case(tmp_path, title="Arkham reingest")
    path = _write_csv(tmp_path / "r.csv", [_row()])
    ArkhamImporter().get_transactions(conn, path)
    ArkhamImporter().get_transactions(conn, path)  # same file again — Invariant #7
    assert conn.execute("SELECT COUNT(*) FROM valuation WHERE source='arkham'").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_movement_priced_by_arkham_is_still_unvalued_for_defillama(tmp_path):
    conn, db = new_case(tmp_path, title="Arkham then defillama")
    ArkhamImporter().get_transactions(conn, _write_csv(tmp_path / "m.csv", [_row()]))
    transfer_id = conn.execute("SELECT id FROM transfer").fetchone()["id"]

    # The movement has an arkham valuation but the DeFiLlama pass must STILL see it as unvalued, so the two
    # sources end up side-by-side (the crux of the `_unvalued_movements` source scoping to 'defillama').
    assert transfer_id in {r["movement_id"] for r in _unvalued_movements(conn)}
    _defillama_valuation(conn, transfer_id, "4800")
    assert transfer_id not in {r["movement_id"] for r in _unvalued_movements(conn)}  # now defillama-valued
