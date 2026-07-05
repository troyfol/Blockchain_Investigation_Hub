"""Repository: case init + idempotent fact upserts + append-only claim inserts (phase_01 step 6).

Natural-key upserts for Family A facts (``INSERT ... ON CONFLICT(...) DO UPDATE``, docs/schema.md
§4); plain append-only inserts for Family B claims (never collapse — Invariant #4). Facts REQUIRE
a ``source_query_id`` (Invariant #3); the upsert helpers reject a missing one. Final transactions
are frozen: re-fetching one is a no-op rather than a mutation (Invariant #6).

All writers take a connection already inside the provenance transaction (see provenance/atomic).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from ..models import (
    Address,
    Annotation,
    Asset,
    Attribution,
    BalanceSnapshot,
    Entity,
    EntityMembership,
    EntityMembershipRetraction,
    Finding,
    FindingRef,
    InvestigatorLabel,
    Report,
    RiskAssessment,
    RiskDetail,
    Tag,
    Trace,
    TraceBridgeLink,
    TraceBtcLink,
    TraceBtcLinkRetraction,
    TraceTransfer,
    TraceTransferRetraction,
    Transaction,
    Transfer,
    TxInput,
    TxOutput,
    Valuation,
)
from ..normalization.canonical import canonical_address
from .migrate import CURRENT_SCHEMA_VERSION

INVESTIGATOR_SOURCE = "investigator"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_provenance(source_query_id: str | None, table: str) -> None:
    if not source_query_id:
        raise ValueError(f"{table}: a fact cannot be written without a source_query_id (Invariant #3)")


# --------------------------------------------------------------------------- case container

def init_case(conn, *, title: str, description: str | None = None, case_id: str | None = None,
              now: str | None = None) -> str:
    """Insert the single ``case_meta`` row with the current schema version. Returns case id."""
    existing = conn.execute("SELECT id FROM case_meta LIMIT 1").fetchone()
    if existing is not None:
        raise ValueError("case_meta already initialized (single-row container)")
    case_id = case_id or str(uuid4())
    ts = now or utc_now_iso()
    conn.execute(
        """INSERT INTO case_meta (id, title, description, status, schema_version, created_at, updated_at)
           VALUES (?,?,?,'open',?,?,?)""",
        (case_id, title, description, CURRENT_SCHEMA_VERSION, ts, ts),
    )
    return case_id


# --------------------------------------------------------------------------- Family A: facts

def upsert_asset(conn, asset: Asset, source_query_id: str | None) -> str:
    _require_provenance(source_query_id, "asset")
    conn.execute(
        """INSERT INTO asset (id, chain, contract_address, symbol, decimals, source_query_id)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(chain, COALESCE(contract_address,'')) DO UPDATE SET
             symbol   = COALESCE(excluded.symbol, asset.symbol),
             -- LOG-12: decimals is an on-chain constant. A low-fidelity source that DEFAULTS decimals
             -- (Arkham missing->0, Bitquery->18) must not clobber a chain-reported value, or every USD
             -- valuation of the asset scales by 10^±d into the court report. So: once a real (>0) value
             -- is established, never downgrade/change it; a placeholder 0 may still be filled from a
             -- real later value. (0 is treated as "unknown/placeholder" — near-universal for tokens.)
             decimals = CASE WHEN asset.decimals > 0    THEN asset.decimals
                             WHEN excluded.decimals > 0 THEN excluded.decimals
                             ELSE asset.decimals END""",
        (asset.id, asset.chain, asset.contract_address, asset.symbol, asset.decimals, source_query_id),
    )
    row = conn.execute(
        "SELECT id FROM asset WHERE chain=? AND COALESCE(contract_address,'')=COALESCE(?,'')",
        (asset.chain, asset.contract_address),
    ).fetchone()
    return row[0]


def upsert_address(conn, address: Address, source_query_id: str | None) -> str:
    _require_provenance(source_query_id, "address")
    canonical = canonical_address(address.chain, address.address_display)
    conn.execute(
        """INSERT INTO address (id, chain, address, address_display, first_seen_ts, source_query_id)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(chain, address) DO UPDATE SET
             address_display = excluded.address_display,
             first_seen_ts   = COALESCE(address.first_seen_ts, excluded.first_seen_ts)""",
        (address.id, address.chain, canonical, address.address_display,
         address.first_seen_ts, source_query_id),
    )
    row = conn.execute(
        "SELECT id FROM address WHERE chain=? AND address=?", (address.chain, canonical)
    ).fetchone()
    return row[0]


def upsert_transaction(conn, tx: Transaction, source_query_id: str | None,
                       *, authoritative: bool = False, update_status: bool = True) -> str:
    """Upsert a transaction on (chain, tx_hash). A FINAL existing row is frozen (no update).

    ``authoritative`` (LOG-13): a chain source (Esplora/Etherscan/Bitquery) reports the COMPLETE current
    block state, so on a provisional re-fetch its block_height/block_ts REPLACE the stored values even to
    NULL — a reorg→mempool eviction must not leave a confirmed-block + mempool-status hybrid. A partial
    import (Arkham CSV, which carries no block_height/confirmations/status) leaves ``authoritative`` False,
    so it only FILLS gaps (COALESCE) and never wipes a chain source's fields. Either way the refreshed row
    re-cites the producing fetch's ``source_query_id`` (Invariant #3 — a fact points at the fetch that
    produced its current values). Final rows are frozen, so their provenance never changes.

    ``update_status`` (LOG-11): only the authoritative top-level feed may overwrite an existing row's
    ``status``. Etherscan's ``txlistinternal`` (a sub-call's isError) and ``tokentx`` (a blanket success)
    pass ``update_status=False`` so they cannot clobber ``txlist``'s top-level status on a provisional row
    (a succeeded tx whose first internal call reverted must stay ``success``). It only gates the conflict
    update — an INSERT still records whatever status the sole feed carries (an internal-only tx).
    """
    _require_provenance(source_query_id, "transaction_")
    existing = conn.execute(
        "SELECT id, finality_status FROM transaction_ WHERE chain=? AND tx_hash=?",
        (tx.chain, tx.tx_hash),
    ).fetchone()
    if existing is not None and existing["finality_status"] == "final":
        return existing["id"]  # immutable once final (Invariant #6) — refetch is a no-op
    if authoritative:
        block_set = ("block_height  = excluded.block_height,\n"
                     "             block_ts      = excluded.block_ts,\n"
                     "             confirmations = excluded.confirmations,")
    else:
        block_set = ("block_height  = COALESCE(excluded.block_height, transaction_.block_height),\n"
                     "             block_ts      = COALESCE(excluded.block_ts, transaction_.block_ts),\n"
                     "             confirmations = COALESCE(excluded.confirmations, transaction_.confirmations),")
    status_set = ("COALESCE(excluded.status, transaction_.status)" if update_status
                  else "transaction_.status")
    conn.execute(
        f"""INSERT INTO transaction_
             (id, chain, tx_hash, block_height, block_ts, fee, status, confirmations,
              finality_status, source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(chain, tx_hash) DO UPDATE SET
             {block_set}
             fee             = COALESCE(excluded.fee, transaction_.fee),
             status          = {status_set},
             finality_status = excluded.finality_status,
             source_query_id = excluded.source_query_id""",
        (tx.id, tx.chain, tx.tx_hash, tx.block_height, tx.block_ts, tx.fee, tx.status,
         tx.confirmations, tx.finality_status, source_query_id),
    )
    row = conn.execute(
        "SELECT id FROM transaction_ WHERE chain=? AND tx_hash=?", (tx.chain, tx.tx_hash)
    ).fetchone()
    return row[0]


def upsert_transfer(conn, transfer: Transfer, source_query_id: str | None) -> str:
    """Idempotent transfer upsert keyed on the movement's CONTENT + ``occurrence`` (NOT ``position``,
    which is source-dependent) — so the same on-chain movement ingested from two sources dedups to one
    row, the first source's provenance winning (decision (c); Invariants #4/#7). ``position`` is stored
    as a source-reported display ordinal. Returns the existing-or-new id."""
    _require_provenance(source_query_id, "transfer")
    conn.execute(
        """INSERT INTO transfer
             (id, transaction_id, chain, from_address_id, to_address_id, asset_id, amount,
              transfer_type, position, occurrence, source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(transaction_id, transfer_type, COALESCE(from_address_id,''),
                       COALESCE(to_address_id,''), asset_id, amount, occurrence) DO NOTHING""",
        (transfer.id, transfer.transaction_id, transfer.chain, transfer.from_address_id,
         transfer.to_address_id, transfer.asset_id, transfer.amount, transfer.transfer_type,
         transfer.position, transfer.occurrence, source_query_id),
    )
    row = conn.execute(
        "SELECT id FROM transfer WHERE transaction_id=? AND transfer_type=? "
        "AND COALESCE(from_address_id,'')=COALESCE(?,'') AND COALESCE(to_address_id,'')=COALESCE(?,'') "
        "AND asset_id=? AND amount=? AND occurrence=?",
        (transfer.transaction_id, transfer.transfer_type, transfer.from_address_id,
         transfer.to_address_id, transfer.asset_id, transfer.amount, transfer.occurrence),
    ).fetchone()
    return row[0]


def upsert_tx_output(conn, out: TxOutput, source_query_id: str | None) -> str:
    _require_provenance(source_query_id, "tx_output")
    conn.execute(
        """INSERT INTO tx_output
             (id, transaction_id, address_id, amount, output_index, spent, spending_tx_id,
              source_query_id)
           VALUES (?,?,?,?,?,?,?,?)
           -- LOG-01: the refresh is MONOTONIC — re-fetching a shared funding tx (whose outputs are
           -- re-upserted with the default spent=0) must never reset a known spend to unspent, nor drop
           -- a recorded spender. MAX keeps spent at 1 once set; COALESCE keeps the first known spender.
           ON CONFLICT(transaction_id, output_index) DO UPDATE SET
             spent          = MAX(tx_output.spent, excluded.spent),
             spending_tx_id = COALESCE(tx_output.spending_tx_id, excluded.spending_tx_id)""",
        (out.id, out.transaction_id, out.address_id, out.amount, out.output_index, out.spent,
         out.spending_tx_id, source_query_id),
    )
    row = conn.execute(
        "SELECT id FROM tx_output WHERE transaction_id=? AND output_index=?",
        (out.transaction_id, out.output_index),
    ).fetchone()
    return row[0]


def upsert_tx_input(conn, txin: TxInput, source_query_id: str | None) -> str:
    _require_provenance(source_query_id, "tx_input")
    conn.execute(
        """INSERT INTO tx_input
             (id, transaction_id, prev_output_id, address_id, amount, input_index, source_query_id)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(transaction_id, input_index) DO UPDATE SET
             prev_output_id = excluded.prev_output_id,
             address_id     = excluded.address_id""",
        (txin.id, txin.transaction_id, txin.prev_output_id, txin.address_id, txin.amount,
         txin.input_index, source_query_id),
    )
    row = conn.execute(
        "SELECT id FROM tx_input WHERE transaction_id=? AND input_index=?",
        (txin.transaction_id, txin.input_index),
    ).fetchone()
    return row[0]


def insert_erc20_approval(conn, a, source_query_id: str | None) -> str:
    """Insert an ERC-20 Approval event (LOG-06). Insert-once on (chain, owner, spender, asset, tx_hash) so
    a re-fetch is idempotent (Invariant #7) — a given owner→spender allowance for a token in one tx."""
    _require_provenance(source_query_id, "erc20_approval")
    existing = conn.execute(
        "SELECT id FROM erc20_approval WHERE chain=? AND owner_address_id=? AND spender_address_id=? "
        "AND COALESCE(asset_id,'')=COALESCE(?,'') AND COALESCE(tx_hash,'')=COALESCE(?,'')",
        (a.chain, a.owner_address_id, a.spender_address_id, a.asset_id, a.tx_hash)).fetchone()
    if existing is not None:
        return existing[0]
    conn.execute(
        """INSERT INTO erc20_approval
             (id, chain, owner_address_id, spender_address_id, asset_id, amount, block_height, tx_hash,
              retrieved_at, source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (a.id, a.chain, a.owner_address_id, a.spender_address_id, a.asset_id, a.amount, a.block_height,
         a.tx_hash, a.retrieved_at, source_query_id),
    )
    return a.id


# --------------------------------------------------------------------------- reorg cleanup (COR-01)

def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _tx_has_investigator_refs(conn, tx_id: str, transfer_ids: list[str], output_ids: list[str]) -> bool:
    """True if an INVESTIGATOR object (annotation / tag / display label / finding_ref) references the tx or
    any of its movements. Deriving policy (COR-01, derived-only): the reorg sweep will cascade DERIVED data
    (valuations, machine trace links) but must NEVER silently destroy investigator work — so a tx the
    investigator personally annotated/tagged/labeled/put a finding on is PRESERVED + reported instead."""
    for table, type_col, id_col in (("annotation", "target_type", "target_id"),
                                    ("tag", "target_type", "target_id"),
                                    ("investigator_label", "target_type", "target_id"),
                                    ("finding_ref", "ref_type", "ref_id")):
        clauses = [f"({type_col}='transaction' AND {id_col}=?)"]
        params: list = [tx_id]
        if transfer_ids:
            clauses.append(f"({type_col}='transfer' AND {id_col} IN ({_placeholders(len(transfer_ids))}))")
            params += transfer_ids
        if output_ids:
            clauses.append(f"({type_col}='tx_output' AND {id_col} IN ({_placeholders(len(output_ids))}))")
            params += output_ids
        if conn.execute(f"SELECT 1 FROM {table} WHERE {' OR '.join(clauses)} LIMIT 1", params).fetchone():
            return True
    return False


def _delete_transaction_with_derived(conn, tx_id: str, transfer_ids: list[str], output_ids: list[str]) -> None:
    """FK-safe delete of a reorged-out provisional tx + its DERIVED dependents (COR-01, derived-only policy;
    the caller guarantees no INVESTIGATOR refs via ``_tx_has_investigator_refs``). Order: derived rows
    (valuations of its movements, machine trace links) → spend-link cleanup → Family-A children → the tx.
    So no dangling valuation/trace remains (the `no-dangling-fk` audit + FKs stay green)."""
    movement_ids = transfer_ids + output_ids
    if movement_ids:  # valuations of this tx's movements — a valuation of a reorged movement is void
        conn.execute(f"DELETE FROM valuation WHERE subject_id IN ({_placeholders(len(movement_ids))})",
                     movement_ids)
    if transfer_ids:  # EVM trace edges over its transfers
        conn.execute(f"DELETE FROM trace_transfer WHERE transfer_id IN ({_placeholders(len(transfer_ids))})",
                     transfer_ids)
    # BTC trace links naming this tx or its outputs (source/dest)
    conn.execute(
        "DELETE FROM trace_btc_link WHERE transaction_id=?"
        + (f" OR source_output_id IN ({_placeholders(len(output_ids))})"
           f" OR dest_output_id IN ({_placeholders(len(output_ids))})" if output_ids else ""),
        [tx_id] + output_ids + output_ids)
    # Outputs THIS tx spent revert to unspent (its spends are void — it was reorged out).
    conn.execute("UPDATE tx_output SET spent=0, spending_tx_id=NULL WHERE spending_tx_id=?", (tx_id,))
    # Inputs of OTHER txs that pointed at this tx's outputs lose their prev-output link (the output is gone).
    if output_ids:
        conn.execute(
            f"UPDATE tx_input SET prev_output_id=NULL WHERE prev_output_id IN ({_placeholders(len(output_ids))})",
            output_ids)
    conn.execute("DELETE FROM transfer  WHERE transaction_id=?", (tx_id,))
    conn.execute("DELETE FROM tx_input  WHERE transaction_id=?", (tx_id,))
    conn.execute("DELETE FROM tx_output WHERE transaction_id=?", (tx_id,))
    conn.execute("DELETE FROM transaction_ WHERE id=?", (tx_id,))


def sweep_reorged_provisional(conn, *, chain: str, address: str, present_tx_hashes: set[str],
                              source_query_id: str | None) -> dict:
    """COR-01: on a COMPLETE address re-fetch, delete the PROVISIONAL transactions (and their Family-A
    children) that reference ``address`` on ``chain`` but are ABSENT from the fresh set — a reorged/
    replaced tx (Invariant #6's correctable side; the documented counterpart to "never freeze tip data").

    Final rows are never touched. The CALLER must only invoke this after a NON-PARTIAL fetch — a bounded
    page legitimately omits txs. The deletion runs inside the fetch's provenance transaction
    (``source_query_id``).

    Deletion policy (COR-01, **derived-only, preserve human work**): a reorged tx is deleted with its
    Family-A children AND its DERIVED dependents (valuations of its movements, machine trace links). But if
    an INVESTIGATOR personally annotated / tagged / labeled it or put a finding on it, the tx is PRESERVED
    and reported under ``skipped_referenced`` — investigator work is never silently destroyed (the operator
    decides). Returns ``{"deleted": [tx_hash…], "skipped_referenced": [tx_hash…]}``."""
    _require_provenance(source_query_id, "transaction_")
    canonical = canonical_address(chain, address)
    row = conn.execute("SELECT id FROM address WHERE chain=? AND address=?", (chain, canonical)).fetchone()
    if row is None:
        return {"deleted": [], "skipped_referenced": []}
    address_id = row["id"]
    candidates = conn.execute(
        """SELECT DISTINCT t.id, t.tx_hash FROM transaction_ t
             WHERE t.chain=? AND t.finality_status='provisional' AND t.id IN (
               SELECT transaction_id FROM tx_input  WHERE address_id=?
               UNION SELECT transaction_id FROM tx_output WHERE address_id=?
               UNION SELECT transaction_id FROM transfer WHERE from_address_id=? OR to_address_id=?
             )""",
        (chain, address_id, address_id, address_id, address_id)).fetchall()
    deleted, skipped = [], []
    for cand in candidates:
        if cand["tx_hash"] in present_tx_hashes:
            continue
        transfer_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transfer WHERE transaction_id=?", (cand["id"],)).fetchall()]
        output_ids = [r[0] for r in conn.execute(
            "SELECT id FROM tx_output WHERE transaction_id=?", (cand["id"],)).fetchall()]
        if _tx_has_investigator_refs(conn, cand["id"], transfer_ids, output_ids):
            skipped.append(cand["tx_hash"])   # preserve human work — the operator decides
            continue
        _delete_transaction_with_derived(conn, cand["id"], transfer_ids, output_ids)
        deleted.append(cand["tx_hash"])
    return {"deleted": deleted, "skipped_referenced": skipped}


# --------------------------------------------------------------------------- Family B: claims
# Append-only: each call is a NEW row (preserve disagreement). Provenance required EXCEPT
# investigator-authored attribution/membership/retraction (source='investigator').

def _claim_provenance(source_query_id: str | None, source: str, table: str) -> None:
    if not source_query_id and source != INVESTIGATOR_SOURCE:
        raise ValueError(
            f"{table}: a sourced claim needs a source_query_id unless source='investigator'"
        )


def insert_attribution(conn, a: Attribution, source_query_id: str | None) -> str:
    _claim_provenance(source_query_id, a.source, "attribution")
    conn.execute(
        """INSERT INTO attribution
             (id, address_id, label, category, source, confidence, note, retrieved_at, source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (a.id, a.address_id, a.label, a.category, a.source, a.confidence, a.note,
         a.retrieved_at, source_query_id),
    )
    return a.id


def upsert_attribution(conn, a: Attribution, source_query_id: str | None) -> str:
    """Idempotent attribution insert keyed on the full claim content ``(address_id, label, source,
    category, confidence, note)`` (Invariant #7). Re-ingesting the SAME tag is a no-op; if a claim
    DISAGREES on any captured dimension — a different ``source``, ``category`` (one party says
    `exchange`, another `mixer`), ``confidence``, or per-tag backlink (in ``note``) — it becomes a NEW
    row, so disagreeing claims are kept side-by-side, never merged (Invariant #4). Only genuinely
    identical claims dedup. Returns the existing row's id on a match, else the new id. (Mirrors the
    entities service's app-level insert-once idiom — the append-only claim tables carry no DB unique
    constraints; ``category``/``confidence`` are in the key, not just ``note``, so a disagreement that
    rides only in ``category`` cannot silently collapse.)"""
    _claim_provenance(source_query_id, a.source, "attribution")
    existing = conn.execute(
        "SELECT id FROM attribution WHERE address_id=? AND label=? AND source=? "
        "AND COALESCE(category,'')=COALESCE(?,'') AND COALESCE(confidence,-1.0)=COALESCE(?,-1.0) "
        "AND COALESCE(note,'')=COALESCE(?,'')",
        (a.address_id, a.label, a.source, a.category, a.confidence, a.note),
    ).fetchone()
    if existing is not None:
        return existing[0]
    return insert_attribution(conn, a, source_query_id)


def insert_risk_assessment(conn, r: RiskAssessment, source_query_id: str | None) -> str:
    _claim_provenance(source_query_id, r.source, "risk_assessment")
    conn.execute(
        """INSERT INTO risk_assessment
             (id, address_id, score, score_scale, category, rationale, source, retrieved_at,
              source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (r.id, r.address_id, r.score, r.score_scale, r.category, r.rationale, r.source,
         r.retrieved_at, source_query_id),
    )
    return r.id


def upsert_risk_assessment(conn, r: RiskAssessment, source_query_id: str | None) -> str:
    """Idempotent risk insert keyed on the full claim content ``(address_id, source, category, rationale,
    score, score_scale)`` (Invariant #7). Re-ingesting an identical claim is a no-op; a DIFFERENT
    source/category/rationale — OR a different **numeric score** (a paid intel source like Arkham/MisTrack
    re-scoring an address while its category/rationale stay the same) — is a NEW, side-by-side row, so the
    corrected score is captured and disagreeing claims are never merged/averaged (Invariants #4/#6). For
    categorical risk (``score=None``) the score terms are inert, so existing callers stay idempotent.
    Returns the existing-or-new id."""
    _claim_provenance(source_query_id, r.source, "risk_assessment")
    existing = conn.execute(
        "SELECT id FROM risk_assessment WHERE address_id=? AND source=? "
        "AND COALESCE(category,'')=COALESCE(?,'') AND COALESCE(rationale,'')=COALESCE(?,'') "
        "AND COALESCE(score,-1.0)=COALESCE(?,-1.0) AND COALESCE(score_scale,'')=COALESCE(?,'')",
        (r.address_id, r.source, r.category, r.rationale, r.score, r.score_scale),
    ).fetchone()
    if existing is not None:
        return existing[0]
    return insert_risk_assessment(conn, r, source_query_id)


def insert_risk_detail(conn, rd: RiskDetail, source_query_id: str | None) -> str:
    """FN-15: append one per-sub-signal risk row under its parent `risk_assessment`. Idempotent on
    (risk_assessment_id, signal) — re-ingesting a parent's breakdown is a no-op (Invariant #7). Each
    sub-signal is stored RAW, never collapsed/averaged (Invariant #4), with its own provenance written in
    the parent's txn (Invariant #3 — a risk sub-signal is always sourced, never investigator-authored).
    Returns the existing-or-new id."""
    _require_provenance(source_query_id, "risk_detail")
    conn.execute(
        """INSERT INTO risk_detail (id, risk_assessment_id, signal, score, score_scale, source_query_id)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(risk_assessment_id, signal) DO NOTHING""",
        (rd.id, rd.risk_assessment_id, rd.signal, rd.score, rd.score_scale, source_query_id),
    )
    row = conn.execute(
        "SELECT id FROM risk_detail WHERE risk_assessment_id=? AND signal=?",
        (rd.risk_assessment_id, rd.signal),
    ).fetchone()
    return row[0]


def insert_valuation(conn, v: Valuation, source_query_id: str | None) -> str:
    _claim_provenance(source_query_id, v.source, "valuation")
    conn.execute(
        """INSERT INTO valuation
             (id, subject_type, subject_id, currency, unit_price, value, price_timestamp,
              confidence, source, retrieved_at, source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (v.id, v.subject_type, v.subject_id, v.currency, v.unit_price, v.value, v.price_timestamp,
         v.confidence, v.source, v.retrieved_at, source_query_id),
    )
    return v.id


def insert_balance_snapshot(conn, b: BalanceSnapshot, source_query_id: str | None) -> str:
    _claim_provenance(source_query_id, b.source, "balance_snapshot")
    conn.execute(
        """INSERT INTO balance_snapshot
             (id, address_id, asset_id, amount, as_of_ts, source, retrieved_at, source_query_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (b.id, b.address_id, b.asset_id, b.amount, b.as_of_ts, b.source, b.retrieved_at,
         source_query_id),
    )
    return b.id


def insert_entity(conn, e: Entity, now: str | None = None) -> str:
    """Entities are Family C (no source_query_id column). created_at set here."""
    conn.execute(
        """INSERT INTO entity
             (id, name, entity_type, origin, merged_into, canonical_membership_id, external_id,
              created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (e.id, e.name, e.entity_type, e.origin, e.merged_into, e.canonical_membership_id,
         e.external_id, now or utc_now_iso()),
    )
    return e.id


def find_or_create_source_entity(conn, *, external_id: str, name: str | None = None,
                                 entity_type: str | None = None, now: str | None = None) -> tuple[str, bool]:
    """Idempotently resolve a source-origin entity by its upstream ``external_id`` (e.g. a GraphSense
    actor id), so a TagPack `actor` reference and the ActorPack that defines that actor land on ONE
    entity regardless of ingest order (Invariant #7). Returns ``(owning_entity_id, created)`` — the row
    that HOLDS ``external_id``, NOT a merge target. Returning the owning id (rather than chasing
    ``merged_into``) keeps re-ingest idempotent after a merge: the membership key stays stable and
    ``services.entities.resolve`` already chases the pointer at read/display time, so the membership
    still surfaces under the canonical entity. A stub created first from a tag (name defaulted to the
    id) is upgraded on this same owning row by a later ActorPack ingest — never clobbering a separate
    merge target's curated name."""
    row = conn.execute(
        "SELECT id, name, entity_type FROM entity WHERE external_id=? AND origin='source'",
        (external_id,),
    ).fetchone()
    if row is not None:
        sets, params = [], []
        if name and row["name"] in (None, external_id) and name != row["name"]:
            sets.append("name=?")
            params.append(name)
        if entity_type and not row["entity_type"]:
            sets.append("entity_type=?")
            params.append(entity_type)
        if sets:
            params.append(row["id"])
            conn.execute(f"UPDATE entity SET {', '.join(sets)} WHERE id=?", params)
        return row["id"], False
    eid = insert_entity(conn, Entity(origin="source", name=name, entity_type=entity_type,
                                     external_id=external_id), now=now)
    return eid, True


def insert_entity_membership(conn, m: EntityMembership, source_query_id: str | None,
                             now: str | None = None) -> str:
    _claim_provenance(source_query_id, m.source, "entity_membership")
    conn.execute(
        """INSERT INTO entity_membership
             (id, entity_id, address_id, source, method, confidence, flags, created_at,
              source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (m.id, m.entity_id, m.address_id, m.source, m.method, m.confidence, m.flags,
         now or utc_now_iso(), source_query_id),
    )
    return m.id


def upsert_entity_membership(conn, m: EntityMembership, source_query_id: str | None,
                             now: str | None = None) -> str:
    """Idempotent membership insert keyed on ``(entity_id, address_id, source, method)`` (Invariant #7).
    Re-ingesting the same link is a no-op (mirrors the entities service's insert-once idiom); returns
    the existing-or-new id. Distinct sources/methods stay side-by-side (Invariant #4)."""
    _claim_provenance(source_query_id, m.source, "entity_membership")
    existing = conn.execute(
        "SELECT id FROM entity_membership WHERE entity_id=? AND address_id=? AND source=? AND method=?",
        (m.entity_id, m.address_id, m.source, m.method),
    ).fetchone()
    if existing is not None:
        return existing[0]
    return insert_entity_membership(conn, m, source_query_id, now=now)


def insert_entity_membership_retraction(conn, r: EntityMembershipRetraction,
                                        source_query_id: str | None, now: str | None = None) -> str:
    _claim_provenance(source_query_id, r.source, "entity_membership_retraction")
    conn.execute(
        """INSERT INTO entity_membership_retraction
             (id, membership_id, reason, source, method, created_at, source_query_id)
           VALUES (?,?,?,?,?,?,?)""",
        (r.id, r.membership_id, r.reason, r.source, r.method, now or utc_now_iso(), source_query_id),
    )
    return r.id


# ----------------------------------------------------------------- Family C: investigator objects
# No source_query_id (investigator constructions, not sourced facts/claims). Append-only.

def insert_trace(conn, t: Trace, now: str | None = None) -> str:
    conn.execute(
        "INSERT INTO trace (id, name, description, created_at) VALUES (?,?,?,?)",
        (t.id, t.name, t.description, now or utc_now_iso()),
    )
    return t.id


def insert_trace_transfer(conn, tt: TraceTransfer) -> str:
    # LOG-07: insert-once on (trace_id, transfer_id) — re-adding the same edge to a trace is a no-op
    # (mirrors the claim tables' idiom), so trace construction is re-run-safe. FN-04: the dedup ignores
    # RETRACTED edges, so re-adding an edge that was retracted appends a FRESH active row (the retracted
    # one is append-only history) instead of returning the still-retracted id — "re-add after retract works".
    existing = conn.execute(
        "SELECT id FROM trace_transfer tt WHERE tt.trace_id=? AND tt.transfer_id=? "
        "AND NOT EXISTS (SELECT 1 FROM trace_transfer_retraction r WHERE r.trace_transfer_id=tt.id)",
        (tt.trace_id, tt.transfer_id)).fetchone()
    if existing is not None:
        return existing[0]
    conn.execute(
        "INSERT INTO trace_transfer (id, trace_id, transfer_id, ordering, note) VALUES (?,?,?,?,?)",
        (tt.id, tt.trace_id, tt.transfer_id, tt.ordering, tt.note),
    )
    return tt.id


def insert_trace_btc_link(conn, link: TraceBtcLink) -> str:
    # LOG-07: insert-once on (trace_id, transaction_id, source_output_id, dest_output_id, basis) — re-running
    # FIFO or re-adding a manual link on the same trace does not append duplicate rows (which would
    # double-count the linkage in the report/graph).
    # FN-04: the dedup ignores RETRACTED links (as insert_trace_transfer does), so re-adding a retracted
    # link appends a fresh active row rather than returning the still-retracted id.
    existing = conn.execute(
        "SELECT id FROM trace_btc_link l WHERE l.trace_id=? AND l.transaction_id=? "
        "AND l.source_output_id=? AND l.dest_output_id=? AND l.basis=? "
        "AND NOT EXISTS (SELECT 1 FROM trace_btc_link_retraction r WHERE r.trace_btc_link_id=l.id)",
        (link.trace_id, link.transaction_id, link.source_output_id, link.dest_output_id, link.basis),
    ).fetchone()
    if existing is not None:
        return existing[0]
    conn.execute(
        """INSERT INTO trace_btc_link
             (id, trace_id, transaction_id, source_output_id, dest_output_id, basis, confidence,
              ordering, note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (link.id, link.trace_id, link.transaction_id, link.source_output_id, link.dest_output_id,
         link.basis, link.confidence, link.ordering, link.note),
    )
    return link.id


def insert_trace_bridge_link(conn, link: TraceBridgeLink, now: str | None = None) -> str:
    # FN-17: a manual cross-chain bridge crossing (Family C — no source_query_id). Each side is a poly ref
    # to a value movement; the CHECK constraints + the no-dangling-fk audit keep the refs honest.
    conn.execute(
        """INSERT INTO trace_bridge_link
             (id, trace_id, src_subject_type, src_subject_id, dst_subject_type, dst_subject_id, basis,
              confidence, ordering, note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (link.id, link.trace_id, link.src_subject_type, link.src_subject_id, link.dst_subject_type,
         link.dst_subject_id, link.basis, link.confidence, link.ordering, link.note, now or utc_now_iso()),
    )
    return link.id


def insert_trace_transfer_retraction(conn, r: TraceTransferRetraction, now: str | None = None) -> str:
    # FN-04: append-only withdrawal of an EVM trace edge (the edge row is never deleted). Family C — no
    # source_query_id (an investigator construction, like the trace itself).
    conn.execute(
        "INSERT INTO trace_transfer_retraction (id, trace_transfer_id, reason, source, created_at) "
        "VALUES (?,?,?,?,?)",
        (r.id, r.trace_transfer_id, r.reason, r.source, now or utc_now_iso()),
    )
    return r.id


def insert_trace_btc_link_retraction(conn, r: TraceBtcLinkRetraction, now: str | None = None) -> str:
    # FN-04: append-only withdrawal of a Bitcoin trace link (mirrors insert_trace_transfer_retraction).
    conn.execute(
        "INSERT INTO trace_btc_link_retraction (id, trace_btc_link_id, reason, source, created_at) "
        "VALUES (?,?,?,?,?)",
        (r.id, r.trace_btc_link_id, r.reason, r.source, now or utc_now_iso()),
    )
    return r.id


def insert_finding(conn, f: Finding, now: str | None = None) -> str:
    conn.execute(
        "INSERT INTO finding (id, statement, assessment, created_at) VALUES (?,?,?,?)",
        (f.id, f.statement, f.assessment, now or utc_now_iso()),
    )
    return f.id


def insert_finding_ref(conn, fr: FindingRef) -> str:
    conn.execute(
        "INSERT INTO finding_ref (id, finding_id, ref_type, ref_id, note) VALUES (?,?,?,?,?)",
        (fr.id, fr.finding_id, fr.ref_type, fr.ref_id, fr.note),
    )
    return fr.id


def update_finding(conn, finding_id: str, *, statement: str, assessment: str | None) -> None:
    """Edit a finding in place — findings are investigator constructions, editable until reported (the
    report is a frozen snapshot; a later report supersedes rather than edits). Not a sourced claim, so
    this is outside the append-only-claims audit."""
    conn.execute("UPDATE finding SET statement=?, assessment=? WHERE id=?",
                 (statement, assessment, finding_id))


def delete_finding(conn, finding_id: str) -> None:
    # A finding is an app-enforced poly-ref TARGET for annotations (ANNOTATION_TARGET_TYPES includes
    # 'finding') with no DB-level FK/cascade, so a note ABOUT this finding would dangle (and fail the
    # no-dangling-fk audit) if left behind. Clear those notes + the finding's own refs, then the finding.
    conn.execute("DELETE FROM annotation WHERE target_type='finding' AND target_id=?", (finding_id,))
    conn.execute("DELETE FROM finding_ref WHERE finding_id=?", (finding_id,))
    conn.execute("DELETE FROM finding WHERE id=?", (finding_id,))


def delete_finding_ref(conn, ref_id: str) -> None:
    conn.execute("DELETE FROM finding_ref WHERE id=?", (ref_id,))


def insert_annotation(conn, a: Annotation, now: str | None = None) -> str:
    conn.execute(
        "INSERT INTO annotation (id, target_type, target_id, content, created_at) VALUES (?,?,?,?,?)",
        (a.id, a.target_type, a.target_id, a.content, now or utc_now_iso()),
    )
    return a.id


def update_annotation(conn, annotation_id: str, *, content: str) -> None:
    """Edit an annotation's text in place — annotations are investigator constructions (Family C),
    editable like findings (NOT a sourced claim, so outside the append-only-claims audit). The target
    + created_at are unchanged; only the note text is rewritten."""
    conn.execute("UPDATE annotation SET content=? WHERE id=?", (content, annotation_id))


def delete_annotation(conn, annotation_id: str) -> None:
    conn.execute("DELETE FROM annotation WHERE id=?", (annotation_id,))


def insert_tag(conn, t: Tag, now: str | None = None) -> str:
    conn.execute(
        "INSERT INTO tag (id, target_type, target_id, label, created_at) VALUES (?,?,?,?,?)",
        (t.id, t.target_type, t.target_id, t.label, now or utc_now_iso()),
    )
    return t.id


def insert_investigator_label(conn, lbl: InvestigatorLabel, now: str | None = None) -> str:
    """Append a display-label override (Family C; no source_query_id). The CURRENT label for a target
    is the most-recent row — every rename is a new immutable row (history preserved)."""
    conn.execute(
        "INSERT INTO investigator_label (id, target_type, target_id, label, created_at) VALUES (?,?,?,?,?)",
        (lbl.id, lbl.target_type, lbl.target_id, lbl.label, now or utc_now_iso()),
    )
    return lbl.id


def current_investigator_labels(conn, target_type: str) -> dict[str, str]:
    """Map ``target_id -> current display label`` for ``target_type`` (latest row per target wins)."""
    out: dict[str, str] = {}
    # Order by created_at then INSERTION order (rowid) so a tie on the (second-precision) timestamp
    # still resolves to the most-recently-written row — the dict overwrite then keeps the latest.
    for r in conn.execute(
        "SELECT target_id, label FROM investigator_label WHERE target_type=? ORDER BY created_at, rowid",
        (target_type,),
    ).fetchall():
        out[r["target_id"]] = r["label"]   # ascending order -> the last (most-recent) row wins
    return out


def insert_report(conn, r: Report, now: str | None = None) -> str:
    """Append a frozen report row (Family C). A later report SUPERSEDES via supersedes_report_id;
    an existing report is never edited (Invariant: reports are immutable snapshots)."""
    import json

    conn.execute(
        """INSERT INTO report
             (id, title, generated_at, scope_spec, rendered_file_ref, content_hash,
              supersedes_report_id)
           VALUES (?,?,?,?,?,?,?)""",
        (r.id, r.title, now or utc_now_iso(), json.dumps(r.scope_spec, sort_keys=True),
         r.rendered_file_ref, r.content_hash, r.supersedes_report_id),
    )
    return r.id
