"""Canonical Pydantic models (the shape connectors produce; provenance assigned at write)."""

from .claims import (
    Attribution,
    BalanceSnapshot,
    Entity,
    EntityMembership,
    EntityMembershipRetraction,
    RiskAssessment,
    Valuation,
)
from .investigator import (
    Annotation,
    Finding,
    FindingRef,
    InvestigatorLabel,
    Report,
    Tag,
    Trace,
    TraceBtcLink,
    TraceTransfer,
)
from .onchain import Address, Asset, Erc20Approval, Transaction, Transfer, TxInput, TxOutput
from .provenance import Exhibit, SourceQuery

__all__ = [
    # provenance
    "SourceQuery",
    "Exhibit",
    # on-chain facts (Family A)
    "Asset",
    "Address",
    "Transaction",
    "Transfer",
    "TxInput",
    "TxOutput",
    "Erc20Approval",
    # sourced claims + entities (Family B)
    "Attribution",
    "RiskAssessment",
    "Valuation",
    "BalanceSnapshot",
    "Entity",
    "EntityMembership",
    "EntityMembershipRetraction",
    # investigator objects (Family C)
    "Trace",
    "TraceTransfer",
    "TraceBtcLink",
    "Finding",
    "FindingRef",
    "Annotation",
    "Tag",
    "InvestigatorLabel",
    "Report",
]
