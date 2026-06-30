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
    Tag,
    Trace,
    TraceBtcLink,
    TraceTransfer,
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
             symbol   = excluded.symbol,
             decimals = excluded.decimals""",
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


def upsert_transaction(conn, tx: Transaction, source_query_id: str | None) -> str:
    """Upsert a transaction on (chain, tx_hash). A FINAL existing row is frozen (no update)."""
    _require_provenance(source_query_id, "transaction_")
    existing = conn.execute(
        "SELECT id, finality_status FROM transaction_ WHERE chain=? AND tx_hash=?",
        (tx.chain, tx.tx_hash),
    ).fetchone()
    if existing is not None and existing["finality_status"] == "final":
        return existing["id"]  # immutable once final (Invariant #6) — refetch is a no-op
    conn.execute(
        """INSERT INTO transaction_
             (id, chain, tx_hash, block_height, block_ts, fee, status, confirmations,
              finality_status, source_query_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(chain, tx_hash) DO UPDATE SET
             block_height    = COALESCE(excluded.block_height, block_height),
             block_ts        = COALESCE(excluded.block_ts, block_ts),
             fee             = COALESCE(excluded.fee, fee),
             status          = COALESCE(excluded.status, status),
             confirmations   = excluded.confirmations,
             finality_status = excluded.finality_status""",
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
           ON CONFLICT(transaction_id, output_index) DO UPDATE SET
             spent          = excluded.spent,
             spending_tx_id = excluded.spending_tx_id""",
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
    conn.execute(
        "INSERT INTO trace_transfer (id, trace_id, transfer_id, ordering, note) VALUES (?,?,?,?,?)",
        (tt.id, tt.trace_id, tt.transfer_id, tt.ordering, tt.note),
    )
    return tt.id


def insert_trace_btc_link(conn, link: TraceBtcLink) -> str:
    conn.execute(
        """INSERT INTO trace_btc_link
             (id, trace_id, transaction_id, source_output_id, dest_output_id, basis, confidence,
              ordering, note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (link.id, link.trace_id, link.transaction_id, link.source_output_id, link.dest_output_id,
         link.basis, link.confidence, link.ordering, link.note),
    )
    return link.id


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
