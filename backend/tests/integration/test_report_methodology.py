"""FN-08 (P13, Track D — court-ready reporting): the report's **Methodology** section.

A single, self-contained section that states HOW to read the report: the Bitcoin input/output tracing
convention + its legal basis (Clayton's Case / *D'Aloia*), the value-at-time valuation method, the
per-chain finality thresholds **actually applied in this case** (read live from ``config.py``, never a
hardcoded literal), the side-by-side/never-averaged claim policy, the scope bounds, and the honest
limits (local-clock timestamps, no third-party notarization).
"""

from __future__ import annotations

import pytest

from backend.app.config import get_settings
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.reporting import generate_report
from backend.tests.integration._helpers import new_case


@pytest.fixture
def btc_case(tmp_path):
    """A minimal case carrying one Bitcoin transaction — so ``bitcoin`` is a chain whose finality
    threshold the Methodology section must state, straight from the live app config."""
    conn, db = new_case(tmp_path, title="Methodology Case")
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display="A"), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="0" * 64, block_height=799999, block_ts="2026-01-01T00:00:00Z",
            confirmations=20, finality_status="final"), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx, address_id=a, amount="100",
                                          output_index=0), sqid)

    write_with_provenance(conn, sq, write)
    yield conn, db, tmp_path
    conn.close()


def test_methodology_states_fifo_and_thresholds(btc_case):
    """The named acceptance test: the report has a Methodology section covering the tracing convention +
    legal basis, value-at-time, the real finality thresholds, side-by-side claims, scope, and limits."""
    conn, db, tmp_path = btc_case
    result = generate_report(conn, case_dir=tmp_path, title="Methodology Case — Report",
                             render_pdf=False)
    page = result["html_path"].read_text(encoding="utf-8")

    # The section itself.
    assert "<h2>Methodology</h2>" in page

    # 1. FIFO tracing convention + its legal basis (Clayton's Case / D'Aloia). The apostrophes render as
    #    typographic entities, so anchor on the unambiguous names + D'Aloia's neutral citation.
    assert "FIFO" in page
    assert "convention" in page
    assert "Clayton" in page and "Aloia" in page and "EWHC 1723" in page

    # 2. Value-at-time valuation method.
    assert "value movement is priced" in page

    # 3. Per-chain finality thresholds ACTUALLY USED — the case's real config value, not a literal.
    #    (Expected computed from the SAME source the report reads, so this stays true under any override.)
    n = get_settings().finality_threshold("bitcoin")
    assert "<td>bitcoin</td>" in page
    assert f"{n}+ confirmations" in page

    # 4. Claims kept side-by-side, never averaged.
    assert "side-by-side" in page and "averaged" in page

    # 5. Scope bounds are pointed at.
    assert "applied bounds" in page

    # 6. Honest limits: local-clock timestamps, no third-party notarization.
    assert "local" in page and "notariz" in page


def test_methodology_thresholds_are_live_config_not_hardcoded(btc_case, monkeypatch):
    """Proves the thresholds are read from the live config: an override flows verbatim into the report,
    and the default value does NOT appear (so the number is not a hardcoded literal)."""
    conn, db, tmp_path = btc_case
    monkeypatch.setenv("BIH_FINALITY_THRESHOLDS", '{"bitcoin": 99}')
    get_settings.cache_clear()
    try:
        result = generate_report(conn, case_dir=tmp_path, title="Override — Report", render_pdf=False)
        page = result["html_path"].read_text(encoding="utf-8")
        assert "99+ confirmations" in page          # the override, read live from config
        assert "6+ confirmations" not in page        # not the settled-default literal
    finally:
        monkeypatch.delenv("BIH_FINALITY_THRESHOLDS", raising=False)
        get_settings.cache_clear()


def test_methodology_render_is_deterministic(btc_case):
    """Court-ready determinism: two renders of the same case produce byte-identical HTML (fixed
    ``generated_at``), so the immutable ``content_hash`` is stable."""
    conn, db, tmp_path = btc_case
    a = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False)
    b = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False)
    assert a["html_path"].read_text(encoding="utf-8") == b["html_path"].read_text(encoding="utf-8")
    assert a["content_hash"] == b["content_hash"]
