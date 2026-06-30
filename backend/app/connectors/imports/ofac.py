"""OFAC SDN sanctions import — the free risk pillar (Phase A; docs/findings/
ofac_sanctions_reconciliation.md, docs/connectors.md §6).

Sanctions screening is free and authoritative. This connector ingests the official OFAC SDN list (a
local copy of the public `sdn.xml` — a structured import of public data, Invariant #1, no per-call
fetch) and writes a **categorical** `risk_assessment(category='sanctioned', score=None)` per sanctioned
BTC/EVM address, with `rationale` carrying the SDN entity name + program(s). It is the free risk pillar
complementing GraphSense `abuse` tags (see the `project-bih-sourcing-architecture` memory). The pure
`adapt_sdn_xml` adapter does the XML parse + ticker→chain filter; the connector writes via the repo.
The SDN file's bytes are the import's `source_query.raw_response` (hashed), and the SDN publication date
is the `endpoint`, so provenance records *which edition* of this mutable list was ingested (Invariant #3).

**Sanctions are mutable (findings §1).** OFAC delists addresses; these are NOT permanent claims. We
record each fetch as a dated observation (idempotent on content) and **report** addresses that were
previously sanctioned but are absent from the current fetch (`delisted`) — we do NOT delete the
historical claim ("address X was on the SDN as of <date>" stays true and provenance-backed; deleting a
claim would violate the append-only claims invariant + audit). The *current* list is the latest fetch
(its `source_query` + `retrieved_at`); a delisted address simply isn't re-asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...db import repository as repo
from ...models import Address, Attribution, RiskAssessment
from ...normalization.ofac_adapter import adapt_sdn_xml
from ..base import ConnectorError
from .base import ImportConnector, known_case_addresses

SDN_URI = "https://www.treasury.gov/ofac/downloads/sdn.xml"


def _parse_sdn_date(value: str | None) -> tuple[int, int, int] | None:
    """OFAC publish date 'MM/DD/YYYY' -> a sortable (yyyy, mm, dd) tuple, or None if unparseable."""
    if not value:
        return None
    parts = str(value).split("/")
    if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
        return None
    mm, dd, yyyy = (int(p) for p in parts)
    return (yyyy, mm, dd)


class OfacSdnImporter(ImportConnector):
    name = "ofac-sdn-import"
    source = "ofac-sdn"

    def capabilities(self) -> set[str]:
        return {"get_risk", "get_attributions"}

    # --- shared helpers ----------------------------------------------------------------------

    @staticmethod
    def _raise_on_errors(notes: dict) -> None:
        """A malformed address on a *supported* chain (or unparseable XML) fails the whole import
        (all-or-nothing) — a corrupt/hostile file errors loudly, never a partial write."""
        if notes["errors"]:
            first = notes["errors"][0]
            raise ConnectorError(
                f"OFAC SDN file has {len(notes['errors'])} unparseable record(s); first "
                f"(entry {first['entry']!r}): {first['reason']}. Nothing was imported.")

    @staticmethod
    def _skip_report(notes: dict) -> dict:
        return {"skipped_unsupported": len(notes["skipped_unsupported"]),
                "unsupported_tickers": sorted({s["ticker"] for s in notes["skipped_unsupported"]}),
                "entries": notes["entries"], "digital_currency_ids": notes["digital_currency_ids"]}

    def _endpoint(self, notes: dict) -> str:
        return f"OFAC SDN {notes['publish_date']}" if notes.get("publish_date") else "sdn.xml"

    def _extra_params(self, notes: dict) -> dict:
        return {"sdn_uri": SDN_URI, "sdn_publish_date": notes.get("publish_date")}

    # --- Phase A: sanctions -> categorical risk ----------------------------------------------

    def get_risk(self, conn, file_path, *, now=None, only_known_addresses=False) -> dict:
        """Ingest the OFAC SDN list -> categorical `risk_assessment(category='sanctioned')`. Idempotent
        re-ingest (Invariant #7); reports `delisted` addresses absent from the current fetch. With
        ``only_known_addresses`` (the intel-enrichment path, P8.7.1 #1) ONLY addresses already in the case
        are flagged — the SDN's other addresses are never injected into an unrelated case."""
        sanctions, notes = adapt_sdn_xml(Path(file_path).read_bytes())
        return self._ingest(conn, file_path=file_path, capability="get_risk",
                            endpoint=self._endpoint(notes), extra_params=self._extra_params(notes),
                            parse=lambda c, sqid, _raw, n: self._write_risk(c, sqid, sanctions, notes, n, only_known_addresses),
                            now=now)

    def _write_risk(self, c, sqid, sanctions, notes, now, only_known=False) -> dict:
        self._raise_on_errors(notes)  # inside the txn -> the source_query rolls back too on error
        known = known_case_addresses(c) if only_known else None
        current: set[tuple[str, str]] = set()
        seen: set[tuple[str, str, str]] = set()  # dedup the COUNT (one addr under multiple tickers = 1 row)
        n = 0
        for s in sanctions:
            addr_id = self._resolve_address(c, sqid, s.chain, s.address_display, known)
            if addr_id is None:
                continue  # intel scoping: this SDN address is not in the case -> skip (never inject)
            repo.upsert_risk_assessment(c, RiskAssessment(
                address_id=addr_id, score=None, score_scale=None, category="sanctioned",
                source=self.source, rationale=s.rationale, retrieved_at=now), sqid)
            current.add((s.chain, s.address_canonical))
            key = (s.chain, s.address_canonical, s.rationale)
            if key not in seen:
                seen.add(key)
                n += 1  # count rows actually written, not raw ids (dedups the natural-key)
        # The delisting diff is only meaningful when this is the newest edition seen — out-of-order
        # ingestion (an older file after a newer one) would otherwise mislabel current addresses.
        stale = self._is_stale_edition(c, sqid, notes.get("publish_date"))
        delisted = [] if stale else self._delisted(c, current)
        return {"risks": n, "delisted": delisted, "stale_edition": stale, **self._skip_report(notes)}

    def _delisted(self, c, current: set[tuple[str, str]]) -> list[str]:
        """Addresses with a prior `ofac-sdn` sanctioned claim that are ABSENT from this fetch — surfaced
        (not deleted). Returns sorted ``chain:address`` strings."""
        rows = c.execute(
            "SELECT DISTINCT a.chain, a.address FROM risk_assessment r JOIN address a ON a.id=r.address_id "
            "WHERE r.source=? AND r.category='sanctioned'", (self.source,)).fetchall()
        known = {(r["chain"], r["address"]) for r in rows}
        return sorted(f"{chain}:{addr}" for chain, addr in (known - current))

    def _is_stale_edition(self, c, sqid, publish_date) -> bool:
        """True if a NEWER OFAC edition was already ingested into this case — meaning this file is an
        older/out-of-order fetch and its delisting diff would be misleading. Best-effort: if dates can't
        be parsed, assume current (False). Excludes this fetch's own (already-inserted) source_query."""
        cur = _parse_sdn_date(publish_date)
        if cur is None:
            return False
        rows = c.execute(
            "SELECT params FROM source_query WHERE connector=? AND id<>?", (self.name, sqid)).fetchall()
        for r in rows:
            try:
                prior = json.loads(r["params"]).get("sdn_publish_date") if r["params"] else None
            except (TypeError, ValueError):
                prior = None
            d = _parse_sdn_date(prior)
            if d is not None and d > cur:
                return True
        return False

    # --- Phase B: optional sanctioned-entity attribution -------------------------------------

    def get_attributions(self, conn, file_path, *, now=None, only_known_addresses=False) -> dict:
        """Also emit an `attribution(category='sanctioned_entity')` per sanctioned address whose SDN
        entry supplies an entity name — a free, authoritative attribution. Idempotent (Invariant #7).
        ``only_known_addresses`` scopes enrichment to addresses already in the case (P8.7.1 #1)."""
        sanctions, notes = adapt_sdn_xml(Path(file_path).read_bytes())
        return self._ingest(conn, file_path=file_path, capability="get_attributions",
                            endpoint=self._endpoint(notes), extra_params=self._extra_params(notes),
                            parse=lambda c, sqid, _raw, n: self._write_attributions(c, sqid, sanctions, notes, n, only_known_addresses),
                            now=now)

    def _write_attributions(self, c, sqid, sanctions, notes, now, only_known=False) -> dict:
        self._raise_on_errors(notes)
        known = known_case_addresses(c) if only_known else None
        seen: set[tuple[str, str, str | None]] = set()
        n = 0
        for s in sanctions:
            if not s.entity_name or s.entity_name == "(unknown)":
                continue  # no name in the SDN entry -> no attribution (don't synthesize one)
            addr_id = self._resolve_address(c, sqid, s.chain, s.address_display, known)
            if addr_id is None:
                continue  # intel scoping: skip an SDN address the case doesn't have (never inject)
            note = f"OFAC SDN programs: {', '.join(s.programs)}" if s.programs else None
            repo.upsert_attribution(c, Attribution(
                address_id=addr_id, label=s.entity_name, category="sanctioned_entity",
                source=self.source, confidence=None, note=note, retrieved_at=now), sqid)
            key = (s.chain, s.address_canonical, note)
            if key not in seen:
                seen.add(key)
                n += 1  # count rows written (one addr under multiple tickers = one attribution)
        return {"attributions": n, **self._skip_report(notes)}
