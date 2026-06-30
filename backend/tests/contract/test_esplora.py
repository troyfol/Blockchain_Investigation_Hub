"""Contract tests for the Esplora adapter (phase_03 step 4). Offline, from cassettes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.normalization.esplora_adapter import adapt_address_txs, balance_from_stats

CASSETTES = Path(__file__).resolve().parent.parent / "cassettes" / "esplora"
CHAIN, TIP, THR = "bitcoin", 800010, 6
G = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
pytestmark = pytest.mark.contract


def _txs():
    return json.loads((CASSETTES / "address_txs.json").read_text())


def test_adapt_btc_tx_node_inputs_outputs():
    txs = _txs()
    p = adapt_address_txs(txs, chain=CHAIN, tip_height=TIP, threshold=THR)[0]
    assert p.transaction.tx_hash == txs[0]["txid"]
    assert p.transaction.finality_status == "final"  # 800010 - 800000 + 1 = 11 >= 6
    assert p.transaction.confirmations == 11
    assert p.transaction.fee == "1000" and p.transaction.status == "confirmed"

    assert len(p.inputs) == 2 and len(p.outputs) == 2
    i0 = p.inputs[0]
    assert i0.address == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"  # base58 untouched
    assert i0.amount == "150000" and i0.input_index == 0
    assert i0.prev_txid == txs[0]["vin"][0]["txid"] and i0.prev_vout == 0

    o0, o1 = p.outputs
    assert o0.address == G and o0.amount == "120000" and o0.output_index == 0  # bech32 lowercased
    assert o1.address == "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy" and o1.amount == "79000"


def test_balance_from_chain_stats():
    stats = json.loads((CASSETTES / "address_stats.json").read_text())
    assert balance_from_stats(stats) == 120000  # funded 120000 - spent 0


def test_coinbase_and_non_standard_output():
    txs = [{
        "txid": "d" * 64, "fee": 0,
        "status": {"confirmed": True, "block_height": 799000, "block_time": 1690000000},
        "vin": [{"is_coinbase": True, "sequence": 4294967295}],
        "vout": [{"scriptpubkey": "6a...", "scriptpubkey_type": "op_return", "value": 0}],
    }]
    p = adapt_address_txs(txs, chain=CHAIN, tip_height=TIP, threshold=THR)[0]
    assert p.inputs[0].address is None and p.inputs[0].amount == "0"  # coinbase: minted, no prevout
    assert p.outputs[0].address is None  # OP_RETURN / non-standard -> NULL address


def test_mempool_tx_is_provisional():
    txs = [{"txid": "e" * 64, "fee": 300, "status": {"confirmed": False}, "vin": [], "vout": []}]
    p = adapt_address_txs(txs, chain=CHAIN, tip_height=TIP, threshold=THR)[0]
    assert p.transaction.finality_status == "provisional"
    assert p.transaction.confirmations == 0 and p.transaction.block_height is None
    assert p.transaction.status == "mempool"
