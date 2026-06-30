"""Unit tests for address canonicalization (docs/algorithms.md §1)."""

from __future__ import annotations

import pytest

from backend.app.normalization.canonical import canonical_address, canonicalize, is_btc, is_evm

# Valid EIP-55 checksummed examples.
CHECKSUM = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
LOWER = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
UPPER = "0x5AAEB6053F3E94C9B9A09F33669435E7EF1BEAED"


def test_evm_canonical_is_lowercase():
    assert canonical_address("ethereum", CHECKSUM) == LOWER
    assert canonical_address("ethereum", UPPER) == LOWER


def test_evm_checksum_and_lowercase_map_equal():
    assert canonical_address("ethereum", CHECKSUM) == canonical_address("ethereum", LOWER)


def test_evm_applies_to_all_account_chains():
    # ~50 EVM chains via one key — anything not bitcoin is treated as account/EVM in v1.
    for chain in ("ethereum", "arbitrum", "base", "polygon", "optimism"):
        assert is_evm(chain)
        assert canonical_address(chain, CHECKSUM) == LOWER


def test_evm_rejects_malformed():
    with pytest.raises(ValueError):
        canonical_address("ethereum", "0x123")  # too short
    with pytest.raises(ValueError):
        canonical_address("ethereum", "not-an-address")
    with pytest.raises(ValueError):
        canonical_address("ethereum", "0x" + "z" * 40)  # non-hex


def test_btc_bech32_is_lowercased():
    bech = "BC1QAR0SRRR7XFKVY5L643LYDNW9RE59GTZZWF5MDQ"
    assert is_btc("bitcoin")
    assert canonical_address("bitcoin", bech) == bech.lower()


def test_btc_base58_is_untouched():
    # base58 is case-sensitive — must not be altered.
    base58 = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    assert canonical_address("bitcoin", base58) == base58


def test_btc_all_segwit_hrps_lowercased():
    # bech32 is lowercase-canonical for mainnet/testnet/regtest alike.
    for addr in ["BC1QFOO", "TB1QFOO", "BCRT1QFOO"]:
        assert canonical_address("bitcoin", addr) == addr.lower()


def test_btc_rejects_empty_or_whitespace():
    for bad in ["", "   ", " bc1qx", "bc1qx "]:
        with pytest.raises(ValueError):
            canonical_address("bitcoin", bad)


def test_canonicalize_returns_canonical_and_display():
    canonical, display = canonicalize("ethereum", CHECKSUM)
    assert canonical == LOWER
    assert display == CHECKSUM  # original source form preserved


def test_canonical_idempotence():
    for chain, addr in [("ethereum", CHECKSUM), ("bitcoin", "BC1QXYZ" + "0" * 30),
                        ("bitcoin", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")]:
        once = canonical_address(chain, addr)
        assert canonical_address(chain, once) == once
