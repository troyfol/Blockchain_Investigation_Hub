"""Canonical raw on-chain fact models (Family A).

Pure canonical records as connectors produce them. Provenance (`source_query_id`) is NOT a
field here — it is assigned atomically at write time by ``provenance/atomic.py``. Amounts are
raw base-unit integers carried as TEXT (satoshi/wei). EVM uses ``Transfer``; Bitcoin uses
``TxInput``/``TxOutput`` only (Invariant #5 — never a synthesized input->output transfer).
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    return str(uuid4())


class Asset(BaseModel):
    id: str = Field(default_factory=_new_id)
    chain: str
    contract_address: str | None = None  # None = native coin
    symbol: str | None = None
    decimals: int


class Address(BaseModel):
    id: str = Field(default_factory=_new_id)
    chain: str
    address_display: str  # original source form; repository derives the canonical `address`
    first_seen_ts: str | None = None


class Transaction(BaseModel):
    id: str = Field(default_factory=_new_id)
    chain: str
    tx_hash: str
    block_height: int | None = None  # None = unconfirmed/mempool
    block_ts: str | None = None
    fee: str | None = None
    status: str | None = None
    confirmations: int | None = None
    finality_status: Literal["provisional", "final"] = "provisional"


class Transfer(BaseModel):
    id: str = Field(default_factory=_new_id)
    transaction_id: str
    chain: str
    from_address_id: str | None = None  # None for mint
    to_address_id: str | None = None    # None for burn
    asset_id: str
    amount: str
    transfer_type: Literal["native", "erc20", "internal"]
    position: int                       # source-reported display ordinal (e.g. Etherscan log order)
    occurrence: int = 0                 # dedup ordinal among identical-content movements (decision (c))


class TxOutput(BaseModel):
    id: str = Field(default_factory=_new_id)
    transaction_id: str
    address_id: str | None = None  # None for non-standard scripts
    amount: str
    output_index: int
    spent: int = 0
    spending_tx_id: str | None = None


class TxInput(BaseModel):
    id: str = Field(default_factory=_new_id)
    transaction_id: str
    prev_output_id: str | None = None  # None if the spent output is not in-DB
    address_id: str | None = None
    amount: str
    input_index: int
