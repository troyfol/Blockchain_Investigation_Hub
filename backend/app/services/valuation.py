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
from ..db.shared_cache import copy_source_query_into_case, get_cached_price, put_cached_price
from ..models import Asset, SourceQuery, Valuation
from ..normalization.valuation_math import compute_value
from ..provenance.atomic import write_with_provenance


def _iso_to_unix(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _unix_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unvalued_movements(conn, limit: int | None = None):
    """Value movements (from the view) that DeFiLlama has not priced yet and can be valued. EFF-04: the
    asset fields + block_ts are JOINed in (no per-movement point queries) and the pass cap is applied as a
    SQL ``LIMIT`` (no materialize-all-then-slice).

    FN-18: the "already valued" check is scoped to ``source='defillama'`` — a movement another source
    already priced (e.g. Arkham's ``historicalUSD``) is STILL priced by DeFiLlama so the two valuations sit
    side-by-side, never merged (Invariant #4). DeFiLlama stays idempotent: once it has priced a movement,
    that movement is excluded on the next pass."""
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
              AND v.subject_id = m.movement_id
              AND v.source = 'defillama')
        ORDER BY m.movement_id
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def _write_cached_valuations(conn, cache_conn, rec, members) -> int:
    """Value movements from a CACHED price (no network). Carries the ORIGINAL ``source_query`` into the
    case first (so provenance reflects the original retrieval and the #8 audit passes), then references it
    — reproducing the original valuation exactly (same unit_price / price_timestamp / confidence /
    retrieved_at). ``members`` are ``(movement_row, subject_type, asset)``. Atomic per coin."""
    price_iso = _unix_to_iso(rec["price_timestamp"])
    sp = "cache_valuation"
    conn.execute(f"SAVEPOINT {sp}")
    try:
        copy_source_query_into_case(conn, cache_conn, rec["source_query_id"])
        n = 0
        for m, subject_type, asset in members:
            repo.insert_valuation(conn, Valuation(
                subject_type=subject_type, subject_id=m["movement_id"], currency="USD",
                unit_price=rec["unit_price"],
                value=compute_value(m["amount"], asset.decimals, rec["unit_price"]),
                price_timestamp=price_iso, confidence=rec["confidence"], source="defillama",
                retrieved_at=rec["retrieved_at"]), rec["source_query_id"])
            n += 1
        conn.execute(f"RELEASE {sp}")
    except Exception:
        conn.execute(f"ROLLBACK TO {sp}")
        conn.execute(f"RELEASE {sp}")
        raise
    return n


def value_movements(conn, connector, *, now: str | None = None, limit: int | None = None,
                    max_consecutive_errors: int = 5, job=None, cache_conn=None) -> dict:
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

        # Group this timestamp's movements by price coin_key (a movement with no key is an honest skip).
        by_coin: dict[str, list] = defaultdict(list)
        for m, subject_type, asset in group:
            try:
                by_coin[connector.coin_key(m["chain"], asset)].append((m, subject_type, asset))
            except ConnectorError:
                skipped += 1  # no price key for this chain/asset → honest gap, NO row

        # FN-05 — serve coins from the shared price cache first (zero network). Uncached coins are a MISS.
        misses: dict[str, tuple] = {}  # coin_key -> a representative (chain, asset) for the batch call
        for ck, members in by_coin.items():
            rec = get_cached_price(cache_conn, ck, ts) if cache_conn is not None else None
            if rec is not None:
                valued += _write_cached_valuations(conn, cache_conn, rec, members)  # hit → original sq
                if job is not None:
                    job.valued = valued
            else:
                misses[ck] = (members[0][0]["chain"], members[0][2])

        if not misses:
            continue  # every coin served from cache → ZERO DeFiLlama calls for this timestamp

        # MISS — one batched DeFiLlama call for the uncached coins at this timestamp.
        try:
            prices, payload = connector.get_prices(list(misses.values()), ts)
            consecutive_errors = 0
        except (ConnectorError, ValueError):
            # RES-01: a rate-limit/upstream ConnectorError OR a decode failure (a non-JSON 200 makes
            # `.json()` raise ValueError/JSONDecodeError) is an honest gap (NO rows) for the missed coins,
            # feeding the circuit-breaker — never an abort of the pass (cache hits above still stand).
            errors += 1
            consecutive_errors += 1
            skipped += sum(len(by_coin[ck]) for ck in misses)
            if consecutive_errors >= max_consecutive_errors:
                source_unavailable = True
            continue

        to_write = []             # (subject_type, movement_id, PriceRecord, value)
        priced: list[tuple] = []  # (coin_key, PriceRecord) → write back to the cache after the case write
        for ck in misses:
            price = prices.get(ck)
            if price is None:
                skipped += len(by_coin[ck])  # missing price → honest gap, NO row and nothing cached
                continue
            for m, subject_type, asset in by_coin[ck]:
                to_write.append((subject_type, m["movement_id"], price,
                                 compute_value(m["amount"], asset.decimals, price.price)))
            priced.append((ck, price))

        if not to_write:
            continue
        sq = SourceQuery(connector="defillama", capability="get_price", endpoint="prices/historical",
                         params={"coins": ",".join(misses), "timestamp": ts},
                         requested_at=now, completed_at=now, status="ok")

        def write(c, sqid, items=to_write):
            for st, sid, p, v in items:
                repo.insert_valuation(c, Valuation(
                    subject_type=st, subject_id=sid, currency="USD", unit_price=p.price, value=v,
                    price_timestamp=_unix_to_iso(p.price_timestamp), confidence=p.confidence,
                    source="defillama", retrieved_at=now), sqid)
            return len(items)

        sqid, _ = write_with_provenance(conn, sq, write, raw_response=payload)
        valued += len(to_write)
        if job is not None:
            job.valued = valued

        # Write the fetched prices back to the shared cache (best-effort — a cache write must NEVER break
        # valuation; the cache is a pure optimization). The just-written source_query IS the original
        # retrieval for these prices, so a later case/pass copies it in as the provenance.
        if cache_conn is not None:
            for ck, p in priced:
                try:
                    put_cached_price(cache_conn, conn, coin_key=ck, price_ts=ts, unit_price=p.price,
                                     price_timestamp=p.price_timestamp, confidence=p.confidence,
                                     retrieved_at=now, source_query_id=sqid)
                except Exception:
                    pass  # never let a cache write fail the valuation

    return {"valued": valued, "skipped": skipped, "errors": errors,
            "price_source_unavailable": source_unavailable}
