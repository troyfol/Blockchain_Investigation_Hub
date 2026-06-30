"""Etherscan V2 connector (phase_02 step 2; docs/connectors.md §2).

One key, many EVM chains via ``chainid``. ``get_transactions`` merges three account endpoints
(txlist/txlistinternal/tokentx) — **each its own source_query** with the raw pages hashed and
the applied bounds recorded in ``params`` (audit #10). Re-fetch is idempotent (repository
upserts on natural keys). Finality is derived from the rows' own ``confirmations`` field
(tip = max(blockNumber + confirmations)), so no extra tip call is needed.

CONFIRMED-AT-BUILD 2026-06-26 (see PROGRESS.md): base URL ``https://api.etherscan.io/v2/api``;
free tier 3 req/s + selected chains only; envelope ``{status,message,result}`` where
``status:"0"`` + list = no records and ``status:"0"`` + string = error.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .base import BaseHttpConnector, ConnectorError, RateLimitError, UpstreamError, filter_supported_bounds
from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Address, SourceQuery, Transfer
from ..normalization.canonical import canonical_address
from ..normalization.reconcile import assign_occurrences
from ..normalization.etherscan_adapter import (
    adapt_balance,
    adapt_tokentx,
    adapt_txlist,
    adapt_txlistinternal,
)
from ..provenance.atomic import write_with_provenance

CHAIN_TO_CHAINID = {"ethereum": 1, "arbitrum": 42161, "optimism": 10, "base": 8453, "polygon": 137}
DEFAULT_OFFSET = 1000  # conservative: free-tier max records/request drops 10000->1000 on 2026-07-01
# Bounds this connector honors. Anything else is refused loudly (never silently ignored).
SUPPORTED_BOUNDS = {"block_range", "max_pages", "min_value", "direction", "contractaddress",
                    "time_window", "top_n_counterparties"}

_TX_ADAPTERS = {
    "txlist": adapt_txlist,
    "txlistinternal": adapt_txlistinternal,
    "tokentx": adapt_tokentx,
}


class EtherscanConnector(BaseHttpConnector):
    name = "etherscan"

    def __init__(self, *, api_key: str, settings, base_url: str | None = None,
                 page_size: int | None = None, **kw):
        super().__init__(base_url=base_url or settings.etherscan_base_url, **kw)
        self.api_key = api_key
        self.settings = settings
        self.page_size = page_size or DEFAULT_OFFSET

    def capabilities(self) -> set[str]:
        return {"get_transactions", "get_balance"}

    def supported_chains(self) -> set[str]:
        return set(CHAIN_TO_CHAINID)

    def _chainid(self, chain: str) -> int:
        try:
            return CHAIN_TO_CHAINID[chain.lower()]
        except KeyError:
            raise UpstreamError(f"unsupported EVM chain {chain!r}")

    # --- envelope / pagination -----------------------------------------------------------

    def _envelope_rows(self, payload: dict) -> list[dict]:
        """Return the result rows, distinguishing no-records from a real error."""
        status = str(payload.get("status"))
        result = payload.get("result")
        if status == "1":
            return result if isinstance(result, list) else []
        if isinstance(result, list):
            return []  # status "0" + list = "no records found"
        message = str(payload.get("message", ""))
        detail = f"{message}: {result}"
        if "rate limit" in str(result).lower() or "rate limit" in message.lower():
            raise RateLimitError(detail)
        raise UpstreamError(detail)

    def _block_range(self, bounds: dict) -> tuple[int, int]:
        br = bounds.get("block_range")
        if br:
            return int(br[0]), int(br[1])
        return 0, 999999999

    def _collect(self, chain: str, address: str, action: str, bounds: dict):
        """Fetch all pages of one endpoint (honoring max_pages). Returns (payloads, rows, partial)."""
        max_pages = bounds.get("max_pages")
        start, end = self._block_range(bounds)
        payloads: list = []
        rows: list[dict] = []
        partial = False
        page = 1
        while True:
            params = {
                "chainid": self._chainid(chain), "module": "account", "action": action,
                "address": address, "startblock": start, "endblock": end,
                "page": page, "offset": self.page_size, "sort": "asc", "apikey": self.api_key,
            }
            if action == "tokentx" and bounds.get("contractaddress"):
                params["contractaddress"] = bounds["contractaddress"]
            payload = self.get(params).json()
            payloads.append(payload)
            page_rows = self._envelope_rows(payload)
            rows.extend(page_rows)
            if len(page_rows) < self.page_size:
                break
            page += 1
            if max_pages is not None and page > max_pages:
                partial = True
                break
        return payloads, self._post_filter(rows, bounds, address, action), partial

    def _post_filter(self, rows: list[dict], bounds: dict, address: str, action: str) -> list[dict]:
        direction = bounds.get("direction")
        if direction == "in":
            rows = [r for r in rows if (r.get("to") or "").lower() == address.lower()]
        elif direction == "out":
            rows = [r for r in rows if (r.get("from") or "").lower() == address.lower()]
        min_value = bounds.get("min_value")
        if min_value is not None and action in ("txlist", "txlistinternal"):
            mv = int(min_value)
            rows = [r for r in rows if int(r.get("value", "0")) >= mv]
        return rows

    @staticmethod
    def _derive_tip(rows: list[dict]) -> int | None:
        tips = [int(r["blockNumber"]) + int(r["confirmations"])
                for r in rows if str(r.get("confirmations", "")).strip()]
        return max(tips) if tips else None

    def _params(self, *, address: str, chain: str, action: str, bounds: dict) -> dict:
        return {"address": address, "chainid": self._chainid(chain), "action": action,
                "bounds": dict(bounds) if bounds else "default"}

    # --- time_window + top_n_counterparties bounds (Phase-2 carryover) --------------------

    @staticmethod
    def _iso_to_unix(iso: str) -> int:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())

    def _block_by_time(self, chain: str, ts_unix: int, closest: str) -> int:
        """Resolve a unix timestamp to a block number via Etherscan getblocknobytime."""
        payload = self.get({"chainid": self._chainid(chain), "module": "block",
                            "action": "getblocknobytime", "timestamp": ts_unix,
                            "closest": closest, "apikey": self.api_key}).json()
        if str(payload.get("status")) == "1":
            return int(payload["result"])
        raise UpstreamError(f"getblocknobytime: {payload.get('message')}: {payload.get('result')}")

    def _resolve_time_window(self, chain: str, bounds: dict) -> dict:
        """Resolve `time_window` to a block range (intersected with any explicit `block_range`)."""
        tw = bounds.get("time_window")
        if not tw:
            return bounds
        start_block = self._block_by_time(chain, self._iso_to_unix(tw[0]), "after")
        end_block = self._block_by_time(chain, self._iso_to_unix(tw[1]), "before")
        eff = dict(bounds)
        br = eff.get("block_range")
        eff["block_range"] = ((max(int(br[0]), start_block), min(int(br[1]), end_block))
                              if br else (start_block, end_block))
        return eff

    @staticmethod
    def _counterparty(tr, queried: str):
        if tr.to_address == queried:
            return tr.from_address
        if tr.from_address == queried:
            return tr.to_address
        return tr.to_address

    def _filter_top_n_counterparties(self, parsed_by: dict, queried: str, n: int) -> None:
        """Keep only transfers whose counterparty is among the top-N by transfer count."""
        counts: Counter = Counter()
        for parsed in parsed_by.values():
            for pt in parsed:
                for tr in pt.transfers:
                    cp = self._counterparty(tr, queried)
                    if cp is not None:
                        counts[cp] += 1
        top = {cp for cp, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]}
        for parsed in parsed_by.values():
            for pt in parsed:
                pt.transfers = [tr for tr in pt.transfers
                                if self._counterparty(tr, queried) is None
                                or self._counterparty(tr, queried) in top]

    # --- writers -------------------------------------------------------------------------

    def _addr_id(self, conn, sqid, chain, canonical):
        if not canonical:
            return None
        return repo.upsert_address(conn, Address(chain=chain, address_display=canonical), sqid)

    def _write_parsed(self, conn, sqid, parsed) -> dict:
        assign_occurrences(parsed)  # content+occurrence dedup key (decision (c)) before the DB write
        n_tx = n_tr = 0
        for pt in parsed:
            tx_id = repo.upsert_transaction(conn, pt.transaction, sqid)
            n_tx += 1
            for tr in pt.transfers:
                from_id = self._addr_id(conn, sqid, tr.chain, tr.from_address)
                to_id = self._addr_id(conn, sqid, tr.chain, tr.to_address)
                asset_id = repo.upsert_asset(conn, tr.asset, sqid)
                repo.upsert_transfer(conn, Transfer(
                    transaction_id=tx_id, chain=tr.chain, from_address_id=from_id, to_address_id=to_id,
                    asset_id=asset_id, amount=tr.amount, transfer_type=tr.transfer_type,
                    position=tr.position, occurrence=tr.occurrence), sqid)
                n_tr += 1
        return {"transactions": n_tx, "transfers": n_tr}

    # --- capabilities --------------------------------------------------------------------

    def get_transactions(self, conn, chain: str, address: str, bounds: dict | None = None) -> dict:
        """Ingest an EVM address: txlist + txlistinternal + tokentx, each its own source_query.

        Re-fetch is idempotent **for a given bounds set** (upsert on natural keys). Note: bounds
        limit *acquisition*, not the fact store — narrowing bounds on a later pull does NOT delete
        facts a prior wider pull already ingested (facts are append-only; each pull records its
        bounds in params for reproducibility).
        """
        # TOLERANT bounds (P8.6): apply the bounds Etherscan supports and SKIP any it doesn't — recorded
        # in params + the query marked partial — instead of hard-erroring (the chain-agnostic depth control
        # may send an unknown bound). Etherscan already supports the full EVM bound set, so this is a
        # forward-compat safety net rather than the BTC-style mismatch.
        applied, skipped = filter_supported_bounds(bounds, SUPPORTED_BOUNDS)
        bounds = applied
        address = canonical_address(chain, address)
        threshold = self.settings.finality_threshold(chain)

        # time_window is resolved to a block range for the actual fetch; the ORIGINAL bounds are
        # what we record in params (reproducibility — audit #10).
        effective = self._resolve_time_window(chain, bounds)
        br = effective.get("block_range")
        if br and int(br[0]) > int(br[1]):
            raise ConnectorError(f"inverted block range: start {br[0]} > end {br[1]}")
        collected = {a: self._collect(chain, address, a, effective)
                     for a in ("txlist", "txlistinternal", "tokentx")}
        tip = self._derive_tip(collected["txlist"][1] + collected["tokentx"][1])
        parsed_by = {a: _TX_ADAPTERS[a](collected[a][1], chain=chain, tip_height=tip, threshold=threshold)
                     for a in ("txlist", "txlistinternal", "tokentx")}

        top_n = bounds.get("top_n_counterparties")
        if top_n is not None:
            self._filter_top_n_counterparties(parsed_by, address, int(top_n))

        summary = {}
        for action in ("txlist", "txlistinternal", "tokentx"):  # txlist first: authoritative tx fields
            payloads, rows, partial = collected[action]
            parsed = parsed_by[action]
            now = utc_now_iso()
            params = self._params(address=address, chain=chain, action=action, bounds=bounds)
            if skipped:
                params["skipped_bounds"] = skipped  # bounds we couldn't apply (recorded; marks partial)
            sq = SourceQuery(
                connector=self.name, capability="get_transactions", endpoint=action,
                params=params, requested_at=now, completed_at=now,
                status="partial" if (partial or skipped) else "ok",
                result_summary=f"{len(rows)} rows, {len(parsed)} txs",
            )
            _, res = write_with_provenance(
                conn, sq, lambda c, sqid, p=parsed: self._write_parsed(c, sqid, p),
                raw_response=payloads)
            summary[action] = res
        return summary

    def get_balance(self, conn, chain: str, address: str) -> str:
        address = canonical_address(chain, address)
        params = {"chainid": self._chainid(chain), "module": "account", "action": "balance",
                  "address": address, "tag": "latest", "apikey": self.api_key}
        payload = self.get(params).json()
        if str(payload.get("status")) != "1":
            raise UpstreamError(f"balance: {payload.get('message')}: {payload.get('result')}")
        now = utc_now_iso()
        canonical, snap = adapt_balance(payload["result"], chain=chain, address=address, as_of_ts=now)
        sq = SourceQuery(connector=self.name, capability="get_balance", endpoint="balance",
                         params={"address": canonical, "chainid": self._chainid(chain),
                                 "bounds": "default"},
                         requested_at=now, completed_at=now, status="ok")

        def write(c, sqid):
            snap.address_id = repo.upsert_address(c, Address(chain=chain, address_display=canonical), sqid)
            return repo.insert_balance_snapshot(c, snap, sqid)

        _, bid = write_with_provenance(conn, sq, write, raw_response=payload)
        return bid
