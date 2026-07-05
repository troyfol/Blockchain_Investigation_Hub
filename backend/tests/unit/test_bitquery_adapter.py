"""FN-06 (P18, Track E): Bitquery V2 EVM GraphQL -> canonical mapping — pure (no HTTP, no DB), synthetic
input. Locks the mapper's behavior on the ASSUMED V2 shape.

NOTE (gated): the live wire shapes (`TRANSFERS_QUERY`, field paths, network slugs) stay `TODO: confirm` /
UNVERIFIED until the user's `bitquery_token` runs the key-gated `RUN_LIVE` drift test
(`tests/contract/test_live_drift.py`) — no fabricated cassette (build directive). This synthetic test proves
the mapping is CORRECT for the assumed shape; RUN_LIVE proves the shape is what Bitquery actually returns.
The connector + adapter were already built (with the hedge) before this phase — this test confirms them.
"""

from __future__ import annotations

from backend.app.normalization.bitquery_adapter import adapt_transfers, network_for
from backend.app.normalization.canonical import canonical_address


def _row(sender, receiver, amount, symbol, contract, decimals, height, ts, tx_hash):
    return {
        "Transaction": {"Hash": tx_hash},
        "Transfer": {"Sender": sender, "Receiver": receiver, "Amount": amount,
                     "Currency": {"Symbol": symbol, "SmartContract": contract, "Decimals": decimals}},
        "Block": {"Number": height, "Time": ts},
    }


def test_maps_graphql_to_canonical_transfer():
    tx = "0x" + "ab" * 32
    sender = "0x52908400098527886E0F7030069857D2E4169EE7"   # EIP-55 checksum test vectors (valid)
    receiver = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"      # lowercase (checksum-agnostic)
    payload = {"data": {"EVM": {"Transfers": [
        _row(sender, receiver, "1.5", "ETH", "", 18, 21000000, "2026-01-01T00:00:00Z", tx),       # native
        _row(sender, receiver, "100", "USDC", usdc, 6, 21000000, "2026-01-01T00:00:00Z", tx),      # erc20
    ]}}}

    bundles, notes = adapt_transfers(payload, chain="ethereum")

    assert len(bundles) == 1                                  # both transfers roll under ONE transaction
    b = bundles[0]
    assert b.transaction.chain == "ethereum" and b.transaction.tx_hash == tx
    assert b.transaction.block_height == 21000000
    # Bitquery returns no confirmations -> provisional, never a guessed final (Invariant #6)
    assert b.transaction.finality_status == "provisional" and b.transaction.confirmations is None
    assert notes["transfers"] == 2 and len(b.transfers) == 2

    native = next(t for t in b.transfers if t.transfer_type == "native")
    erc20 = next(t for t in b.transfers if t.transfer_type == "erc20")

    # canonical addresses on the fact; the source EIP-55 checksum preserved as display (COR-02, Invariant #8)
    assert native.from_address == canonical_address("ethereum", sender)
    assert native.to_address == canonical_address("ethereum", receiver)
    assert native.from_address_display == sender and native.to_address_display == receiver
    # display -> base-unit integer amounts (Decimal, half-even)
    assert native.amount == "1500000000000000000"            # 1.5 ETH @ 18
    assert erc20.amount == "100000000"                       # 100 USDC @ 6
    # native carries no contract; erc20 carries the canonical contract + its decimals
    assert native.asset.contract_address is None and native.asset.symbol == "ETH"
    assert erc20.asset.contract_address == canonical_address("ethereum", usdc)
    assert erc20.asset.decimals == 6
    # EVM-only: the bundle exposes transfers, never UTXO edges (Invariant #5)
    assert all(t.transfer_type in ("native", "internal", "erc20") for t in b.transfers)


def test_native_sentinel_zero_address_is_native():
    """A zero-address / missing SmartContract is the native asset, not a fabricated ERC-20."""
    tx = "0x" + "cd" * 32
    a = "0x52908400098527886E0F7030069857D2E4169EE7"
    zero = "0x0000000000000000000000000000000000000000"
    payload = {"data": {"EVM": {"Transfers": [
        _row(a, a, "2", "ETH", zero, 18, 5, "2026-01-01T00:00:00Z", tx)]}}}
    b = adapt_transfers(payload, chain="ethereum")[0][0]
    assert b.transfers[0].transfer_type == "native"
    assert b.transfers[0].asset.contract_address is None


def test_bitcoin_has_no_evm_network_slug():
    """The mapper is EVM-only — a non-EVM chain has no network slug (the connector refuses it)."""
    assert network_for("bitcoin") is None
    assert network_for("ethereum") == "eth"
