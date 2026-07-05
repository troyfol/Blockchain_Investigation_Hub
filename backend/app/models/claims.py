"""Canonical sourced-claim models (Family B) + entity objects.

Claims are append-only and preserved side-by-side, never collapsed (Invariant #4). Provenance
is assigned at write time, except investigator-authored attribution/membership (source=
'investigator'), whose `source_query_id` may be NULL.
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    return str(uuid4())


class Attribution(BaseModel):
    id: str = Field(default_factory=_new_id)
    address_id: str
    label: str
    category: str | None = None
    source: str  # arkham|misttrack|breadcrumbs|investigator|...
    confidence: float | None = None
    note: str | None = None
    retrieved_at: str


class RiskAssessment(BaseModel):
    id: str = Field(default_factory=_new_id)
    address_id: str
    score: float | None = None
    score_scale: str | None = None
    category: str | None = None
    rationale: str | None = None
    source: str
    retrieved_at: str


class RiskDetail(BaseModel):
    """FN-15: one per-sub-signal row of a `RiskAssessment` breakdown (e.g. mixer/hacker/sanctions), stored
    RAW and never collapsed/averaged (Invariant #4). A child of one parent risk_assessment; its provenance
    is the parent's source_query (written in the same txn). Idempotent on (risk_assessment_id, signal)."""
    id: str = Field(default_factory=_new_id)
    risk_assessment_id: str
    signal: str                       # the source's own sub-signal key (e.g. 'mixer', 'hacker')
    score: float | None = None
    score_scale: str | None = None


class Valuation(BaseModel):
    id: str = Field(default_factory=_new_id)
    subject_type: Literal["transfer", "tx_output"]
    subject_id: str
    currency: str = "USD"
    unit_price: str
    value: str
    price_timestamp: str
    confidence: float | None = None
    source: str = "defillama"
    retrieved_at: str


class BalanceSnapshot(BaseModel):
    id: str = Field(default_factory=_new_id)
    address_id: str
    asset_id: str | None = None  # None = native/aggregate
    amount: str
    as_of_ts: str
    source: str
    retrieved_at: str


class Entity(BaseModel):
    id: str = Field(default_factory=_new_id)
    name: str | None = None  # None for auto co-spend clusters
    entity_type: str | None = None
    origin: Literal["cospend-cluster", "source", "investigator", "heuristic-cluster"]
    merged_into: str | None = None
    canonical_membership_id: str | None = None
    external_id: str | None = None  # upstream id for origin='source' (e.g. a GraphSense actor id)


class EntityMembership(BaseModel):
    id: str = Field(default_factory=_new_id)
    entity_id: str
    address_id: str
    source: str  # arkham|cospend-heuristic|same-address-heuristic|investigator
    method: str  # shared-label|co-spend|same-address-heuristic|manual
    confidence: float | None = None
    flags: str | None = None


class EntityMembershipRetraction(BaseModel):
    id: str = Field(default_factory=_new_id)
    membership_id: str
    reason: str
    source: str
    method: str | None = None
