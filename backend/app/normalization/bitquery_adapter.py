"""Bitquery V2 EVM GraphQL -> canonical transfer mapping (pure; no HTTP, no DB).

A multi-chain EVM **facts** source (a fallback when Etherscan's free chain coverage shrinks). Output is
the same `ParsedTransaction`/`ParsedTransfer` shape the Etherscan/Arkham adapters produce, so the same
DB writer resolves ids.

`TODO: confirm` — the GraphQL query body + every response field path below are UNCONFIRMED against the
live API (no token at build to record a response). Reads are defensive (`.get`) so a path mismatch
degrades to "no rows" rather than crashing; the RUN_LIVE drift test (`tests/contract/test_live_drift.py`)
is the path that confirms/repairs them. No fabricated cassette is used (per the build directive).
Finality: Bitquery does not return confirmations, so facts are marked `provisional` (honest — upgraded by
an idempotent chain re-fetch, Invariant #6), never frozen `final` on a guess.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from ..models import Asset, Transaction
from .canonical import canonical_address, to_canonical_ts
from .etherscan_adapter import NATIVE_SYMBOL, ParsedTransaction, ParsedTransfer, _display_or_none

# Canonical chain -> Bitquery V2 EVM `network` slug. TODO: confirm the exact slugs (40+ supported).
CHAIN_TO_NETWORK: dict[str, str] = {
    "ethereum": "eth", "bsc": "bsc", "base": "base", "arbitrum": "arbitrum",
    "optimism": "optimism", "polygon": "matic",
}

# The GraphQL query. TODO: confirm field names against the live V2 EVM schema before production.
TRANSFERS_QUERY = """
query ($network: evm_network, $address: String!, $limit: Int!) {
  EVM(dataset: archive, network: $network) {
    Transfers(
      where: {any: [{Transfer: {Sender: {is: $address}}}, {Transfer: {Receiver: {is: $address}}}]}
      limit: {count: $limit}
      orderBy: {descending: Block_Time}
    ) {
      Transaction { Hash }
      Transfer { Sender Receiver Amount Currency { Symbol SmartContract Decimals } }
      Block { Number Time }
    }
  }
}
""".strip()


def network_for(chain: str) -> str | None:
    return CHAIN_TO_NETWORK.get(chain.lower())


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _canon_or_none(chain: str, addr) -> str | None:
    addr = (addr or "").strip() if isinstance(addr, str) else ""
    if not addr:
        return None
    try:
        return canonical_address(chain, addr)
    except ValueError:
        return None


def _to_base_units(display_value, decimals: int) -> str:
    """Bitquery returns a DECIMAL display Amount; convert to a base-unit integer (Decimal, half-even).
    TODO: confirm whether Amount is display or base units. Defensive: unparseable -> '0'."""
    try:
        scaled = Decimal(str(display_value)) * (Decimal(10) ** decimals)
        return str(int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN)))
    except (InvalidOperation, ValueError, TypeError):
        return "0"


def adapt_transfers(payload: dict, *, chain: str) -> tuple[list[ParsedTransaction], dict]:
    """Map a Bitquery Transfers response to canonical bundles. Returns ``(bundles, notes)``. Every field
    path is TODO: confirm; defensive reads keep it from crashing on a shape mismatch."""
    notes = {"rows": 0, "transfers": 0, "skipped": 0}
    rows = (((payload.get("data") or {}).get("EVM") or {}).get("Transfers")) or []
    by_tx: dict[str, ParsedTransaction] = {}
    pos: dict[tuple, int] = {}

    for row in rows:
        notes["rows"] += 1
        if not isinstance(row, dict):
            notes["skipped"] += 1
            continue
        tx = row.get("Transaction") or {}
        tr = row.get("Transfer") or {}
        blk = row.get("Block") or {}
        tx_hash = (tx.get("Hash") or "").strip() if isinstance(tx.get("Hash"), str) else ""
        if not tx_hash:
            notes["skipped"] += 1
            continue

        cur = tr.get("Currency") or {}
        decimals = _int(cur.get("Decimals"), default=18)
        contract = (cur.get("SmartContract") or "").strip() if isinstance(cur.get("SmartContract"), str) else ""
        # Native sentinel TODO: confirm (Bitquery often uses the zero address or a missing contract).
        is_native = (not contract) or contract.lower() in ("0x", "0x0000000000000000000000000000000000000000")
        transfer_type = "native" if is_native else "erc20"
        symbol = (cur.get("Symbol") or "").strip() or (
            NATIVE_SYMBOL.get(chain.lower(), "ETH") if is_native else None)

        amount = _to_base_units(tr.get("Amount"), decimals)
        from_addr = _canon_or_none(chain, tr.get("Sender"))
        to_addr = _canon_or_none(chain, tr.get("Receiver"))
        asset = Asset(chain=chain, contract_address=(None if is_native else _canon_or_none(chain, contract)),
                      symbol=symbol, decimals=decimals)

        if tx_hash not in by_tx:
            by_tx[tx_hash] = ParsedTransaction(transaction=Transaction(
                chain=chain, tx_hash=tx_hash, block_height=_int(blk.get("Number")),
                block_ts=to_canonical_ts(blk.get("Time")), fee=None, status=None,  # LOG-05: canonical ts
                confirmations=None, finality_status="provisional"))  # no confirmations from Bitquery
        key = (tx_hash, transfer_type)
        position = pos.get(key, 0)
        pos[key] = position + 1
        by_tx[tx_hash].transfers.append(ParsedTransfer(
            chain=chain, from_address=from_addr, to_address=to_addr, asset=asset,
            amount=amount, transfer_type=transfer_type, position=position,
            from_address_display=_display_or_none(tr.get("Sender")),        # COR-02: keep EIP-55 checksum
            to_address_display=_display_or_none(tr.get("Receiver"))))
        notes["transfers"] += 1

    return list(by_tx.values()), notes
