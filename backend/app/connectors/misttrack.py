"""MisTrack API connector — optional PAID risk + attribution (docs/findings/misttrack_reconciliation.md,
docs/findings/paid_api_integrations.md §2).

Re-scoped from the retired CSV importer to the real API (Invariant #1 — the official API is the
preferred path). Two endpoints, two capabilities, stored raw-per-source (Invariant #4 — never combined
with another source's score):
  - `get_risk`        → `GET /v2/risk_score?coin=…&address=…` → `RiskAssessment` (score **3-100**).
  - `get_attributions`→ `GET /v1/address_labels?coin=…&address=…` → `Attribution`.

Optional + disabled by default (`BIH_MISTRACK_ENABLED`); key in the OS keyring (`misttrack_api_key`),
passed as the API's `api_key` query param and **never recorded** in `source_query.params`. A call
without a key raises a clear `ConnectorError` naming the keyring entry. Every call writes a
`source_query` (the JSON response is the hashed `raw_response`) even on an empty result (Invariant #3).

`TODO: confirm` the live envelope + V2-vs-V3 field names before production (no key at build).
"""

from __future__ import annotations

from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Address, Attribution, RiskAssessment, SourceQuery
from ..normalization.canonical import canonical_address
from ..normalization.misttrack_adapter import CHAIN_TO_COIN, adapt_labels, adapt_risk, coin_for
from ..provenance.atomic import write_with_provenance
from ..secrets import get_secret
from .base import BaseHttpConnector, ConnectorError, UpstreamError

MISTRACK_KEY_NAME = "misttrack_api_key"


class MisTrackConnector(BaseHttpConnector):
    name = "misttrack-api"
    source = "misttrack"

    def __init__(self, *, settings=None, api_key: str | None = None, base_url: str | None = None, **kw):
        base = base_url or (settings.misttrack_base_url if settings else "https://openapi.misttrack.io")
        super().__init__(base_url=base, **kw)
        self.settings = settings
        self.api_key = api_key if api_key is not None else get_secret(MISTRACK_KEY_NAME)
        self._has_key = bool(self.api_key)

    def capabilities(self) -> set[str]:
        return {"get_risk", "get_attributions"}

    # --- shared helpers ----------------------------------------------------------------------

    def _require_key(self) -> None:
        if not self._has_key:
            raise ConnectorError(
                f"MisTrack API key not set — store it in the keyring as {MISTRACK_KEY_NAME!r} "
                f"(BIH_MISTRACK_ENABLED must also be on). Cannot query without it.")

    def _coin(self, chain: str) -> str:
        coin = coin_for(chain)
        if coin is None:
            raise ConnectorError(
                f"MisTrack: no coin mapping for chain {chain!r} (TODO: confirm the coin list). "
                f"Supported: {sorted(CHAIN_TO_COIN)}")
        return coin

    def _fetch(self, path: str, coin: str, address: str) -> tuple[dict, dict]:
        """GET ``path`` with coin/address/api_key; the api_key is sent but NEVER recorded. Returns
        ``(full_payload, data_object)`` (defensive: a non-dict body / missing data -> {})."""
        # api_key as a query param (MisTrack's auth) — kept out of the recorded source_query.params.
        payload = self.request(path=path, params={
            "coin": coin, "address": address, "api_key": self.api_key}).json()
        if not isinstance(payload, dict):
            raise UpstreamError(f"MisTrack returned a non-object body for {path}: {type(payload).__name__}")
        if payload.get("success") is False:  # documented failure envelope
            raise UpstreamError(f"MisTrack {path}: {payload.get('msg') or payload.get('message') or 'error'}")
        data = payload.get("data")
        return payload, (data if isinstance(data, dict) else {})

    def _sq(self, capability: str, endpoint: str, coin: str, address: str, chain: str, now: str,
            summary: str) -> SourceQuery:
        return SourceQuery(
            connector=self.name, capability=capability, endpoint=endpoint,
            params={"address": address, "coin": coin, "chain": chain, "bounds": "default"},  # no api_key
            requested_at=now, completed_at=now, status="ok", result_summary=summary)

    # --- capabilities --------------------------------------------------------------------

    def get_risk(self, conn, chain: str, address: str, *, now=None) -> dict:
        self._require_key()
        coin = self._coin(chain)
        canonical = canonical_address(chain, address)
        payload, data = self._fetch("/v2/risk_score", coin, canonical)
        risk = adapt_risk(data)
        now = now or utc_now_iso()
        sq = self._sq("get_risk", "v2/risk_score", coin, canonical, chain, now,
                      f"score={risk.score if risk else None}")

        def write(c, sqid):
            addr_id = repo.upsert_address(c, Address(chain=chain, address_display=address), sqid)
            if risk is None or risk.score is None:
                return {"risks": 0}  # no usable score -> the check is still recorded (provenance), no row
            repo.upsert_risk_assessment(c, RiskAssessment(
                address_id=addr_id, score=risk.score, score_scale=risk.score_scale,
                category=risk.category, rationale=risk.rationale, source=self.source, retrieved_at=now), sqid)
            return {"risks": 1, "score": risk.score, "category": risk.category}

        _, res = write_with_provenance(conn, sq, write, raw_response=payload)
        return res

    def get_attributions(self, conn, chain: str, address: str, *, now=None) -> dict:
        self._require_key()
        coin = self._coin(chain)
        canonical = canonical_address(chain, address)
        payload, data = self._fetch("/v1/address_labels", coin, canonical)
        labels = adapt_labels(data)
        now = now or utc_now_iso()
        sq = self._sq("get_attributions", "v1/address_labels", coin, canonical, chain, now,
                      f"{len(labels)} label(s)")

        def write(c, sqid):
            addr_id = repo.upsert_address(c, Address(chain=chain, address_display=address), sqid)
            n = 0
            for lab in labels:
                repo.upsert_attribution(c, Attribution(
                    address_id=addr_id, label=lab.label, category=lab.category, source=self.source,
                    note=lab.note, retrieved_at=now), sqid)
                n += 1
            return {"attributions": n}

        _, res = write_with_provenance(conn, sq, write, raw_response=payload)
        return res
