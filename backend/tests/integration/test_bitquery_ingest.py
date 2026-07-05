"""FN-06 (P18, Track E): Bitquery end-to-end ingest through the canonical path, WITHOUT a live token — the
GraphQL call is monkeypatched to a synthetic response (no network, no fabricated cassette on disk). Proves
acceptance #1 (canonical EVM rows + provenance) and #3 (idempotent occurrence dedup) and #2 (EVM-only, no
UTXO edges). The live wire shapes remain UNVERIFIED until the key-gated RUN_LIVE drift test runs.
"""

from __future__ import annotations

from backend.app.connectors.bitquery import BitqueryConnector
from backend.tests.integration._helpers import new_case

TX = "0x" + "ab" * 32
SENDER = "0x52908400098527886E0F7030069857D2E4169EE7"
RECEIVER = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _payload():
    def row(amount, symbol, contract, decimals):
        return {"Transaction": {"Hash": TX},
                "Transfer": {"Sender": SENDER, "Receiver": RECEIVER, "Amount": amount,
                             "Currency": {"Symbol": symbol, "SmartContract": contract, "Decimals": decimals}},
                "Block": {"Number": 21000000, "Time": "2026-01-01T00:00:00Z"}}
    return {"data": {"EVM": {"Transfers": [row("1.5", "ETH", "", 18), row("100", "USDC", USDC, 6)]}}}


def test_ingest_writes_canonical_evm_rows_with_provenance_and_is_idempotent(tmp_path, monkeypatch):
    conn, db = new_case(tmp_path, title="Bitquery ingest")
    c = BitqueryConnector(token="tok")                       # a non-empty token passes the key guard
    monkeypatch.setattr(c, "_graphql", lambda query, variables: _payload())   # no network
    try:
        c.get_transactions(conn, "ethereum", SENDER)

        # canonical EVM facts written
        assert conn.execute("SELECT COUNT(*) FROM transaction_ WHERE chain='ethereum'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 2
        # EVM-only: no UTXO edges fabricated (Invariant #5)
        assert conn.execute("SELECT COUNT(*) FROM tx_output").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tx_input").fetchone()[0] == 0
        # provisional (no confirmations from Bitquery, Invariant #6)
        assert conn.execute(
            "SELECT finality_status FROM transaction_ WHERE tx_hash=?", (TX,)).fetchone()[0] == "provisional"
        # provenance: a bitquery source_query, and every transfer references it (Invariant #3)
        assert conn.execute("SELECT COUNT(*) FROM source_query WHERE connector='bitquery'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transfer WHERE source_query_id IS NULL").fetchone()[0] == 0

        # idempotent re-ingest of the same data: a new source_query row, but NO duplicate facts (Invariant #7)
        c.get_transactions(conn, "ethereum", SENDER)
        assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 1
    finally:
        c.close()
        conn.close()
