"""Etherscan V2 -> canonical mapping (phase_02 step 2; docs/connectors.md §2).

Pure functions (no HTTP, no DB) so the contract test can replay a recorded cassette and assert
the exact canonical rows. Output is a list of :class:`ParsedTransaction` bundles (a canonical
``Transaction`` + its ``ParsedTransfer`` children carrying addresses as canonical strings and an
``Asset`` descriptor); the writer resolves those to DB ids.

A transfer is only created for a movement that actually happened: a value>0 row whose tx (or
internal call) **succeeded**. A reverted tx (``isError=1``) moves no value, so it yields a
``transaction_`` row (status='failed') with NO transfer — never fabricate a movement.

Position conventions. ``position`` is a source-reported DISPLAY ordinal (receipt-log order); the transfer
DEDUP key is content + ``occurrence`` (migration 0007 / ``normalization/reconcile.py``), NOT position:
- **native** (txlist): one native value move per tx -> ``position=0``.
- **internal** (txlistinternal): rows are sorted by ``traceId`` first, then ``position`` = the
  row's index within its tx — deterministic regardless of the order Etherscan returns them, so
  re-fetch is idempotent.
- **erc20** (tokentx): ``position`` = the row's index within its tx's token rows. Etherscan has
  no per-log ordinal here, so this relies on Etherscan returning token transfers in receipt-log
  order (deterministic for a historical tx).

Confirmed against live docs 2026-06-26: envelope ``{status,message,result}``; field names per
endpoint. ``status:"0"`` + list result = no records; ``status:"0"`` + string result = error
(handled by the connector, not here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import Asset, BalanceSnapshot, Transaction
from .canonical import canonical_address
from .finality import finality_for

# EVM native coin symbol per chain (all 18-decimal). Default ETH for L2s that use ETH gas.
NATIVE_SYMBOL = {
    "ethereum": "ETH", "arbitrum": "ETH", "optimism": "ETH", "base": "ETH", "polygon": "POL",
    "bsc": "BNB",
}


@dataclass
class ParsedTransfer:
    chain: str
    from_address: str | None   # canonical, None for mint/contract-creation
    to_address: str | None     # canonical, None for burn/contract-creation
    asset: Asset
    amount: str
    transfer_type: str         # native|internal|erc20
    position: int              # source-reported display ordinal
    occurrence: int = 0        # set by normalization.reconcile.assign_occurrences before the DB write
    # COR-02: the SOURCE display form (EIP-55 checksum) so the repository choke-point preserves it in
    # `address_display` while still keying on the canonical form. None → fall back to the canonical form
    # (the connector passes `from_address_display or from_address` to upsert_address).
    from_address_display: str | None = None
    to_address_display: str | None = None
    # A source-reported value-at-time (total USD) for this movement, when the export carries one (e.g.
    # Arkham `historicalUSD`). Drives a SECOND sourced `valuation` claim alongside DeFiLlama — never
    # merged (Invariant #4). None when the source did not price the movement (an honest gap, no row).
    historical_usd: str | None = None


@dataclass
class ParsedTransaction:
    transaction: Transaction
    transfers: list[ParsedTransfer] = field(default_factory=list)


def _iso(unix_ts: str | int) -> str:
    return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon_or_none(chain: str, addr: str | None) -> str | None:
    if not addr or not str(addr).strip():
        return None
    return canonical_address(chain, addr)


# ERC-20 Approval(address indexed owner, address indexed spender, uint256 value) — LOG-06.
# topic0 = keccak256("Approval(address,address,uint256)") (confirmed via 4byte.directory).
APPROVAL_TOPIC0 = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"


def _topic_address(chain: str, topic: str | None) -> str | None:
    """A 32-byte indexed-address topic left-pads the 20-byte address — take the last 40 hex chars."""
    h = (topic or "").lower().removeprefix("0x")
    if len(h) < 40:
        return None
    try:
        return canonical_address(chain, "0x" + h[-40:])
    except ValueError:
        return None


def owner_topic(chain: str, address: str) -> str:
    """The 32-byte left-padded topic for filtering getLogs by the approval OWNER (topic1)."""
    return "0x" + canonical_address(chain, address)[2:].rjust(64, "0")


def adapt_approval_logs(rows: list[dict], *, chain: str) -> list[dict]:
    """Decode Etherscan getLogs ``Approval`` rows → ``{owner, spender, token, amount, block_height,
    tx_hash}`` (owner/spender/token canonical, amount as base-unit text). Non-Approval / malformed rows
    are skipped."""
    out = []
    for r in rows:
        topics = r.get("topics") or []
        if len(topics) < 3 or (topics[0] or "").lower() != APPROVAL_TOPIC0:
            continue
        owner = _topic_address(chain, topics[1])
        spender = _topic_address(chain, topics[2])
        if not owner or not spender:
            continue
        try:
            token = canonical_address(chain, r["address"])
        except (KeyError, ValueError):
            continue
        try:
            amount = str(int((r.get("data") or "0x0"), 16))
        except (ValueError, TypeError):
            amount = None
        try:
            block = int(r["blockNumber"], 16)
        except (KeyError, ValueError, TypeError):
            block = None
        out.append({"owner": owner, "spender": spender, "token": token, "amount": amount,
                    "block_height": block, "tx_hash": r.get("transactionHash")})
    return out


def _display_or_none(addr: str | None) -> str | None:
    """The trimmed SOURCE form of an address (EIP-55 checksum preserved), or None (COR-02)."""
    s = str(addr).strip() if addr is not None else ""
    return s or None


def native_asset(chain: str) -> Asset:
    return Asset(chain=chain, contract_address=None,
                 symbol=NATIVE_SYMBOL.get(chain.lower(), "ETH"), decimals=18)


def _status_from_iserror(row: dict) -> str:
    return "failed" if str(row.get("isError", "0")) == "1" else "success"


def _fee_from(row: dict) -> str | None:
    gu, gp = row.get("gasUsed"), row.get("gasPrice")
    if gu is None or gp is None:
        return None
    return str(int(gu) * int(gp))


def _assign_group_positions(rows: list[dict]) -> list[tuple[dict, int]]:
    """Pair each row with its 0-based index within its tx (``hash``) group, in input order."""
    counters: dict[str, int] = {}
    out = []
    for row in rows:
        h = row["hash"]
        pos = counters.get(h, 0)
        counters[h] = pos + 1
        out.append((row, pos))
    return out


def _traceid_sort_key(row: dict):
    """Stable ordering key for internal rows: (hash, hierarchical traceId as int tuple).

    Etherscan traceIds are like ``0``, ``1``, ``0_1`` (hierarchical); sorting by the int-tuple
    makes per-tx positions deterministic regardless of the order the API returns rows.
    """
    parts = []
    for p in str(row.get("traceId", "0")).split("_"):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return (row["hash"], tuple(parts))


def _txn(row: dict, *, chain: str, tip_height: int | None, threshold: int,
         fee: str | None, status: str) -> Transaction:
    block_height = int(row["blockNumber"])
    confirmations, finality = finality_for(tip_height=tip_height, block_height=block_height,
                                           threshold=threshold)
    return Transaction(
        chain=chain, tx_hash=row["hash"], block_height=block_height, block_ts=_iso(row["timeStamp"]),
        fee=fee, status=status, confirmations=confirmations, finality_status=finality,
    )


def adapt_txlist(rows: list[dict], *, chain: str, tip_height: int | None,
                 threshold: int) -> list[ParsedTransaction]:
    """Normal transactions -> tx + a single native transfer (when value>0), position 0."""
    out = []
    for row in rows:
        status = _status_from_iserror(row)
        txn = _txn(row, chain=chain, tip_height=tip_height, threshold=threshold,
                   fee=_fee_from(row), status=status)
        pt = ParsedTransaction(transaction=txn)
        # A reverted tx moves no value — record the tx, but never fabricate a transfer.
        if int(row["value"]) != 0 and status == "success":
            pt.transfers.append(ParsedTransfer(
                chain=chain, from_address=_canon_or_none(chain, row.get("from")),
                to_address=_canon_or_none(chain, row.get("to")), asset=native_asset(chain),
                amount=row["value"], transfer_type="native", position=0,
                from_address_display=_display_or_none(row.get("from")),
                to_address_display=_display_or_none(row.get("to"))))
        out.append(pt)
    return out


def adapt_txlistinternal(rows: list[dict], *, chain: str, tip_height: int | None,
                         threshold: int) -> list[ParsedTransaction]:
    """Internal transactions -> tx (fee left to txlist) + internal transfers (succeeded, value>0).

    Sorted by traceId so per-tx positions are deterministic across re-fetches.
    """
    by_tx: dict[str, ParsedTransaction] = {}
    for row, position in _assign_group_positions(sorted(rows, key=_traceid_sort_key)):
        h = row["hash"]
        if h not in by_tx:
            by_tx[h] = ParsedTransaction(transaction=_txn(
                row, chain=chain, tip_height=tip_height, threshold=threshold,
                fee=None, status=_status_from_iserror(row)))
        # A reverted internal call moves no value.
        if int(row["value"]) != 0 and _status_from_iserror(row) == "success":
            by_tx[h].transfers.append(ParsedTransfer(
                chain=chain, from_address=_canon_or_none(chain, row.get("from")),
                to_address=_canon_or_none(chain, row.get("to")), asset=native_asset(chain),
                amount=row["value"], transfer_type="internal", position=position,
                from_address_display=_display_or_none(row.get("from")),
                to_address_display=_display_or_none(row.get("to"))))
    return list(by_tx.values())


def adapt_tokentx(rows: list[dict], *, chain: str, tip_height: int | None,
                  threshold: int) -> list[ParsedTransaction]:
    """ERC-20 transfers -> tx (success; events emit only on success) + erc20 transfers + assets."""
    by_tx: dict[str, ParsedTransaction] = {}
    for row, position in _assign_group_positions(rows):
        h = row["hash"]
        if h not in by_tx:
            by_tx[h] = ParsedTransaction(transaction=_txn(
                row, chain=chain, tip_height=tip_height, threshold=threshold,
                fee=_fee_from(row), status="success"))
        decimals = int(row["tokenDecimal"]) if str(row.get("tokenDecimal", "")).strip() else 0
        asset = Asset(chain=chain, contract_address=canonical_address(chain, row["contractAddress"]),
                      symbol=row.get("tokenSymbol"), decimals=decimals)
        by_tx[h].transfers.append(ParsedTransfer(
            chain=chain, from_address=_canon_or_none(chain, row.get("from")),
            to_address=_canon_or_none(chain, row.get("to")), asset=asset,
            amount=row["value"], transfer_type="erc20", position=position,
            from_address_display=_display_or_none(row.get("from")),
            to_address_display=_display_or_none(row.get("to"))))
    return list(by_tx.values())


def adapt_balance(result: str, *, chain: str, address: str, as_of_ts: str,
                  source: str = "etherscan") -> tuple[str, BalanceSnapshot]:
    """Native balance string (wei) -> (canonical_address, BalanceSnapshot with asset_id=None)."""
    canonical = canonical_address(chain, address)
    snap = BalanceSnapshot(address_id="", asset_id=None, amount=str(result), as_of_ts=as_of_ts,
                           source=source, retrieved_at=as_of_ts)
    return canonical, snap
