"""Esplora (Bitcoin/UTXO) -> canonical mapping (phase_03 step 1; docs/connectors.md §4).

Pure functions (no HTTP, no DB). A Bitcoin transaction maps to a ``transaction_`` node plus
``tx_input`` / ``tx_output`` rows ONLY — **never** a synthesized vin->vout transfer (Invariant
#5). The transaction is a visible routing node; any input->output linkage is a trace-time claim,
not a ledger fact.

Confirmed against live Esplora docs 2026-06-26, RE-CONFIRMED 2026-06-28: ``/address/:a/txs`` returns
full tx objects (``txid, version, locktime, size, weight, fee, status{confirmed,block_height,
block_time}, vin[], vout[]``); amounts are in satoshis; ``vin[].prevout`` carries
``scriptpubkey_address`` + ``value``; ``vout[]`` carries ``scriptpubkey_address`` (absent for
non-standard scripts -> NULL) + ``value``. A mempool tx has ``status.confirmed=false`` and no
``block_height`` -> ``finality_for`` yields 0 confirmations / ``provisional``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import Transaction
from .canonical import canonical_address

BTC_NATIVE_SYMBOL = "BTC"
BTC_DECIMALS = 8


@dataclass
class ParsedTxInput:
    chain: str
    address: str | None       # canonical; None for non-standard scripts / coinbase
    amount: str               # satoshis
    input_index: int
    prev_txid: str | None     # the spent output's tx (to resolve prev_output_id if in-DB)
    prev_vout: int | None


@dataclass
class ParsedTxOutput:
    chain: str
    address: str | None       # None for non-standard scripts
    amount: str
    output_index: int


@dataclass
class ParsedBtcTx:
    transaction: Transaction
    inputs: list[ParsedTxInput] = field(default_factory=list)
    outputs: list[ParsedTxOutput] = field(default_factory=list)


def _iso(unix_ts) -> str | None:
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon_or_none(chain: str, addr) -> str | None:
    if not addr or not str(addr).strip():
        return None
    return canonical_address(chain, addr)


def adapt_address_txs(txs: list[dict], *, chain: str, tip_height: int | None,
                      threshold: int) -> list[ParsedBtcTx]:
    """Map Esplora tx objects to ParsedBtcTx (transaction + inputs + outputs)."""
    from .finality import finality_for

    out = []
    for tx in txs:
        status = tx.get("status") or {}
        block_height = status.get("block_height")  # None = unconfirmed/mempool
        confirmations, finality = finality_for(tip_height=tip_height, block_height=block_height,
                                               threshold=threshold)
        txn = Transaction(
            chain=chain, tx_hash=tx["txid"], block_height=block_height,
            block_ts=_iso(status.get("block_time")),
            fee=str(tx["fee"]) if tx.get("fee") is not None else None,
            status="confirmed" if status.get("confirmed") else "mempool",
            confirmations=confirmations, finality_status=finality,
        )
        parsed = ParsedBtcTx(transaction=txn)
        for idx, vin in enumerate(tx.get("vin") or []):
            if vin.get("is_coinbase"):
                # Coinbase: newly minted coins, no prevout/address/value.
                parsed.inputs.append(ParsedTxInput(chain, None, "0", idx, None, None))
                continue
            prevout = vin.get("prevout") or {}
            parsed.inputs.append(ParsedTxInput(
                chain, _canon_or_none(chain, prevout.get("scriptpubkey_address")),
                str(prevout.get("value", 0)), idx, vin.get("txid"), vin.get("vout")))
        for idx, vout in enumerate(tx.get("vout") or []):
            parsed.outputs.append(ParsedTxOutput(
                chain, _canon_or_none(chain, vout.get("scriptpubkey_address")),
                str(vout.get("value", 0)), idx))
        out.append(parsed)
    return out


def balance_from_stats(payload: dict) -> int:
    """Confirmed balance (sats) = chain_stats.funded_txo_sum - spent_txo_sum (docs/connectors.md §4)."""
    cs = payload.get("chain_stats") or {}
    return int(cs.get("funded_txo_sum", 0)) - int(cs.get("spent_txo_sum", 0))
