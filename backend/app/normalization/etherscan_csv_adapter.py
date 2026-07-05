"""Etherscan UI "Download CSV Export" (normal transactions) -> canonical EVM transfer mapping (P22/FN-25).

Investigators routinely export an address's transaction history from Etherscan's UI ("Download CSV
Export") when they have no API key or the free API is rate-limited / doesn't cover a chain. This pure
adapter (no HTTP/DB) maps the **normal-transactions** export onto the canonical EVM `transfer` fact path,
reusing `etherscan_adapter`'s `ParsedTransaction`/`ParsedTransfer` shape — the SAME shape the Etherscan
**API** connector produces — so the same DB writer resolves addresses/assets to ids AND the same on-chain
movement pulled from the API vs this CSV dedups to one row (content+`occurrence`, Invariant #7). Structured
manual import of data the human legitimately exported from the tool's own UI — Invariant #1 (never scraped).

Scope + resolved decisions (`TODO: confirm` where the public export can't settle it):

(chain) **The CSV carries NO chain column** — the export is explorer-scoped (etherscan.io = ethereum,
    bscscan.com = bsc, ...). The chain is therefore a PARAMETER the investigator states; it defaults to
    `ethereum` and MUST be an EVM/account-model chain (the connector refuses a UTXO chain up front — an
    Etherscan export is inherently account-model; Invariant #5, no synthesized input->output edge).

(header) **Era-robust column resolution.** Etherscan's header drifted across eras — the first column is
    `Txhash` (classic) or `Transaction Hash` (current); `DateTime` vs `DateTime (UTC)`; a trailing
    `Method` column was added later. Columns are resolved by a NORMALIZED alias (lowercased, non-alnum
    stripped) so either era ingests. A file missing the essential columns (hash/from/to) is refused with a
    clean error (it isn't an Etherscan normal-tx export) rather than silently importing nothing.

(a) **`Value_IN(ETH)` / `Value_OUT(ETH)` are DISPLAY-decimal ETH**, address-relative (IN = received by the
    exported address, OUT = sent). The movement's native amount is the non-zero side (a self-send shows the
    same value in both -> ONE transfer, never summed). `Transfer.amount` is raw base units, so
    `amount = round(Decimal(value) x 10^18)` — Decimal, never float; native ETH is 18-decimal, so decimals
    is known (unlike the ERC-20 token export, which omits `tokenDecimal` — see NOT-done). A value with MORE
    precision than 18 dp is flagged `rounded`; a NON-zero value shown with FEWER fractional digits than 18
    is flagged `truncation_risk` (FN-24/P19). **Honesty caveat:** Etherscan appears to render the value at
    ~15 significant figures, so a genuinely long value CAN be display-rounded — the flag catches that; but a
    round value (`0.5`) is exact-yet-short, so the flag conservatively OVER-reports it. It is only a surfaced
    caveat COUNTER — it never alters `amount` and never blocks dedup, so an exact value still reconciles
    against the API; the authoritative low-order wei come from an idempotent chain re-fetch. *TODO: confirm
    whether Etherscan ever lossy-truncates the Value column (vs always exact).*

(b) **Direction/parties are explicit** (`From`/`To`), so IN/OUT is used ONLY to pick the amount, never to
    synthesize a party. A zero-value row (both sides 0 — a contract call/method with no ETH moved) yields a
    `transaction_` row with NO transfer (never fabricate a movement).

(c) **Finality:** the export has no confirmations column -> every tx is `provisional` (honest — not
    confirmed), upgraded to `final` by a later idempotent chain re-fetch (Invariant #6). Never frozen final.

(d) **Failure:** a reverted tx moved no value -> `transaction_` (status='failed') with NO transfer.
    `ErrCode` (non-empty ⇒ failed) is the authoritative signal; a `Status` of `Error(...)`/`Fail`/`1` is
    also treated as failed. *TODO: confirm the numeric Status polarity across eras (some use 0=ok/1=error);
    ErrCode is relied on as primary so the polarity guess is never the sole failure signal.*

(e) **`position`:** the normal-tx export is one row per tx -> a single native move at `position=0` (mirrors
    `adapt_txlist`). Cross-source dedup is by content+`occurrence` (`reconcile.assign_occurrences`), NOT
    position, so the SAME movement pulled from the Etherscan **API** and this CSV dedups to one row
    (Invariant #7) — while a truncated CSV amount is a distinct content key, kept side-by-side (Invariant #4).

(f) **dropped columns** (no canonical slot; we do NOT extend the schema for import-only metadata):
    `CurrentValue @ $.../Eth` (price-of-day-of-export, varies per file), `TxnFee(USD)`,
    `Historical $Price/Eth`, `Method`, `ContractAddress` (populated only for contract-CREATION rows, where
    `To` is empty and value 0 -> no transfer anyway). `TxnFee(ETH)` IS mapped to `Transaction.fee`
    (base-unit wei) when present.

NOT done (honest gaps, documented follow-ups): the **ERC-20 token** export and the **internal-tx** export.
The token export omits `tokenDecimal`, so its display `TokenValue` can't be converted to base units from the
CSV alone (needs a decimals lookup) — deferred rather than guessed. `Historical $Price/Eth` (a per-unit USD
price) could drive a second sourced `valuation` like FN-18/Arkham — deferred (not in P22 acceptance).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from ..models import Transaction
from .canonical import to_canonical_ts
from .etherscan_adapter import (
    ParsedTransaction, ParsedTransfer, _canon_or_none, _display_or_none, native_asset,
)

# logical name -> accepted NORMALIZED headers (lowercased, non-alphanumeric stripped). Era-robust:
# Txhash/Transaction Hash -> {txhash, transactionhash}; Value_IN(ETH) -> valueineth; etc.
_ALIASES = {
    "txhash": ("txhash", "transactionhash"),
    "blockno": ("blockno", "blocknumber"),
    "timestamp": ("unixtimestamp",),
    "datetime": ("datetime", "datetimeutc"),
    "from": ("from",),
    "to": ("to",),
    "value_in": ("valueineth", "valuein"),
    "value_out": ("valueouteth", "valueout"),
    "txnfee_eth": ("txnfeeeth",),
    "status": ("status",),
    "errcode": ("errcode",),
}
_REQUIRED = ("txhash", "from", "to")


def _norm(header: str) -> str:
    """Normalize a header for era-robust matching: lowercase, drop every non-alphanumeric char."""
    return "".join(ch for ch in header.lower() if ch.isalnum())


def _resolve_columns(fieldnames) -> dict:
    """Map each logical name -> the ACTUAL header string present (first alias match). Absent = not in map."""
    present = {_norm(h): h for h in (fieldnames or []) if h}
    out = {}
    for logical, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in present:
                out[logical] = present[alias]
                break
    return out


def _is_error_status(status: str) -> bool:
    """A `Status` cell that signals failure. ErrCode is the primary signal (see decision (d)); this only
    catches a textual `Error(...)`/`Fail...` or a bare `1`. Empty / `0` / `success` -> not an error."""
    s = status.strip().lower()
    return ("error" in s) or ("fail" in s) or (s == "1")


def _to_base_units(display: str, decimals: int) -> tuple[str, bool, bool]:
    """display value x 10^decimals -> raw base-unit integer TEXT (Decimal, never float).

    Mirrors `arkham_adapter`'s FN-24/P19 helper. Returns ``(amount, rounded, truncation_risk)`` — ``rounded``
    when the display carried MORE precision than ``decimals`` (product non-integral, rounded half-even);
    ``truncation_risk`` when a NON-zero value carried FEWER fractional digits than ``decimals`` (low-order
    units display-rounded, not source-verified). Mutually exclusive (over- vs under-precision).
    """
    value = Decimal(str(display))
    scaled = value * (Decimal(10) ** decimals)
    integral = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
    exp = value.as_tuple().exponent  # int for a finite Decimal; a symbol for NaN/Inf (caught by caller)
    frac_digits = -exp if isinstance(exp, int) and exp < 0 else 0
    truncation_risk = frac_digits < decimals and int(integral) != 0
    return str(int(integral)), integral != scaled, truncation_risk


def adapt_etherscan_csv(rows: list[dict], *, chain: str = "ethereum",
                        fieldnames=None) -> tuple[list[ParsedTransaction], dict]:
    """Map Etherscan normal-tx CSV rows to canonical native-transfer bundles.

    Returns ``(bundles, notes)``. ``chain`` is stated by the caller (the CSV has no chain column) and is
    assumed EVM (the connector rejects a non-EVM chain up front). ``fieldnames`` is the export's header (for
    era-robust column resolution); it defaults to the first row's keys. A per-row malformed value/address is
    recorded in ``notes["errors"]`` (with its row index) and surfaced by the connector as a clean error —
    never a raw traceback. A missing REQUIRED column short-circuits with a header error (row -1).
    """
    notes = {"rows": 0, "transfers": 0, "skipped": 0, "failed": 0, "rounded_amounts": 0,
             "truncation_risk": 0, "errors": []}
    cols = _resolve_columns(fieldnames if fieldnames is not None
                            else (list(rows[0].keys()) if rows else []))
    missing = [k for k in _REQUIRED if k not in cols]
    if missing:
        notes["errors"].append({"row": -1, "tx": "", "reason":
                                f"missing required column(s) {missing} — not an Etherscan normal-tx CSV export"})
        return [], notes

    def cell(row, logical):
        col = cols.get(logical)
        return (row.get(col) or "").strip() if col else ""

    by_tx: dict[tuple[str, str], ParsedTransaction] = {}  # (chain, tx_hash)
    for idx, row in enumerate(rows):
        notes["rows"] += 1
        tx_hash = cell(row, "txhash")
        if not tx_hash:
            notes["skipped"] += 1  # a blank/summary line — no tx to form
            continue
        failed = bool(cell(row, "errcode")) or _is_error_status(cell(row, "status"))

        # Per-row parse: a malformed amount/address/block is recorded (with its row index) and surfaced by
        # the connector as a clean error — never a raw traceback, never silently dropped.
        try:
            block_raw = cell(row, "blockno")
            block_height = int(block_raw) if block_raw else None
            block_ts = to_canonical_ts(cell(row, "timestamp") or cell(row, "datetime"))
            fee_raw = cell(row, "txnfee_eth")
            fee = _to_base_units(fee_raw, 18)[0] if fee_raw and Decimal(fee_raw) != 0 else None
            from_addr = _canon_or_none(chain, cell(row, "from"))
            to_addr = _canon_or_none(chain, cell(row, "to"))
            # amount = the non-zero side (OUT sent / IN received); a self-send shows the same in both.
            v_out, v_in = cell(row, "value_out"), cell(row, "value_in")
            disp = v_out if (v_out and Decimal(v_out) != 0) else v_in
            has_value = bool(disp) and Decimal(disp) != 0
            amount = rounded = truncated = None
            if has_value:
                amount, rounded, truncated = _to_base_units(disp, 18)
        except (InvalidOperation, ValueError) as exc:
            notes["errors"].append({"row": idx, "tx": tx_hash, "reason": str(exc)})
            continue

        tx_key = (chain, tx_hash)
        if tx_key not in by_tx:
            by_tx[tx_key] = ParsedTransaction(transaction=Transaction(
                chain=chain, tx_hash=tx_hash, block_height=block_height, block_ts=block_ts,
                fee=fee, status=("failed" if failed else "success"),
                confirmations=None, finality_status="provisional"))

        # A reverted tx (or a zero-value contract call) moved no native value -> tx row, NO transfer.
        if failed:
            notes["failed"] += 1
            continue
        if not has_value:
            notes["skipped"] += 1
            continue
        if rounded:
            notes["rounded_amounts"] += 1
        if truncated:
            notes["truncation_risk"] += 1  # FN-24: display carried fewer digits than 18 — low-order-lossy
        by_tx[tx_key].transfers.append(ParsedTransfer(
            chain=chain, from_address=from_addr, to_address=to_addr, asset=native_asset(chain),
            amount=amount, transfer_type="native", position=0,
            from_address_display=_display_or_none(cell(row, "from")),  # COR-02: keep EIP-55 checksum
            to_address_display=_display_or_none(cell(row, "to"))))
        notes["transfers"] += 1

    return list(by_tx.values()), notes
