"""DeFiLlama price connector (phase_05 step 1; docs/connectors.md §5).

Enrichment connector keyed by asset + timestamp (NOT address-scoped). ``get_price`` returns the
USD price-at-time for an asset, or ``None`` when the coin/price is absent (an honest gap — the
valuation service then writes NO row, never a fabricated zero).

RE-CONFIRMED live 2026-06-28 (docs/findings/external_facts_confirmation.md): base
``https://coins.llama.fi``; ``/prices/historical/{unix_ts}/{coins}`` where coins is comma-joined keys —
``{chain}:{contract}`` for ERC-20 (contract must be LOWERCASE — a mixed-case key returned nothing live;
production ``canonical_address`` lowercases EVM contracts, so real keys are already lowercase),
``coingecko:{id}`` for native coins. Response ``{"coins": {"<key>": {"price","symbol","timestamp",
"confidence"[,"decimals"]}}}`` (confidence 0-1). **``decimals`` is NOT returned for ``coingecko:`` keys**
— ``coin.get("decimals")`` may be ``None``; tolerated, never assumed (valuation uses the DB asset's decimals).
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import BaseHttpConnector, UpstreamError

# Native-coin CoinGecko ids per chain. L2s that pay gas in ETH value as ETH. Confirmed live 2026-06-28:
# coingecko:binancecoin -> BNB price (conf 0.99); coingecko:ethereum -> ETH (conf 0.99).
NATIVE_COINGECKO_ID = {
    "ethereum": "ethereum", "arbitrum": "ethereum", "optimism": "ethereum",
    "base": "ethereum", "polygon": "matic-network", "bsc": "binancecoin", "bitcoin": "bitcoin",
}


@dataclass
class PriceRecord:
    key: str
    price: str               # carried as text for exact Decimal math downstream
    symbol: str | None
    decimals: int | None
    price_timestamp: int     # unix seconds (the price's own timestamp)
    confidence: float | None
    raw: dict


class DeFiLlamaConnector(BaseHttpConnector):
    name = "defillama"

    def __init__(self, *, settings, base_url: str | None = None, **kw):
        super().__init__(base_url=base_url or settings.defillama_base_url, **kw)
        self.settings = settings

    def capabilities(self) -> set[str]:
        return {"get_price"}

    def supported_chains(self) -> set[str]:
        return set(NATIVE_COINGECKO_ID)

    def coin_key(self, chain: str, asset) -> str:
        if asset.contract_address:  # ERC-20 / token
            return f"{chain.lower()}:{asset.contract_address}"
        gid = NATIVE_COINGECKO_ID.get(chain.lower())
        if not gid:
            raise UpstreamError(f"no native-coin price key for chain {chain!r}")
        return f"coingecko:{gid}"

    def _record(self, key: str, coin: dict, timestamp: int, raw: dict) -> PriceRecord | None:
        if not coin or coin.get("price") is None:
            return None  # missing price → honest gap (no valuation row, never a fabricated zero)
        return PriceRecord(
            key=key, price=str(coin["price"]), symbol=coin.get("symbol"),
            decimals=coin.get("decimals"), price_timestamp=int(coin.get("timestamp", timestamp)),
            confidence=coin.get("confidence"), raw=raw)

    def get_price(self, chain: str, asset, timestamp: int) -> PriceRecord | None:
        key = self.coin_key(chain, asset)
        payload = self.request(path=f"/prices/historical/{int(timestamp)}/{key}").json()
        return self._record(key, (payload.get("coins") or {}).get(key), timestamp, payload)

    def get_prices(self, items, timestamp: int):
        """Batch ``get_price`` for many ``(chain, asset)`` at ONE timestamp in a single comma-joined call
        (the endpoint accepts ``coingecko:<slug>`` and ``{chain}:{contract}`` keys side by side). Returns
        ``({coin_key: PriceRecord | None}, raw_payload)``. Keys that can't be built (no native id) are
        omitted — the caller treats those movements as a miss (no fabricated price). ``None`` per key is an
        honest gap. One HTTP call regardless of how many movements share the timestamp."""
        key_to_pair: dict[str, tuple] = {}
        for chain, asset in items:
            try:
                key_to_pair.setdefault(self.coin_key(chain, asset), (chain, asset))
            except UpstreamError:
                continue
        if not key_to_pair:
            return {}, None
        joined = ",".join(key_to_pair)
        payload = self.request(path=f"/prices/historical/{int(timestamp)}/{joined}").json()
        coins = (payload.get("coins") or {}) if isinstance(payload, dict) else {}
        return ({key: self._record(key, coins.get(key), timestamp, payload) for key in key_to_pair},
                payload)
