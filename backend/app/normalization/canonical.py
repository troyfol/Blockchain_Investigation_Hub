"""Address canonicalization (Phase 1, docs/algorithms.md §1).

One canonical string per real address so ``(chain, address)`` is a true unique key (Invariant #8).

- **EVM:** canonical = lowercase ``0x`` + 40 hex. Source checksummed form kept as `address_display`.
- **Bitcoin:** keep as the source presents it, but lowercase where the *encoding* defines case:
  bech32/bech32m (`bc1...`) are lowercase-canonical; base58 (`1...`/`3...`) is case-sensitive — untouched.
  Distinct encodings of the same key are distinct addresses (unifying them is a heuristic *claim*).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Accept either prefix case (0x / 0X); the canonical form always lowercases everything.
EVM_ADDRESS_RE = re.compile(r"0[xX][0-9a-fA-F]{40}")

_EPOCH_RE = re.compile(r"\d+(\.\d+)?")


def to_canonical_ts(raw) -> str | None:
    """Normalize a source timestamp to the canonical ``YYYY-MM-DDTHH:MM:SSZ`` (schema.md §2 — "one format
    everywhere"). Accepts an ISO-8601 string (``Z`` or an explicit offset; naive is assumed UTC) or a unix
    epoch (int / all-digit string). Returns ``None`` for empty/unparseable input — an honest gap the
    valuation pass skips (LOG-05), never a raw non-canonical string leaking downstream to abort a pass."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _EPOCH_RE.fullmatch(s):
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# v1 scope is EVM (account) + Bitcoin (UTXO). Treat bitcoin as UTXO; everything else as EVM.
BTC_CHAINS = {"bitcoin", "btc"}

# bech32/bech32m human-readable prefixes (mainnet/testnet/regtest). bech32 is case-defined:
# the canonical form is lowercase for ANY of these, so we lowercase when the (lowercased)
# address starts with a known segwit HRP. base58 (1.../3...) stays case-sensitive (untouched).
BECH32_HRPS = ("bc1", "tb1", "bcrt1")


def is_btc(chain: str) -> bool:
    return chain.lower() in BTC_CHAINS


def is_evm(chain: str) -> bool:
    return not is_btc(chain)


def canonical_address(chain: str, address: str) -> str:
    """Return the canonical form of ``address`` on ``chain``. Raises on malformed input.

    Full bech32/base58 checksum validation is the connector's job (we ingest addresses a tool
    legitimately surfaced); here we only reject obviously-empty input and canonicalize case.
    """
    if is_btc(chain):
        if not address or not address.strip() or address != address.strip():
            raise ValueError(f"malformed Bitcoin address on chain {chain!r}: {address!r}")
        low = address.lower()
        return low if low.startswith(BECH32_HRPS) else address
    # EVM (account model)
    if not EVM_ADDRESS_RE.fullmatch(address):
        raise ValueError(f"malformed EVM address on chain {chain!r}: {address!r}")
    return address.lower()


def canonicalize(chain: str, address: str) -> tuple[str, str]:
    """Return ``(canonical, display)`` — display is the original source form."""
    return canonical_address(chain, address), address
