"""Valuation service (Phase 5, phase_05 step 2).

Attaches value-at-time (USD) to value movements — EVM ``transfer`` and Bitcoin ``tx_output`` — as
append-only ``valuation`` claims with confidence + provenance. Each movement is valued at ITS
block timestamp (docs/algorithms.md §3). A missing price writes NO row (honest gap, never a
fabricated zero); re-valuing appends a new row, never overwrites (Invariant #4).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from ..connectors.base import ConnectorError
from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Asset, SourceQuery, Valuation
from ..normalization.valuation_math import compute_value
from ..provenance.atomic import write_with_provenance


def _iso_to_unix(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _unix_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unvalued_movements(conn, limit: int | None = None):
    """Value movements (from the view) that have no valuation yet and can be valued. EFF-04: the asset
    fields + block_ts are JOINed in (no per-movement point queries) and the pass cap is applied as a SQL
    ``LIMIT`` (no materialize-all-then-slice)."""
    sql = """
        SELECT m.paradigm, m.movement_id, m.amount, m.chain, m.transaction_id,
               a.chain AS asset_chain, a.contract_address AS asset_contract,
               a.symbol AS asset_symbol, a.decimals AS asset_decimals,
               t.block_ts AS block_ts
        FROM v_value_movement m
        JOIN asset a ON a.id = m.asset_id
        JOIN transaction_ t ON t.id = m.transaction_id
        WHERE NOT EXISTS (
            SELECT 1 FROM valuation v
            WHERE v.subject_type = (CASE m.paradigm WHEN 'evm' THEN 'transfer' ELSE 'tx_output' END)
              AND v.subject_id = m.movement_id)
        ORDER BY m.movement_id
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def value_movements(conn, connector, *, now: str | None = None, limit: int | None = None,
                    max_consecutive_errors: int = 5, job=None) -> dict:
    """Value unvalued movements via ``connector.get_prices``, BATCHED by block timestamp. Returns counts.

    Movements are grouped by their block timestamp and all coins at a timestamp are fetched in ONE
    ``/prices/historical/{ts}/{comma-joined keys}`` call (a single call serves every movement sharing the
    timestamp). Robust for bulk valuation against a rate-limited public price API:
    - a price-source error on a timestamp's batch **skips that whole group** (an honest gap, NO rows —
      same as a genuinely missing price) instead of aborting the pass;
    - after ``max_consecutive_errors`` consecutive batch failures a circuit-breaker stops further calls
      (don't keep hammering a source that is clearly unavailable) and reports it;
    - a missing price for a coin is an honest gap (no row, never a fabricated zero).
    ``limit`` caps how many movements are attempted (bounded batches). All valuations from one batch
    share one ``source_query`` (the batch response is its hashed ``raw_response``).
    """
    now = now or utc_now_iso()
    valued = skipped = errors = 0
    consecutive_errors = 0
    source_unavailable = False

    rows = _unvalued_movements(conn, limit=limit)  # EFF-04: cap + asset/block_ts JOIN pushed into SQL
    if job is not None:  # P8.7.2 — report progress ("valued M of N") via the active job
        job.phase = "valuing"
        job.total = len(rows)
        job.valued = 0

    # Group by timestamp for batching. Asset + block_ts come back on the row (no per-movement queries).
    by_ts: dict[int, list] = defaultdict(list)
    for m in rows:
        subject_type = "transfer" if m["paradigm"] == "evm" else "tx_output"
        asset = Asset(chain=m["asset_chain"], contract_address=m["asset_contract"],
                      symbol=m["asset_symbol"], decimals=m["asset_decimals"])
        block_ts = m["block_ts"]
        if not block_ts:
            skipped += 1  # no block timestamp (mempool) → can't value at time
            continue
        try:
            unix_ts = _iso_to_unix(block_ts)  # LOG-05: a non-ISO block_ts is an honest skip, not a crash
        except ValueError:
            skipped += 1
            continue
        by_ts[unix_ts].append((m, subject_type, asset))

    for ts in sorted(by_ts):
        if job is not None:
            job.check_cancel()  # cooperative cancel between batches (before the next price call/write)
        group = by_ts[ts]
        if source_unavailable:
            skipped += len(group)  # circuit-breaker open — don't keep calling
            continue

        # Distinct (chain, asset) for this timestamp's single batch call.
        distinct: dict[str, tuple] = {}
        for m, _st, asset in group:
            try:
                distinct.setdefault(connector.coin_key(m["chain"], asset), (m["chain"], asset))
            except ConnectorError:
                pass  # no price key for this chain/asset → handled as a miss per-movement below

        try:
            prices, payload = connector.get_prices(list(distinct.values()), ts)
            consecutive_errors = 0
        except (ConnectorError, ValueError):
            # RES-01: a rate-limit/upstream ConnectorError OR a decode failure (a non-JSON 200 makes
            # `.json()` raise ValueError/JSONDecodeError) is an honest gap (NO rows) for the whole group,
            # feeding the circuit-breaker — never an abort of the entire pass.
            errors += 1
            consecutive_errors += 1
            skipped += len(group)
            if consecutive_errors >= max_consecutive_errors:
                source_unavailable = True
            continue

        to_write = []
        for m, subject_type, asset in group:
            try:
                key = connector.coin_key(m["chain"], asset)
            except ConnectorError:
                skipped += 1  # no price key (e.g. unsupported native chain) → honest gap
                continue
            price = prices.get(key)
            if price is None:
                skipped += 1  # missing price → honest gap, NO row
                continue
            to_write.append((subject_type, m["movement_id"], price,
                             compute_value(m["amount"], asset.decimals, price.price)))

        if not to_write:
            continue
        sq = SourceQuery(connector="defillama", capability="get_price", endpoint="prices/historical",
                         params={"coins": ",".join(distinct), "timestamp": ts},
                         requested_at=now, completed_at=now, status="ok")

        def write(c, sqid, items=to_write):
            for st, sid, p, v in items:
                repo.insert_valuation(c, Valuation(
                    subject_type=st, subject_id=sid, currency="USD", unit_price=p.price, value=v,
                    price_timestamp=_unix_to_iso(p.price_timestamp), confidence=p.confidence,
                    source="defillama", retrieved_at=now), sqid)
            return len(items)

        write_with_provenance(conn, sq, write, raw_response=payload)
        valued += len(to_write)
        if job is not None:
            job.valued = valued

    return {"valued": valued, "skipped": skipped, "errors": errors,
            "price_source_unavailable": source_unavailable}
