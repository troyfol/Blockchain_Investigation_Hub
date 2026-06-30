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


def _load_asset(conn, asset_id: str) -> Asset | None:
    r = conn.execute(
        "SELECT chain, contract_address, symbol, decimals FROM asset WHERE id=?", (asset_id,)).fetchone()
    if r is None:
        return None
    return Asset(chain=r["chain"], contract_address=r["contract_address"], symbol=r["symbol"],
                 decimals=r["decimals"])


def _unvalued_movements(conn):
    """Value movements (from the view) that have no valuation yet and can be valued."""
    return conn.execute(
        """
        SELECT m.paradigm, m.movement_id, m.amount, m.chain, m.asset_id, m.transaction_id
        FROM v_value_movement m
        WHERE m.asset_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM valuation v
            WHERE v.subject_type = (CASE m.paradigm WHEN 'evm' THEN 'transfer' ELSE 'tx_output' END)
              AND v.subject_id = m.movement_id)
        """
    ).fetchall()


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

    rows = _unvalued_movements(conn)
    if limit is not None:
        rows = rows[:limit]
    if job is not None:  # P8.7.2 — report progress ("valued M of N") via the active job
        job.phase = "valuing"
        job.total = len(rows)
        job.valued = 0

    # Resolve each movement's asset + block timestamp; group by timestamp for batching.
    by_ts: dict[int, list] = defaultdict(list)
    for m in rows:
        subject_type = "transfer" if m["paradigm"] == "evm" else "tx_output"
        asset = _load_asset(conn, m["asset_id"])
        block_ts = conn.execute(
            "SELECT block_ts FROM transaction_ WHERE id=?", (m["transaction_id"],)).fetchone()["block_ts"]
        if asset is None or not block_ts:
            skipped += 1  # no asset/decimals or no block timestamp (mempool) → can't value at time
            continue
        by_ts[_iso_to_unix(block_ts)].append((m, subject_type, asset))

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
        except ConnectorError:
            errors += 1  # rate limit / upstream → honest gap (NO rows) for the whole group
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
