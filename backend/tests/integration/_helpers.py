"""Shared test helpers (not collected as tests — no test_ prefix)."""

from __future__ import annotations

from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.models import (
    Address,
    Asset,
    EntityMembership,
    SourceQuery,
    Transaction,
    Transfer,
    TxInput,
    TxOutput,
)
from backend.app.provenance.atomic import write_with_provenance


def new_case(tmp_path, title="Case"):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title=title)
    return conn, db


def seed_btc_custom(conn, *, txid, input_addrs, output_amounts, final=True, in_amount=100_000):
    """Seed a Bitcoin tx with explicit input addresses + output amounts (sats). Returns tx_id."""
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        conf, fin = (11, "final") if final else (1, "provisional")
        tx_id = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash=txid, block_height=800000, block_ts="2026-01-01T00:00:00Z",
            fee="1000", confirmations=conf, finality_status=fin), sqid)
        for idx, addr in enumerate(input_addrs):
            aid = repo.upsert_address(c, Address(chain="bitcoin", address_display=addr), sqid) if addr else None
            repo.upsert_tx_input(c, TxInput(transaction_id=tx_id, address_id=aid,
                                            amount=str(in_amount), input_index=idx), sqid)
        for idx, amt in enumerate(output_amounts):
            oid = repo.upsert_address(c, Address(chain="bitcoin", address_display=f"out_{txid[:8]}_{idx}"), sqid)
            repo.upsert_tx_output(c, TxOutput(transaction_id=tx_id, address_id=oid,
                                              amount=str(amt), output_index=idx), sqid)
        return tx_id

    _, tx_id = write_with_provenance(conn, sq, write)
    return tx_id


def seed_evm_address(conn, address_display, *, chain="ethereum", counterparty=None):
    """Put an EVM address into the case as a real on-chain participant (a native transfer FROM it) so
    intel can screen it AND it renders as a graph node. The repo canonicalizes on ingest (Inv #8):
    ``address.address`` is stored LOWERCASE while ``address_display`` keeps the source (possibly
    checksummed) form — exactly the case the case-insensitive match must handle. Returns the address_id."""
    counterparty = counterparty or ("0x" + "cc" * 20)
    tx_hash = "0x" + (address_display.lower().replace("0x", "") + "0" * 64)[:64]
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": address_display, "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    out = []

    def write(c, sqid):
        asset_id = repo.upsert_asset(c, Asset(chain=chain, symbol="ETH", decimals=18), sqid)
        aid = repo.upsert_address(c, Address(chain=chain, address_display=address_display), sqid)
        cid = repo.upsert_address(c, Address(chain=chain, address_display=counterparty), sqid)
        tx_id = repo.upsert_transaction(c, Transaction(
            chain=chain, tx_hash=tx_hash, block_height=900, block_ts="2026-01-01T00:00:00Z",
            fee="210000000000000", status="1", confirmations=100, finality_status="final"), sqid)
        repo.upsert_transfer(c, Transfer(
            transaction_id=tx_id, chain=chain, from_address_id=aid, to_address_id=cid,
            asset_id=asset_id, amount="1000000000000000000", transfer_type="native", position=0), sqid)
        out.append(aid)

    write_with_provenance(conn, sq, write)
    return out[0]


def make_membership(conn, *, entity_id, address_id, source, method, connector="arkham-import"):
    """Create a sourced (non-investigator) entity_membership with provenance. Returns membership id."""
    sq = SourceQuery(connector=connector, capability="get_attributions", endpoint="import",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    out = []

    def write(c, sqid):
        out.append(repo.insert_entity_membership(c, EntityMembership(
            entity_id=entity_id, address_id=address_id, source=source, method=method), sqid))

    write_with_provenance(conn, sq, write)
    return out[0]
