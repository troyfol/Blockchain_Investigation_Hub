"""Chainalysis free sanctions-screening API — second free risk source (Phase C; docs/findings/
ofac_sanctions_reconciliation.md §"Source 2", docs/connectors.md §6).

A free (key-gated) sanctions screener: ``GET https://public.chainalysis.com/api/v1/address/{address}``
with an ``X-API-Key`` header returns ``identifications[]`` (empty ⇒ not sanctioned). Each identification
becomes a CATEGORICAL ``risk_assessment(source='chainalysis-sanctions', score=None)`` — stored
**side-by-side** with OFAC, never merged (Invariant #4): two sanctions sources may differ, and the
investigator sees both. The key lives in the OS keyring (``chainalysis_api_key``); it is never logged.

TODO: confirm the exact response field names against the live API (no key available at build to record a
fresh cassette). The mapping below follows the documented shape (``identifications[].category/name/
description/url``); the field reads are defensive (``.get``) so a renamed field degrades, not crashes.
A negative result (empty ``identifications``) still records a ``source_query`` — "we checked X, clean as
of <date>" is itself a provenance-worthy observation.
"""

from __future__ import annotations

from urllib.parse import quote

from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Address, RiskAssessment, SourceQuery
from ..normalization.canonical import canonical_address
from ..provenance.atomic import write_with_provenance
from ..secrets import get_secret
from .base import BaseHttpConnector, ConnectorError

CHAINALYSIS_KEY_NAME = "chainalysis_api_key"
DEFAULT_BASE_URL = "https://public.chainalysis.com/api/v1"


class ChainalysisSanctionsConnector(BaseHttpConnector):
    name = "chainalysis-sanctions"
    source = "chainalysis-sanctions"

    def __init__(self, *, settings=None, api_key: str | None = None, base_url: str | None = None, **kw):
        super().__init__(base_url=base_url or DEFAULT_BASE_URL, **kw)
        self.settings = settings
        key = api_key if api_key is not None else get_secret(CHAINALYSIS_KEY_NAME)
        if key:
            self._client.headers["X-API-Key"] = key  # sent on every request; never logged
        self._has_key = bool(key)

    def capabilities(self) -> set[str]:
        return {"get_risk"}

    def get_risk(self, conn, chain: str, address: str, *, now=None) -> dict:
        """Screen one address against Chainalysis sanctions; write a categorical risk row per
        identification (side-by-side with OFAC — Invariant #4). Records the check even when clean."""
        if not self._has_key:
            raise ConnectorError(
                f"Chainalysis API key not set — store it in the keyring as {CHAINALYSIS_KEY_NAME!r} "
                f"(free via Chainalysis's request form). Cannot screen without it.")
        canonical = canonical_address(chain, address)
        # URL-encode the address segment: canonical_address does NOT charset-validate a Bitcoin
        # address (canonical.py defers that to the connector), so a malformed addr with '/'?'#'/'..'
        # could otherwise silently change the queried endpoint and the recorded provenance (Inv #3).
        payload = self.request(path=f"/address/{quote(canonical, safe='')}").json()
        if not isinstance(payload, dict):  # unconfirmed API shape -> fail clean, not a raw AttributeError
            raise ConnectorError(
                f"Chainalysis returned an unexpected body shape for {canonical}: {type(payload).__name__}")
        identifications = payload.get("identifications") or []  # TODO: confirm field name
        now = now or utc_now_iso()
        sq = SourceQuery(
            connector=self.name, capability="get_risk", endpoint=f"address/{canonical}",
            params={"address": canonical, "chain": chain, "bounds": "default"},
            requested_at=now, completed_at=now, status="ok",
            result_summary=f"{len(identifications)} identification(s)")

        def write(c, sqid):
            addr_id = repo.upsert_address(c, Address(chain=chain, address_display=address), sqid)
            n = 0
            for ident in identifications:
                # TODO: confirm exact field names. Defensive reads so a rename degrades, not crashes.
                # We PRESERVE the source's own `category` (e.g. 'sanctions'/'pep') rather than flatten it
                # to OFAC's fixed 'sanctioned' — Invariant #4 stores each source's classification raw;
                # the two sources are kept side-by-side anyway. Falls back to 'sanctioned' if absent.
                category = ident.get("category") or "sanctioned"
                name = ident.get("name")
                description = ident.get("description")
                rationale = " — ".join(x for x in (name, description) if x) or None
                repo.upsert_risk_assessment(c, RiskAssessment(
                    address_id=addr_id, score=None, score_scale=None, category=category,
                    source=self.source, rationale=rationale, retrieved_at=now), sqid)
                n += 1
            return {"risks": n, "identifications": len(identifications), "sanctioned": bool(identifications)}

        _, res = write_with_provenance(conn, sq, write, raw_response=payload)
        return res
