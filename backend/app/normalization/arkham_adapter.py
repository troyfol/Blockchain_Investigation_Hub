"""Arkham UI CSV export -> canonical EVM transfer mapping (Path A).

See `docs/findings/arkham_export_reconciliation.md`. The Arkham logged-in "download" export is a
**transfer log** (one row = one A→B value movement, 19 columns) — NOT the attribution schema the first
`arkham.py` was built against. This pure adapter (no HTTP/DB) maps **EVM** rows onto the canonical
`transfer` fact path, reusing etherscan_adapter's `ParsedTransaction`/`ParsedTransfer` shape so the DB
writer resolves addresses/assets to ids.

**Chain branch (Invariant #5 — critical).** Arkham emits the SAME `fromAddress → toAddress` shape for
Bitcoin (`chain=bitcoin`, `type=inflow`), and `fromAddress` is sometimes a comma-joined *set* of input
addresses — i.e. it collapses the UTXO input set into one from→to pair. Writing that as a `transfer`
fact would synthesize an input→output edge, which Invariant #5 forbids (BTC stores `tx_input`/`tx_output`
only, and this export doesn't carry the real UTXO structure). So this adapter only builds transfers for
**account-model (EVM) chains** (`NATIVE_SYMBOL`, incl. **bsc**). Non-EVM rows split into two classes the
connector treats differently: **UTXO** (Bitcoin → `notes["rejected_utxo"]`, a hard Invariant #5 refusal)
and **unsupported account-model** (e.g. **Tron** → `notes["rejected_unsupported"]` — account-model but
not modelled here; NOT a fabrication risk, so it's skipped, not an invariant breach). Arkham chain ids are
alias-normalized to the system's canonical names first (`ARKHAM_CHAIN_ALIASES`).

Resolved decisions (full reconciliation in the findings note; `TODO: confirm` where the export can't settle it):

(a) **`unitValue` is a DISPLAY value, not base units** (confirmed: BTC `unitValue=0.00000546`, decimals=8 →
    546 sats; EVM USDT `historicalUSD==unitValue==32` for a $1 token). `Transfer.amount` is a raw
    base-unit integer, so `amount = round(Decimal(unitValue) × 10^decimals)` — parsed via `Decimal`, never
    float. A product with MORE precision than `decimals` is flagged `rounded` and surfaced. **Honesty
    caveat:** the commoner loss — Arkham *truncating* a high-decimal token's display (e.g. showing
    `1.2346` for a true `1.234567…`) — yields an *integral* product and is therefore NOT flagged; such
    amounts are low-order-lossy and the authoritative value needs a chain re-fetch (Etherscan). *TODO:
    confirm exactness for high-decimal tokens.*

(b) **`type` is DIRECTION, not the transfer-type enum.** Arkham's `type` is `inflow`/`outflow` relative to
    the queried subject (empty when neither party is the subject) — NOT `native/erc20/internal`. We derive
    `transfer_type` from `tokenAddress` presence (token ⇒ `erc20`, else `native`). `internal` is not
    distinguishable from this export. *TODO: confirm `internal`/contract-interaction representation.*

(c) **`position`:** no log index in the export → `position` = the row's 0-based index within its
    `(tx_hash, transfer_type)` group, in CSV row order — so RE-INGESTING THE SAME FILE is idempotent
    (Invariant #7). Cross-source caveat (surfaced as a follow-up): these positions need not align with
    Etherscan's log-order positions, so the same tx from both sources could yield two transfer rows.

(d) **finality:** no `confirmations` column → every tx is `provisional` (honest: not confirmed), upgraded
    to `final` by a later idempotent chain re-fetch (Invariant #6). Never frozen final on a guess.

(e) **dropped columns** (no canonical slot; we do NOT extend the schema for import-only metadata):
    `blockHash`, `tokenName`, `from/toIsContract`, `from/toLabel`. `tokenId` is a **coin slug** (e.g.
    `tether`/`bitcoin`, DeFiLlama-style pricing key) — NOT an erc721 id — and is not needed for the raw
    transfer; it would only matter for a future valuation join.

(f) **`historicalUSD`** is a SOURCED valuation claim, not a raw fact — kept OUT of the transfer write.

NOT done — attribution: address→entity/label/confidence is not in any UI export; it requires Arkham's
official API (Path B, Invariant #1). `from/toLabel` are too thin (often bare addresses) to synthesize from.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from ..models import Asset, Transaction
from .canonical import canonical_address, to_canonical_ts
from .etherscan_adapter import NATIVE_SYMBOL, ParsedTransaction, ParsedTransfer, _display_or_none

# Account-model (EVM) chains we can faithfully map to `transfer` — mirrors the EVM chains the system
# knows (NATIVE_SYMBOL, incl. bsc). Everything else falls into one of two DISTINCT rejection classes:
#   - UTXO (Bitcoin): a from→to would FABRICATE an input→output edge (Invariant #5) — hard refuse.
#   - other account-model chains (e.g. Tron): account-model but unsupported here (no canonical-address
#     handling / not modelled) — NOT a fabrication risk, just unsupported.
ACCOUNT_MODEL_CHAINS = frozenset(NATIVE_SYMBOL)
UTXO_CHAINS = frozenset({"bitcoin"})  # litecoin/dogecoin/bitcoin-cash would belong here too — none seen yet

# Arkham chain id -> canonical chain name the rest of the system uses (config.CHAINID_TO_NAME). The real
# exports use bsc/tron/base/ethereum (already canonical), but the earlier "no alias map needed" note held
# ONLY for ethereum — normalize here so any Arkham synonym collapses to one canonical chain.
ARKHAM_CHAIN_ALIASES = {
    "ethereum": "ethereum", "eth": "ethereum",
    "bsc": "bsc", "bnb": "bsc", "binance-smart-chain": "bsc", "binance": "bsc",
    "base": "base", "arbitrum": "arbitrum", "optimism": "optimism",
    "polygon": "polygon", "matic": "polygon", "polygon-pos": "polygon",
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "tron": "tron", "trx": "tron",
}


def canonical_chain(raw: str) -> str:
    """Map an Arkham chain id to the canonical chain name (identity for unknown ids, lowercased)."""
    return ARKHAM_CHAIN_ALIASES.get(raw.lower(), raw.lower())


def _canon_or_none(chain: str, addr) -> str | None:
    addr = (addr or "").strip()
    return canonical_address(chain, addr) if addr else None


def _int_or_none(v):
    s = ("" if v is None else str(v)).strip()
    return int(s) if s else None


def _decimals(row: dict, is_native: bool) -> int:
    raw = (row.get("tokenDecimals") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 18 if is_native else 0  # native coins are 18-dec; a token missing decimals -> 0 (surfaced)


def _to_base_units(unit_value: str, decimals: int) -> tuple[str, bool]:
    """display value × 10^decimals → raw base-unit integer TEXT. Decimal (never float). (amount, rounded)."""
    scaled = Decimal(str(unit_value)) * (Decimal(10) ** decimals)
    integral = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
    return str(int(integral)), integral != scaled


def adapt_arkham_transfers(rows: list[dict]) -> tuple[list[ParsedTransaction], dict]:
    """Map Arkham EVM transfer-log rows to canonical bundles.

    Returns ``(bundles, notes)``. Rows are classified by canonical chain into ``rejected_utxo`` (Bitcoin
    — must never become a transfer fact, Invariant #5) vs ``rejected_unsupported`` (account-model chains
    we don't handle, e.g. Tron — not a fabrication risk); the connector treats those two classes
    differently. Other note counters surface the open-decision signals (rounded amounts, the unmapped
    direction `type`, dropped `tokenId`).
    """
    notes = {"rows": 0, "transfers": 0, "skipped": 0, "rounded_amounts": 0, "type_present": 0,
             "tokenid_present": 0, "rejected_utxo": [], "rejected_unsupported": [], "errors": []}
    by_tx: dict[tuple[str, str], ParsedTransaction] = {}  # (chain, tx_hash) — LOG-10
    pos: dict[tuple, int] = {}

    for idx, row in enumerate(rows):
        notes["rows"] += 1
        tx_hash = (row.get("transactionHash") or "").strip()
        raw_chain = (row.get("chain") or "").strip()
        chain = canonical_chain(raw_chain) if raw_chain else ""  # alias-normalize before classifying
        unit = (row.get("unitValue") or "").strip()

        # Classify by chain BEFORE touching addresses/amount (so non-EVM addrs never hit canonical_address).
        if chain and chain not in ACCOUNT_MODEL_CHAINS:
            bucket = "rejected_utxo" if chain in UTXO_CHAINS else "rejected_unsupported"
            notes[bucket].append({"chain": chain, "tx": tx_hash})
            continue
        if not tx_hash or not chain or unit == "":
            notes["skipped"] += 1  # can't form a transfer fact without tx/chain/amount
            continue

        if (row.get("type") or "").strip():
            notes["type_present"] += 1     # (b) `type` is direction (inflow/outflow), not the enum
        if (row.get("tokenId") or "").strip():
            notes["tokenid_present"] += 1   # (e) coin slug, dropped from the raw transfer

        # Per-row parse: a malformed amount/address/block is recorded (with its row index) and surfaced
        # by the connector as a clean error — never a raw traceback, never silently dropped.
        try:
            token = (row.get("tokenAddress") or "").strip()
            is_native = not token
            transfer_type = "native" if is_native else "erc20"
            decimals = _decimals(row, is_native)
            amount, rounded = _to_base_units(unit, decimals)
            block_height = _int_or_none(row.get("blockNumber"))
            from_addr = _canon_or_none(chain, row.get("fromAddress"))
            to_addr = _canon_or_none(chain, row.get("toAddress"))
            asset = Asset(chain=chain, contract_address=(None if is_native else canonical_address(chain, token)),
                          symbol=(row.get("tokenSymbol") or "").strip()
                          or (NATIVE_SYMBOL.get(chain.lower(), "ETH") if is_native else None),
                          decimals=decimals)
        except (InvalidOperation, ValueError) as exc:
            notes["errors"].append({"row": idx, "tx": tx_hash, "reason": str(exc)})
            continue

        if rounded:
            notes["rounded_amounts"] += 1
        # LOG-10: key on (chain, tx_hash), NOT the hash alone — an EVM tx hash replayed across two chains
        # in one multichain export must NOT collapse into a single transaction on the first-seen chain
        # (which loses the second chain's tx and makes transfer.chain != transaction_.chain).
        tx_key = (chain, tx_hash)
        if tx_key not in by_tx:
            by_tx[tx_key] = ParsedTransaction(transaction=Transaction(
                chain=chain, tx_hash=tx_hash, block_height=block_height,
                block_ts=to_canonical_ts(row.get("blockTimestamp")),  # LOG-05: one canonical format
                fee=None, status=None, confirmations=None, finality_status="provisional"))

        key = (chain, tx_hash, transfer_type)
        position = pos.get(key, 0)
        pos[key] = position + 1
        by_tx[tx_key].transfers.append(ParsedTransfer(
            chain=chain, from_address=from_addr, to_address=to_addr,
            asset=asset, amount=amount, transfer_type=transfer_type, position=position,
            from_address_display=_display_or_none(row.get("fromAddress")),  # COR-02: keep EIP-55 checksum
            to_address_display=_display_or_none(row.get("toAddress"))))
        notes["transfers"] += 1

    return list(by_tx.values()), notes
