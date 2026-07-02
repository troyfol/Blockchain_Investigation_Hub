"""Bitquery connector — optional PAID multi-chain EVM facts via GraphQL (docs/findings/
paid_api_integrations.md §1).

A fallback EVM facts source (useful when Etherscan's free chain coverage shrinks), routed through the
canonical `transaction_`/`transfer` path. The user has a token. CONFIRMED: V2 endpoint
`https://streaming.bitquery.io/graphql`, GraphQL over HTTP POST, **OAuth2 Bearer** auth
(`Authorization: Bearer <token>`); V1 (`graphql.bitquery.io` + `X-API-KEY`) is a configurable fallback.

Optional + disabled by default (`BIH_BITQUERY_ENABLED`); token in the OS keyring (`bitquery_token`),
sent as the auth header and never logged. Each call writes a `source_query` (the GraphQL JSON is the
hashed `raw_response`). The GraphQL query body + response mapping are `TODO: confirm` (built, not faked;
validated by the RUN_LIVE drift test — no fabricated cassette per the build directive).
"""

from __future__ import annotations

import httpx

from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Address, SourceQuery, Transfer
from ..normalization.bitquery_adapter import TRANSFERS_QUERY, adapt_transfers, network_for
from ..normalization.canonical import canonical_address
from ..normalization.reconcile import assign_occurrences
from ..provenance.atomic import write_with_provenance
from ..secrets import get_secret
from .base import BaseHttpConnector, ConnectorError, UpstreamError

BITQUERY_KEY_NAME = "bitquery_token"
DEFAULT_LIMIT = 1000


class BitqueryConnector(BaseHttpConnector):
    name = "bitquery"

    def __init__(self, *, settings=None, token: str | None = None, base_url: str | None = None,
                 use_v1: bool | None = None, **kw):
        use_v1 = settings.bitquery_use_v1 if (use_v1 is None and settings) else bool(use_v1)
        if base_url is None and settings is not None:
            base_url = settings.bitquery_v1_base_url if use_v1 else settings.bitquery_base_url
        super().__init__(base_url=base_url or "https://streaming.bitquery.io/graphql", **kw)
        self.settings = settings
        self.use_v1 = use_v1
        key = token if token is not None else get_secret(BITQUERY_KEY_NAME)
        if key:
            # V2: OAuth2 Bearer; V1: X-API-KEY. The token is sent as a header, never logged.
            self._client.headers["X-API-KEY" if use_v1 else "Authorization"] = (
                key if use_v1 else f"Bearer {key}")
        self._has_key = bool(key)

    def capabilities(self) -> set[str]:
        return {"get_transactions", "get_transfers"}

    def supported_chains(self) -> set[str]:
        from ..normalization.bitquery_adapter import CHAIN_TO_NETWORK
        return set(CHAIN_TO_NETWORK)

    def _require_key(self) -> None:
        if not self._has_key:
            raise ConnectorError(
                f"Bitquery token not set — store it in the keyring as {BITQUERY_KEY_NAME!r} "
                f"(BIH_BITQUERY_ENABLED must also be on). Cannot query without it.")

    def _graphql(self, query: str, variables: dict) -> dict:
        """POST a GraphQL query with the base's rate-limit + 429/5xx backoff. Raises on GraphQL errors."""
        body = {"query": query, "variables": variables}
        attempt = 0
        while True:
            self.rate_limiter.acquire()
            try:
                resp = self._client.post(self.base_url, json=body)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise UpstreamError(f"Bitquery transport error after {attempt} retries: {exc!r}") from exc
                self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt >= self.max_retries:
                    raise UpstreamError(f"Bitquery HTTP {resp.status_code} after {attempt} retries")
                self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ConnectorError(
                    f"Bitquery returned a non-object body: {type(payload).__name__}")
            if payload.get("errors"):
                raise UpstreamError(f"Bitquery GraphQL errors: {payload['errors']}")
            return payload

    def _network(self, chain: str) -> str:
        net = network_for(chain)
        if net is None:
            raise UpstreamError(f"Bitquery: unsupported EVM chain {chain!r} (TODO: confirm the network slug)")
        return net

    def _write(self, conn, sqid, parsed) -> dict:
        assign_occurrences(parsed)  # content+occurrence dedup key (decision (c)) before the DB write
        n_tx = n_tr = 0
        for pt in parsed:
            tx_id = repo.upsert_transaction(conn, pt.transaction, sqid, authoritative=True)
            n_tx += 1
            for tr in pt.transfers:
                from_id = (repo.upsert_address(conn, Address(  # COR-02: keep the source checksum form
                    chain=tr.chain, address_display=tr.from_address_display or tr.from_address), sqid)
                           if tr.from_address else None)
                to_id = (repo.upsert_address(conn, Address(
                    chain=tr.chain, address_display=tr.to_address_display or tr.to_address), sqid)
                         if tr.to_address else None)
                asset_id = repo.upsert_asset(conn, tr.asset, sqid)
                repo.upsert_transfer(conn, Transfer(
                    transaction_id=tx_id, chain=tr.chain, from_address_id=from_id, to_address_id=to_id,
                    asset_id=asset_id, amount=tr.amount, transfer_type=tr.transfer_type,
                    position=tr.position, occurrence=tr.occurrence), sqid)
                n_tr += 1
        return {"transactions": n_tx, "transfers": n_tr}

    def get_transactions(self, conn, chain: str, address: str, bounds: dict | None = None,
                         *, capability: str = "get_transactions") -> dict:
        self._require_key()
        net = self._network(chain)
        canonical = canonical_address(chain, address)
        # max_pages may be present-but-None (Bounds TypedDict allows int|None) — coerce defensively so a
        # `{"max_pages": null}` request can't reach int(None) and clamp to >=1 (never send limit:{count:0}).
        max_pages = (bounds or {}).get("max_pages") or 1
        limit = max(1, int(max_pages)) * DEFAULT_LIMIT
        payload = self._graphql(TRANSFERS_QUERY, {"network": net, "address": canonical, "limit": limit})
        parsed, notes = adapt_transfers(payload, chain=chain)
        now = utc_now_iso()
        sq = SourceQuery(
            connector=self.name, capability=capability, endpoint=f"graphql/{net}",
            params={"address": canonical, "chain": chain, "network": net, "limit": limit,
                    "bounds": dict(bounds) if bounds else "default"},
            requested_at=now, completed_at=now, status="ok",
            result_summary=f"{notes['transfers']} transfers, {len(parsed)} txs")
        _, res = write_with_provenance(
            conn, sq, lambda c, sqid: self._write(c, sqid, parsed), raw_response=payload)
        return res

    def get_transfers(self, conn, chain: str, address: str, bounds: dict | None = None) -> dict:
        # tx-scoped single-tx fetch is a TODO: confirm follow-up; for now it runs the address query but
        # records its OWN capability so provenance accurately labels which capability produced the facts.
        return self.get_transactions(conn, chain, address, bounds, capability="get_transfers")
