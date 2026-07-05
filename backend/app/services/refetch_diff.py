"""Re-fetch snapshot / diff (P23/FN-13) — what changed between two fetches of the same data.

A re-fetch is idempotent (Invariant #7): a movement already ingested is a no-op, and a `provisional`
transaction may mature to `final` or be corrected on the sanctioned re-fetch (Invariant #6). This service
answers "what did that re-fetch actually change?" by capturing a **lightweight, read-only snapshot** of the
case's fact-state, then diffing it against the state after the re-fetch:

  before = capture_snapshot(conn)      # read-only
  ... orchestrator re-fetches (the ONE sanctioned mutation) ...
  diff   = compute_diff(conn, before)  # read-only -> "+N transfers, K provisional->final, C corrected"

**Read-only (acceptance #4):** both functions issue only SELECTs — the diff never mutates a fact; the only
change is the wrapped re-fetch's sanctioned provisional refresh. **Zero-dup (Invariant #7):** `new_transfers`
counts only transfer rows whose id is genuinely new (a re-fetched identical movement keeps its original row +
id via the content+occurrence `DO NOTHING`, so it is NOT counted). **Finality maturation (Invariant #6):** a
tx that was `provisional` before and is `final` after is reported as a flip; a tx that STAYED provisional but
whose block identity/status changed (a reorg correction) is reported as `corrected` (confirmations ticking up
on a still-provisional tx is normal maturation, NOT a correction, so it is deliberately excluded from the
signature).

The snapshot is a plain JSON-serializable dict (counts/ids/signatures) — transient here (diff a re-fetch
against the state just before it, the P23 acceptance). Persisting snapshots to diff two ARBITRARY historical
fetches is a documented follow-up (would use `source_query.result_summary` or a dedicated table); the MVP
needs neither, so it makes no schema change.
"""

from __future__ import annotations

# Sourced-claim tables whose new rows a re-fetch can add (each keyed by a stable `id`). Family-A facts are
# covered by `transfer` (movements) + `transaction_` (finality); these are the Family-B claim surfaces.
_CLAIM_TABLES = ("attribution", "risk_assessment", "valuation", "risk_detail")


def _tx_sig(row) -> str:
    """A change-detection signature for a transaction's BLOCK IDENTITY + status — the fields a reorg
    correction would move. Deliberately EXCLUDES `confirmations` (it ticks up on every re-fetch and is
    normal maturation, not a correction) and `finality_status` (a flip is reported separately)."""
    return f"{row['block_height']}|{row['block_ts']}|{row['status']}"


def capture_snapshot(conn) -> dict:
    """Read-only snapshot of the case's fact-state for a later `compute_diff`. Case-wide (a re-fetch of one
    address only writes that address's data, so a case-wide diff equals that address's diff, with no
    address-scoping complexity). Writes nothing."""
    txs = {}
    for r in conn.execute(
        "SELECT chain, tx_hash, finality_status, block_height, block_ts, status FROM transaction_"
    ).fetchall():
        txs[f"{r['chain']}|{r['tx_hash']}"] = {"finality": r["finality_status"], "sig": _tx_sig(r)}
    transfer_ids = [r[0] for r in conn.execute("SELECT id FROM transfer").fetchall()]
    claims = {t: [r[0] for r in conn.execute(f"SELECT id FROM {t}").fetchall()] for t in _CLAIM_TABLES}
    return {"tx": txs, "transfer_ids": transfer_ids, "claims": claims}


def _split_key(key: str) -> dict:
    chain, _, tx_hash = key.partition("|")
    return {"chain": chain, "tx_hash": tx_hash}


def compute_diff(conn, before: dict) -> dict:
    """Read-only delta of the CURRENT state vs a `before` snapshot. Returns new-transfer count/ids, the list
    of provisional->final maturations, the list of corrected-while-provisional facts (with before/after
    signatures), per-table new-claim counts, and a one-line human summary. Writes nothing."""
    after = capture_snapshot(conn)

    new_transfer_ids = sorted(set(after["transfer_ids"]) - set(before["transfer_ids"]))

    flips, corrected = [], []
    for key, aval in after["tx"].items():
        bval = before["tx"].get(key)
        if bval is None:
            continue  # a brand-new tx — its movements are counted as new transfers, not a flip/correction
        if bval["finality"] == "provisional" and aval["finality"] == "final":
            flips.append(_split_key(key))
        elif (bval["finality"] == "provisional" and aval["finality"] == "provisional"
              and bval["sig"] != aval["sig"]):
            corrected.append({**_split_key(key), "before": bval["sig"], "after": aval["sig"]})

    new_claims = {t: len(set(after["claims"][t]) - set(before["claims"][t])) for t in _CLAIM_TABLES}

    return {
        "new_transfers": len(new_transfer_ids),
        "new_transfer_ids": new_transfer_ids,
        "provisional_to_final": flips,
        "corrected": corrected,
        "new_claims": new_claims,
        "changed": bool(new_transfer_ids or flips or corrected or any(new_claims.values())),
        "summary": (f"+{len(new_transfer_ids)} transfers, {len(flips)} provisional→final, "
                    f"{len(corrected)} corrected"),
    }
