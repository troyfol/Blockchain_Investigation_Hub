"""Connector base: capability interface, bounds, rate-limit + backoff (phase_02 step 1).

Connectors acquire data and own provenance. The HTTP base class provides a per-connector rate
limit (token-bucket-ish, free-tier default 3 req/s — confirmed 2026-06-26) and exponential
backoff with jitter on 429/5xx. Endpoint-specific parsing/pagination lives in the concrete
connector; mapping to canonical records lives in a normalization adapter (nothing downstream
knows a source's native shape, docs/overview.md §5).
"""

from __future__ import annotations

import random as _random
import time
from typing import Literal, Protocol, TypedDict, runtime_checkable

import httpx

from ..runtime import ca_bundle


class Bounds(TypedDict, total=False):
    """Address-scoped expansion bounds (decision #2). Recorded in source_query.params."""

    block_range: tuple[int, int] | None
    time_window: tuple[str, str] | None      # ISO-8601
    min_value: str | None                    # base-unit integer as text
    top_n_counterparties: int | None
    max_pages: int | None
    direction: Literal["in", "out", "both"] | None


def filter_supported_bounds(bounds: dict | None, supported: set[str]) -> tuple[dict, list[str]]:
    """Split ``bounds`` into ``(applied, skipped)``: keep the keys this connector supports, drop + report
    the rest. Connectors are TOLERANT of an unknown bound (P8.6) — they SKIP it (the caller records it in
    ``source_query.params`` and marks the query ``partial``) rather than RAISING and aborting the whole
    ingest. This matters because the UI's chain-agnostic depth control may send an EVM-only bound (e.g.
    ``top_n_counterparties``) to a Bitcoin connector; that must not hard-fail the BTC ingest."""
    b = bounds or {}
    applied = {k: v for k, v in b.items() if k in supported}
    skipped = sorted(k for k in b if k not in supported)
    return applied, skipped


class ConnectorError(Exception):
    """Base for connector failures."""


class OfflineError(ConnectorError):
    """A network fetch was attempted while OFFLINE MODE is on (P5). A ConnectorError subclass, so the
    orchestrator/expand path surfaces it as a clean error (not a 500) — ingest/expand is disabled until
    offline mode is turned off; cached data + view/report/export are unaffected."""


class RateLimitError(ConnectorError):
    """Upstream signalled a rate limit (after retries exhausted)."""


class UpstreamError(ConnectorError):
    """Upstream returned a persistent error / bad status."""


@runtime_checkable
class Connector(Protocol):
    name: str

    def capabilities(self) -> set[str]: ...


class RateLimiter:
    """Spacing rate limiter: at most one call per ``1/rate`` seconds."""

    def __init__(self, rate_per_sec: float, *, enabled: bool = True,
                 sleep=time.sleep, monotonic=time.monotonic):
        self.min_interval = (1.0 / rate_per_sec) if rate_per_sec > 0 else 0.0
        self.enabled = enabled
        self._sleep = sleep
        self._mono = monotonic
        self._last: float | None = None

    def acquire(self) -> None:
        if not self.enabled or self.min_interval <= 0:
            return
        now = self._mono()
        if self._last is not None:
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                self._sleep(wait)
                now = self._mono()
        self._last = now


class BaseHttpConnector:
    """Shared HTTP plumbing only: rate limiting + retry/backoff with jitter on 429/5xx.

    Subclasses own everything API-specific: the pagination loop (offset- or cursor-based),
    response-envelope parsing, bounds->params mapping, and writing rows via
    ``write_with_provenance``. The base deliberately knows nothing about response shapes or
    canonical records, so a UTXO connector (Esplora, Phase 3) with a different envelope and
    cursor pagination reuses ``get()`` unchanged.
    """

    name = "base"

    def __init__(self, *, base_url: str, client: httpx.Client | None = None,
                 rate_limiter: RateLimiter | None = None, max_retries: int = 4,
                 backoff_base: float = 0.5, backoff_cap: float = 8.0,
                 sleep=time.sleep, rng: _random.Random | None = None):
        self.base_url = base_url
        # verify=certifi (the bundled CA) so TLS works in a frozen app with no system cert store (P7).
        self._client = client or httpx.Client(timeout=30.0, verify=ca_bundle())
        self._owns_client = client is None
        self.rate_limiter = rate_limiter or RateLimiter(3.0)  # free-tier default
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng or _random.Random()

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        return base * (0.5 + self._rng.random() * 0.5)  # full-ish jitter

    def request(self, path: str = "", params: dict | None = None) -> httpx.Response:
        """GET ``base_url`` + ``path`` (optional query ``params``); retry 429/5xx/transport w/ backoff.

        Supports both query-param APIs (Etherscan: ``request(params=...)``) and path APIs
        (Esplora: ``request(path="/address/...")``).
        """
        # Offline mode (P5): refuse any outbound call BEFORE touching the network. Lazy import keeps the
        # connector base free of a service dependency at import time (and avoids an import cycle).
        from ..services import jobs
        from ..services.settings_store import is_offline

        if is_offline():
            raise OfflineError(
                "offline mode is on — outbound network calls are disabled; ingest/expand is unavailable "
                "until you turn offline mode off (cached data, views, reports and export still work)")
        jobs.check_cancel()  # P8.7.2 — honor a cancel before starting the next page/price call
        url = self.base_url + path
        attempt = 0
        while True:
            jobs.check_cancel()  # ...and before EACH retry, so a cancel set during a backoff sleep is
                                 # honored promptly (not after another full network round-trip) and never
                                 # mislabeled as an UpstreamError when retries exhaust.
            self.rate_limiter.acquire()
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise UpstreamError(f"transport error after {attempt} retries: {exc!r}") from exc
                jobs.note_backoff()  # transient transport error -> backing off (+ honor cancel)
                self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt >= self.max_retries:
                    raise UpstreamError(f"HTTP {resp.status_code} after {attempt} retries")
                jobs.note_backoff()  # rate-limited / upstream 5xx -> the UI shows "backing off" (+ cancel)
                self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue
            resp.raise_for_status()
            jobs.note_request()  # a successful page/price call -> progress + clear backoff + honor cancel
            return resp

    def get(self, params: dict) -> httpx.Response:
        """Back-compat alias: query-param GET against ``base_url`` (Etherscan-style)."""
        return self.request(params=params)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
