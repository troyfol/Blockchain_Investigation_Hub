"""Optional paid-connector registry (docs/findings/paid_api_integrations.md).

The single place that knows the optional PAID connectors and gates their availability. A paid source is
**available (selectable)** only when its config `*_enabled` flag is on AND its key is in the OS keyring;
otherwise it is silently absent — never blocking the free baseline (Invariant #4). `paid_status` reports
this (for `/health` + the operator); `available_fact_connectors` / `available_intel_connectors`
instantiate the available ones (the caller owns closing their httpx clients).
"""

from __future__ import annotations

from ..secrets import get_secret

# kind: 'fact' (routed by the orchestrator) | 'intel' (risk/attribution; invoked directly).
PAID_SPECS = [
    {"name": "bitquery", "keyring": "bitquery_token", "enabled_attr": "bitquery_enabled",
     "kind": "fact", "capabilities": {"get_transactions", "get_transfers"}},
    {"name": "arkham-api", "keyring": "arkham_api_key", "enabled_attr": "arkham_api_enabled",
     "kind": "intel", "capabilities": {"get_attributions", "get_risk"}},
    {"name": "misttrack-api", "keyring": "misttrack_api_key", "enabled_attr": "misttrack_enabled",
     "kind": "intel", "capabilities": {"get_risk", "get_attributions"}},
    {"name": "oklink", "keyring": "oklink_api_key", "enabled_attr": "oklink_enabled",
     "kind": "intel", "capabilities": {"get_attributions", "get_risk"}},
]


def spec_by_name(name: str) -> dict | None:
    """The PAID spec for a connector name (e.g. the in-app Settings UI resolving a key target)."""
    return next((s for s in PAID_SPECS if s["name"] == name), None)


def paid_status(settings) -> list[dict]:
    """Per paid connector: ``enabled`` + ``has_key`` (keyring) + ``available`` (both).

    ``enabled`` is the RUNTIME override the in-app Settings UI set (``settings_store``), falling back to
    the config/env default — so a toggle flips real availability for the orchestrator AND ``/health``."""
    from ..services.settings_store import paid_enabled_override

    out = []
    for s in PAID_SPECS:
        override = paid_enabled_override(s["name"])
        enabled = bool(getattr(settings, s["enabled_attr"])) if override is None else bool(override)
        has_key = bool(get_secret(s["keyring"]))
        out.append({"name": s["name"], "keyring": s["keyring"], "kind": s["kind"],
                    "capabilities": sorted(s["capabilities"]), "enabled": enabled,
                    "has_key": has_key, "available": enabled and has_key})
    return out


def _build(name: str, settings):
    if name == "bitquery":
        from .bitquery import BitqueryConnector
        return BitqueryConnector(settings=settings)
    if name == "arkham-api":
        from .arkham import ArkhamApiConnector
        return ArkhamApiConnector(settings=settings)
    if name == "misttrack-api":
        from .misttrack import MisTrackConnector
        return MisTrackConnector(settings=settings)
    if name == "oklink":
        from .oklink import OkLinkConnector
        return OkLinkConnector(settings=settings)
    raise KeyError(name)


def _available(settings, kind: str) -> list:
    return [_build(s["name"], settings) for s in paid_status(settings)
            if s["available"] and s["kind"] == kind]


def available_fact_connectors(settings) -> list:
    """Enabled+keyed PAID FACT connectors (e.g. Bitquery) for the orchestrator. Caller closes them."""
    return _available(settings, "fact")


def available_intel_connectors(settings) -> list:
    """Enabled+keyed PAID intel (risk/attribution) connectors. Caller closes them."""
    return _available(settings, "intel")
