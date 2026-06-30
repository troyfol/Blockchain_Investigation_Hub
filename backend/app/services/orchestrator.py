"""Acquisition orchestrator (phase_02 step 2 — dispatch on capability + chain).

Routes a request to a connector that provides the capability AND handles the chain. Chain-aware
dispatch matters once more than one connector is registered (Etherscan = EVM chains, Esplora =
bitcoin): capability alone is ambiguous because both provide ``get_transactions``/``get_balance``.
Connectors own provenance (each call writes its own ``source_query``), so this stays a thin router.
"""

from __future__ import annotations


class NoConnectorError(Exception):
    pass


class Orchestrator:
    def __init__(self, connectors: list):
        self.connectors = list(connectors)

    def _for(self, capability: str, chain: str):
        chain_l = chain.lower()
        for c in self.connectors:
            if capability in c.capabilities() and chain_l in {x.lower() for x in c.supported_chains()}:
                return c
        raise NoConnectorError(
            f"no connector provides capability {capability!r} for chain {chain!r}")

    def get_transactions(self, conn, chain: str, address: str, bounds: dict | None = None):
        return self._for("get_transactions", chain).get_transactions(conn, chain, address, bounds)

    def get_balance(self, conn, chain: str, address: str):
        return self._for("get_balance", chain).get_balance(conn, chain, address)

    def get_transfers(self, conn, chain: str, identifier: str):
        return self._for("get_transfers", chain).get_transfers(conn, chain, identifier)
