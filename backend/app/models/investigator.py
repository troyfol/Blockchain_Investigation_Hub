"""Canonical investigator-constructed object models (Family C; phase_08).

These carry NO ``source_query_id`` — they are the investigator's own constructions, not sourced
facts/claims. Bitcoin input->output linkage lives ONLY here, as a ``TraceBtcLink`` with an explicit
``basis`` (``fifo`` convention | ``investigator`` override) — never a ledger fact (Invariant #5).
Tags (investigator) are deliberately a separate object from attributions (source).
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

FINDING_REF_TYPES = ("address", "transfer", "transaction", "tx_output", "trace", "exhibit", "entity")
ANNOTATION_TARGET_TYPES = ("address", "transfer", "transaction", "tx_output", "trace", "entity", "finding")
TAG_TARGET_TYPES = ("address", "entity")
# An investigator display-label override is settable on a node (address or transaction) or a flow
# (transfer / tx_output) or a trace/path — anything the investigator can name on the canvas (migration
# 0009 widened the table's CHECK from the original address|trace).
INVESTIGATOR_LABEL_TARGET_TYPES = ("address", "trace", "transaction", "transfer", "tx_output")


def _new_id() -> str:
    return str(uuid4())


class Trace(BaseModel):
    id: str = Field(default_factory=_new_id)
    name: str
    description: str | None = None


class TraceTransfer(BaseModel):
    id: str = Field(default_factory=_new_id)
    trace_id: str
    transfer_id: str
    ordering: int | None = None
    note: str | None = None


class TraceBtcLink(BaseModel):
    id: str = Field(default_factory=_new_id)
    trace_id: str
    transaction_id: str
    source_output_id: str  # the prev tx_output an input spends (must be in-DB)
    dest_output_id: str    # an output of this transaction
    basis: Literal["fifo", "investigator"]
    confidence: float | None = None  # NULL for fifo — a convention, not a probability
    ordering: int | None = None
    note: str | None = None


class Finding(BaseModel):
    id: str = Field(default_factory=_new_id)
    statement: str
    assessment: str | None = None


class FindingRef(BaseModel):
    id: str = Field(default_factory=_new_id)
    finding_id: str
    ref_type: Literal["address", "transfer", "transaction", "tx_output", "trace", "exhibit", "entity"]
    ref_id: str
    note: str | None = None


class Annotation(BaseModel):
    id: str = Field(default_factory=_new_id)
    target_type: Literal["address", "transfer", "transaction", "tx_output", "trace", "entity", "finding"]
    target_id: str
    content: str


class Tag(BaseModel):
    id: str = Field(default_factory=_new_id)
    target_type: Literal["address", "entity"]
    target_id: str
    label: str


class InvestigatorLabel(BaseModel):
    """A display-label override the investigator sets on a node (address) or a trace/path.

    Append-only — the CURRENT display label for a target is the most-recent row. An investigator
    construction (Family C): no ``source_query_id``; the underlying facts are never touched.
    """

    id: str = Field(default_factory=_new_id)
    target_type: Literal["address", "trace", "transaction", "transfer", "tx_output"]
    target_id: str
    label: str


class Report(BaseModel):
    id: str = Field(default_factory=_new_id)
    title: str
    scope_spec: dict = Field(default_factory=dict)  # JSON; records applied expansion bounds
    rendered_file_ref: str                          # the self-contained report HTML under reports/
    #                                                 (portable, relative; the PDF is a derived artifact)
    content_hash: str                               # SHA-256 of the canonical report HTML (engine-
    #                                                 independent source of truth, not the PDF bytes)
    supersedes_report_id: str | None = None         # a later report supersedes, never edits
