"""Finality upgrade mechanism (docs/findings/external_facts_confirmation.md §3; Invariant #6).

Provisional facts are correctable; final facts are frozen. Import/fallback connectors (Arkham,
Bitquery) and tip-adjacent fetches land transactions ``provisional`` with no/low confirmations. This
service re-evaluates finality against a freshly-fetched chain ``tip_height``: it recomputes
``confirmations`` for every PROVISIONAL transaction on a chain and flips it to ``final`` ONLY once
``confirmations >= finality_threshold(chain)`` (the cited per-chain thresholds in ``config.py``, incl.
bsc=15). FINAL rows are never touched (frozen — never re-freeze, never thaw). Idempotent: re-running with
the same tip is a no-op; a higher tip only ever advances provisional→final, never the reverse (Invariant
#6 — tip data is never frozen as ``final`` prematurely).

The refresh is recorded as its own ``source_query`` (connector ``finality-refresh``) so there is an audit
trail of when finality was re-evaluated and against which tip; the transactions keep their original
ingest provenance (a status recompute adds no new sourced data).
"""

from __future__ import annotations

from ..db.repository import utc_now_iso
from ..models import SourceQuery
from ..normalization.finality import finality_for
from ..provenance.atomic import write_with_provenance


def upgrade_finality(conn, *, chain: str, tip_height: int, threshold: int, now: str | None = None) -> dict:
    """Re-evaluate finality for all PROVISIONAL transactions on ``chain`` against ``tip_height``.

    Flip to ``final`` those whose ``confirmations >= threshold``; refresh ``confirmations`` on the rest.
    FINAL rows are untouched. Returns ``{"upgraded", "refreshed", "tip_height"}``.
    """
    now = now or utc_now_iso()
    sq = SourceQuery(
        connector="finality-refresh", capability="upgrade_finality", endpoint="local",
        params={"chain": chain, "tip_height": tip_height, "threshold": threshold, "bounds": "default"},
        requested_at=now, completed_at=now, status="ok")

    def write(c, _sqid):
        upgraded = refreshed = 0
        rows = c.execute(
            "SELECT id, block_height FROM transaction_ WHERE chain=? AND finality_status='provisional'",
            (chain,)).fetchall()
        for r in rows:
            confirmations, status = finality_for(
                tip_height=tip_height, block_height=r["block_height"], threshold=threshold)
            if status == "final":
                c.execute(
                    "UPDATE transaction_ SET confirmations=?, finality_status='final' WHERE id=?",
                    (confirmations, r["id"]))
                upgraded += 1
            else:
                # Still provisional — keep confirmations current so the row stays correctable/honest.
                c.execute("UPDATE transaction_ SET confirmations=? WHERE id=?", (confirmations, r["id"]))
                refreshed += 1
        return {"upgraded": upgraded, "refreshed": refreshed, "tip_height": tip_height}

    _, res = write_with_provenance(conn, sq, write)
    return res


def refresh_finality(conn, chain: str, *, settings, tip_height: int | None = None, connector=None,
                     now: str | None = None) -> dict:
    """Operator convenience: resolve the tip + the per-chain threshold (from ``settings``), then run
    ``upgrade_finality``. Supply ``tip_height`` directly, OR a ``connector`` exposing ``tip_height(chain)``
    (the BTC/UTXO operator flow passes the Esplora connector, which fetches ``/blocks/tip/height``). For
    EVM a fresh Etherscan address fetch already carries confirmations, so re-fetching upgrades those rows
    directly — this service covers tip-only refreshes and import/Bitquery-ingested provisional facts."""
    if tip_height is None:
        if connector is None or not hasattr(connector, "tip_height"):
            raise ValueError(
                "refresh_finality needs an explicit tip_height or a connector exposing tip_height(chain)")
        tip_height = connector.tip_height(chain)
    return upgrade_finality(conn, chain=chain, tip_height=tip_height,
                            threshold=settings.finality_threshold(chain), now=now)
