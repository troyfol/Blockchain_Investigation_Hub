"""Batch 1 (SEC-01 / SEC-11 / SEC-12): the court-facing report must not let an attacker-controlled
on-chain token symbol / attribution name / investigator label break out of the inline ``<script>``
(or, latently, the ``<style>``) that carries the Cytoscape payload.

``reporting.render_html`` embeds ``var ELEMENTS = {json.dumps(elements)}`` inside an inline ``<script>``.
``json.dumps`` does NOT escape ``<`` / ``>`` / ``/``, so a label containing ``</script>...`` closes the
script element per the HTML script-data-end rule and the trailing markup is parsed as HTML -- stored
code-execution into the flagship evidentiary artifact when the headless engine renders the PDF.

These tests seed the payload through the REAL graph path (token ``asset_symbol`` on an edge, a sourced
entity ``name`` on a node, an investigator label on a node) and assert the payload survives as DATA.
"""

from __future__ import annotations

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, Entity, SourceQuery, Transaction, Transfer
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.investigator import set_label
from backend.app.services.reporting import build_report_context, render_html
from backend.tests.integration._helpers import make_membership, new_case

# Three distinct breakout attempts: a raw </script> close, an HTML comment open, and the two Unicode
# line separators that terminate a JS string literal in a browser but not in JSON.
_SCRIPT_PAYLOAD = "</script><img src=x onerror=alert(1)>"
_COMMENT_PAYLOAD = "<!--</script>"
_U2028_PAYLOAD = "line sep end"


def _seed_payload_case(tmp_path):
    conn, db = new_case(tmp_path, title="Injection Test")
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    ids = {}

    def write(c, sqid):
        # A token whose on-chain SYMBOL is the attacker payload -- reaches edge `asset_symbol`.
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol=_SCRIPT_PAYLOAD,
                                           contract_address="0x" + "ab" * 20, decimals=18), sqid)
        a = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "11" * 20), sqid)
        b = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "22" * 20), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="ethereum", tx_hash="0x" + "cd" * 32, block_height=900,
            block_ts="2026-01-01T00:00:00Z", status="1", confirmations=100,
            finality_status="final"), sqid)
        repo.upsert_transfer(c, Transfer(
            transaction_id=tx, chain="ethereum", from_address_id=a, to_address_id=b,
            asset_id=asset, amount="1000000000000000000", transfer_type="erc20", position=0), sqid)
        ids["a"] = a

    write_with_provenance(conn, sq, write)

    # A sourced entity whose NAME is the comment-open payload -- reaches node `entity_label`.
    ent = repo.insert_entity(conn, Entity(origin="source", name=_COMMENT_PAYLOAD))
    make_membership(conn, entity_id=ent, address_id=ids["a"], source="arkham", method="shared-label")

    # An investigator custom label carrying the U+2028/U+2029 payload -- reaches node `custom_label`.
    set_label(conn, target_type="address", target_id=ids["a"], label=_U2028_PAYLOAD)

    return conn, db


def _bootstrap_region(html: str) -> str:
    """Everything from the bootstrap payload (``var ELEMENTS =``) to the end of the document. Deliberately
    NOT regex-terminated at ``</script>`` -- a non-greedy match would itself be fooled by an injected
    close-tag (that fooling IS the vulnerability), so we take the whole tail and count close-tags."""
    start = html.index("var ELEMENTS =")
    return html[start:]


def test_script_payload_does_not_break_out(tmp_path):
    conn, db = _seed_payload_case(tmp_path)
    ctx = build_report_context(conn, title="Injection", scope_spec={"bounds": {}},
                               generated_at="2026-01-01T00:00:00Z")
    html = render_html(ctx)
    region = _bootstrap_region(html)

    # From `var ELEMENTS =` to EOF there must be EXACTLY ONE `</script` -- the bootstrap element's own
    # legitimate closer. A payload that breaks out injects a second raw `</script` (count >= 2).
    assert region.lower().count("</script") == 1, \
        "a raw </script> (token symbol) breaks out of the inline <script>"
    body = region[: region.lower().rfind("</script")]
    # `<!--` opens script-data-escaped state in a browser; it must not appear raw in the payload.
    assert "<!--" not in body, "raw <!-- (entity name) enters script-data-escaped state"
    # The raw Unicode line/paragraph separators terminate a JS string in a browser but not in JSON.
    assert " " not in body and " " not in body, "raw U+2028/U+2029 must be escaped in <script>"

    # ...and the payload still survives as DATA (escaped), so the report is faithful, not silently dropped.
    assert "onerror" in body, "the token symbol should survive as neutralized data, not be dropped"
    conn.close()


def test_style_payload_cannot_break_out(tmp_path):
    """SEC-12 latent twin: any value interpolated into the report's inline <style> must not carry a
    </style> breakout. Today the tokens are bundled + hex-only; this guards the invariant so the planned
    customize-UI can't reintroduce it."""
    from backend.app import theme

    # A strict color-grammar validator must exist and reject a breakout string.
    assert hasattr(theme, "validate_color_value"), "theme.validate_color_value (SEC-12 guard) is missing"
    assert theme.validate_color_value("#0f172a")
    assert theme.validate_color_value("rgb(12, 20, 40)")
    for bad in ["</style><script>alert(1)</script>", "red;} body{display:none", "url(javascript:x)",
                "expression(alert(1))"]:
        assert not theme.validate_color_value(bad), f"validator accepted a breakout: {bad!r}"

    # And the emitted :root block for the real (bundled) catalog contains no style-breaking char.
    block = theme.css_root_block()
    assert "</style" not in block.lower()
    assert "<" not in block and ">" not in block
