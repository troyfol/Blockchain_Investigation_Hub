"""OKLink connector — optional PAID AML labels + risk (SHELL; docs/findings/paid_api_integrations.md §4).

**Partially confirmed.** The build-time scrape captured OKLink's *Explorer* conventions (which hold
API-wide) but NOT the AML data pages BIH wants. So this is a SHELL: the confirmed conventions are wired,
but the two AML endpoint paths + response fields are `TODO: confirm` and the capabilities raise a clear
"not yet wired" error rather than guess an endpoint (CLAUDE.md §6 — do not invent endpoint shapes).

Confirmed conventions (2026-06-28):
  - base `https://www.oklink.com/api/v5/explorer/`; chain via a **`chainShortName`** param (ETH/BSC/…);
  - response envelope `{"code":"0","msg":"","data":[…]}` (`code=="0"` ⇒ success);
  - auth header `TODO: confirm` (OKX/OKLink convention is `Ok-Access-Key`, unverified).

Optional + disabled by default (`BIH_OKLINK_ENABLED`); key in the OS keyring (`oklink_api_key`).
"""

from __future__ import annotations

from ..secrets import get_secret
from .base import BaseHttpConnector, ConnectorError, UpstreamError

OKLINK_KEY_NAME = "oklink_api_key"
OK_ACCESS_KEY_HEADER = "Ok-Access-Key"  # TODO: confirm the exact auth header

# Canonical chain -> OKLink `chainShortName` (confirmed convention). TODO: confirm the full chain list.
CHAIN_TO_SHORTNAME: dict[str, str] = {
    "ethereum": "ETH", "bsc": "BSC", "polygon": "POLYGON", "arbitrum": "ARBITRUM",
    "optimism": "OPTIMISM", "base": "BASE", "bitcoin": "BTC", "tron": "TRON",
}


class OkLinkConnector(BaseHttpConnector):
    name = "oklink"
    source = "oklink"

    def __init__(self, *, settings=None, api_key: str | None = None, base_url: str | None = None, **kw):
        base = base_url or (settings.oklink_base_url if settings else
                            "https://www.oklink.com/api/v5/explorer/")
        super().__init__(base_url=base, **kw)
        self.settings = settings
        key = api_key if api_key is not None else get_secret(OKLINK_KEY_NAME)
        if key:
            self._client.headers[OK_ACCESS_KEY_HEADER] = key  # TODO: confirm header; never logged
        self._has_key = bool(key)

    def capabilities(self) -> set[str]:
        return {"get_attributions", "get_risk"}

    def _require_key(self) -> None:
        if not self._has_key:
            raise ConnectorError(
                f"OKLink API key not set — store it in the keyring as {OKLINK_KEY_NAME!r} "
                f"(BIH_OKLINK_ENABLED must also be on). Cannot query without it.")

    def short_name(self, chain: str) -> str:
        sn = CHAIN_TO_SHORTNAME.get(chain.lower())
        if sn is None:
            raise UpstreamError(f"OKLink: no chainShortName for chain {chain!r} (TODO: confirm)")
        return sn

    def _data(self, payload: dict) -> list:
        """Unwrap the confirmed `{"code","msg","data"}` envelope (code "0" = success)."""
        if not isinstance(payload, dict):
            raise ConnectorError(f"OKLink returned a non-object body: {type(payload).__name__}")
        if str(payload.get("code")) != "0":
            raise UpstreamError(f"OKLink error {payload.get('code')}: {payload.get('msg')}")
        data = payload.get("data")
        return data if isinstance(data, list) else []

    def _not_wired(self, capability: str):
        raise ConnectorError(
            f"OKLink {capability} is scaffolded but not wired — the AML endpoint path + response fields "
            f"are TODO: confirm (the scraped docs were the Explorer module, not the AML data). Capture "
            f"the AML OpenAPI/Postman page, then fill in the endpoint. Conventions ready: base, "
            f"chainShortName, the {{code,msg,data}} envelope.")

    def get_attributions(self, conn, chain: str, address: str, *, now=None) -> dict:
        self._require_key()
        # The AML endpoint is unimplemented for ALL chains, so report "not wired" FIRST — a chain-mapping
        # error here would mislead the operator into thinking only the chain is the problem.
        self._not_wired("get_attributions")  # TODO: confirm /address/label (or equivalent) endpoint

    def get_risk(self, conn, chain: str, address: str, *, now=None) -> dict:
        self._require_key()
        self._not_wired("get_risk")  # TODO: confirm the KYT/KYA risk endpoint
