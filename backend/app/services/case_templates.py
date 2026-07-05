"""Case templates (P26/FN-22) — declarative presets for a NEW case.

A template pre-populates a fresh case's investigative framing so an investigator starting a common
scenario (e.g. sanctions tracing) needn't hand-configure the same things every time. It pre-seeds ONLY
settings/metadata:
  - ``description`` → the case's own ``case_meta.description`` (a per-case methodology stub);
  - ``connectors`` → the app-wide paid-connector enables (settings_store) — a template is the user's stated
    intent for the scenario, so a "sanctions-tracing" template enabling chainalysis is expected;
  - ``default_bounds`` → returned to the UI as a first-ingest hint (not persisted).

It **never fabricates facts**: no address, transaction, transfer, or claim is created — data still enters
ONLY via a real fetch/import (Invariants #1/#3). Templates are DECLARATIVE — adding one is a dict entry in
this registry, not code (acceptance #2). A from-scratch case (no template) is entirely unaffected.
"""

from __future__ import annotations

# Each entry: id (stable key), name (UI label), description (methodology stub → case_meta.description),
# connectors (paid connectors to enable app-wide), default_bounds (first-ingest hint returned to the UI).
CASE_TEMPLATES: list[dict] = [
    {
        "id": "sanctions-tracing",
        "name": "Sanctions tracing",
        "description": (
            "Methodology: trace value flows to and from OFAC-sanctioned addresses. Screen every "
            "counterparty against the sanctions list; record attributions side-by-side with their source "
            "and never merge disagreeing sources. Value movements at time-of-transaction."),
        "connectors": ["chainalysis"],
        "default_bounds": {"max_pages": 5, "top_n_counterparties": 50},
    },
    {
        "id": "exchange-attribution",
        "name": "Exchange attribution",
        "description": (
            "Methodology: identify the exchange and service endpoints a set of addresses interact with. "
            "Treat attribution-source labels (Arkham/GraphSense) as CLAIMS with provenance, never as "
            "facts; corroborate across sources before asserting an entity."),
        "connectors": ["arkham"],
        "default_bounds": {"max_pages": 10, "top_n_counterparties": 100},
    },
    {
        "id": "ransomware-tracing",
        "name": "Ransomware tracing",
        "description": (
            "Methodology: follow ransom payments from the victim's payment address through mixers and "
            "bridges to cash-out points. Record each hop as a trace with an explicit basis (FIFO / "
            "investigator); a cross-chain bridge crossing is a manual, labeled claim, never synthesized."),
        "connectors": [],
        "default_bounds": {"max_pages": 10},
    },
]

_BY_ID = {t["id"]: t for t in CASE_TEMPLATES}


def list_templates() -> list[dict]:
    """The templates for the picker UI — each a copy of ``{id, name, description, connectors, default_bounds}``."""
    return [dict(t) for t in CASE_TEMPLATES]


def get_template(template_id: str) -> dict | None:
    """The template with this id (a copy), or ``None`` if unknown (a template is optional; an unknown id is
    rejected by the caller). Returns a copy so a caller can't mutate the registry."""
    t = _BY_ID.get(template_id)
    return dict(t) if t else None
