"""Arkham API connector — optional PAID attribution (Path B) + numeric risk (docs/findings/
paid_api_integrations.md §3, CONFIRMED 2026-06-28).

The real Arkham *attribution* source (the UI transfer export `imports/arkham.py` carries no attribution).
Two capabilities, stored raw-per-source (Invariant #4):
  - `get_attributions` → `GET /intelligence/address/{address}?chain=…` → `entity`/`entity_membership`/
    `attribution`. A confirmed `arkhamEntity` and a probabilistic `predictedEntity` are written SEPARATELY
    at different confidence — never collapsed (Invariant #4).
  - `get_risk`         → `GET /risk/address/{address}` → `RiskAssessment(score=max_score, scale='0-100',
    category=greatest_risk_category)`, the per-category breakdown kept raw.

Optional + disabled by default (`BIH_ARKHAM_API_ENABLED`); key in the OS keyring (`arkham_api_key`),
sent as the `API-Key` header and never logged. Each call writes a `source_query` (the JSON response is
the hashed `raw_response`) even on an empty result (Invariant #3).
"""

from __future__ import annotations

from urllib.parse import quote

from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Address, Attribution, EntityMembership, RiskAssessment, RiskDetail, SourceQuery
from ..normalization.arkham_api_adapter import adapt_address, adapt_risk, entity_key
from ..normalization.canonical import canonical_address
from ..provenance.atomic import write_with_provenance
from ..secrets import get_secret
from .base import BaseHttpConnector, ConnectorError

ARKHAM_KEY_NAME = "arkham_api_key"


class ArkhamApiConnector(BaseHttpConnector):
    name = "arkham-api"
    source = "arkham-api"

    def __init__(self, *, settings=None, api_key: str | None = None, base_url: str | None = None, **kw):
        base = base_url or (settings.arkham_api_base_url if settings else "https://api.arkm.com")
        super().__init__(base_url=base, **kw)
        self.settings = settings
        key = api_key if api_key is not None else get_secret(ARKHAM_KEY_NAME)
        if key:
            self._client.headers["API-Key"] = key  # Arkham auth header; never logged
        self._has_key = bool(key)

    def capabilities(self) -> set[str]:
        return {"get_attributions", "get_risk"}

    def _require_key(self) -> None:
        if not self._has_key:
            raise ConnectorError(
                f"Arkham API key not set — store it in the keyring as {ARKHAM_KEY_NAME!r} "
                f"(BIH_ARKHAM_API_ENABLED must also be on). Cannot query without it.")

    def _get(self, path: str) -> dict:
        payload = self.request(path=path).json()
        if not isinstance(payload, dict):
            raise ConnectorError(
                f"Arkham returned an unexpected body shape for {path}: {type(payload).__name__}")
        return payload

    # --- get_attributions: entity + label + predicted + deposit-service ----------------------

    def get_attributions(self, conn, chain: str, address: str, *, now=None) -> dict:
        self._require_key()
        canonical = canonical_address(chain, address)
        payload = self._get(f"/intelligence/address/{quote(canonical, safe='')}?chain={quote(chain, safe='')}")
        plan = adapt_address(payload)
        now = now or utc_now_iso()
        sq = SourceQuery(
            connector=self.name, capability="get_attributions", endpoint="intelligence/address",
            params={"address": canonical, "chain": chain, "bounds": "default"},
            requested_at=now, completed_at=now, status="ok",
            result_summary=f"{len(plan.entities)} entity(s), {len(plan.attributions)} attribution(s)")

        def write(c, sqid):
            addr_id = repo.upsert_address(c, Address(chain=chain, address_display=address), sqid)
            n_ent = n_attr = 0
            for e in plan.entities:
                # Confirmed and predicted entities resolve to DISTINCT entities/memberships — the
                # method ('arkham-entity' vs 'arkham-predicted') + confidence keep them side-by-side.
                eid, _ = repo.find_or_create_source_entity(
                    c, external_id=entity_key(e), name=e.name, entity_type=e.entity_type, now=now)
                repo.upsert_entity_membership(c, EntityMembership(
                    entity_id=eid, address_id=addr_id, source=self.source, method=e.method,
                    confidence=e.confidence), sqid, now=now)
                n_ent += 1
            for a in plan.attributions:
                repo.upsert_attribution(c, Attribution(
                    address_id=addr_id, label=a.label, category=a.category, source=self.source,
                    confidence=a.confidence, note=a.note, retrieved_at=now), sqid)
                n_attr += 1
            return {"entities": n_ent, "attributions": n_attr}

        _, res = write_with_provenance(conn, sq, write, raw_response=payload)
        return res

    # --- get_risk: numeric max_score + raw category breakdown --------------------------------

    def get_risk(self, conn, chain: str, address: str, *, now=None) -> dict:
        self._require_key()
        canonical = canonical_address(chain, address)
        payload = self._get(f"/risk/address/{quote(canonical, safe='')}")
        risk = adapt_risk(payload)
        now = now or utc_now_iso()
        sq = SourceQuery(
            connector=self.name, capability="get_risk", endpoint="risk/address",
            params={"address": canonical, "chain": chain, "bounds": "default"},
            requested_at=now, completed_at=now, status="ok",
            result_summary=f"max_score={risk.score if risk else None}")

        def write(c, sqid):
            addr_id = repo.upsert_address(c, Address(chain=chain, address_display=address), sqid)
            if risk is None or risk.score is None:
                return {"risks": 0}  # no score -> still records the check (provenance), no row
            ra_id = repo.upsert_risk_assessment(c, RiskAssessment(
                address_id=addr_id, score=risk.score, score_scale=risk.score_scale,
                category=risk.category, rationale=risk.rationale, source=self.source, retrieved_at=now), sqid)
            # FN-15: each per-category sub-signal → a first-class RAW risk_detail row (Invariant #4).
            for d in risk.details:
                repo.insert_risk_detail(c, RiskDetail(
                    risk_assessment_id=ra_id, signal=d.signal, score=d.score,
                    score_scale=risk.score_scale), sqid)
            return {"risks": 1, "score": risk.score, "category": risk.category,
                    "detail_signals": len(risk.details)}

        _, res = write_with_provenance(conn, sq, write, raw_response=payload)
        return res
