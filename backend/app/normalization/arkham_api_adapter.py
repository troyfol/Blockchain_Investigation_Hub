"""Arkham API -> canonical attribution/entity/risk mapping (pure; no HTTP, no DB).

Path B — the real Arkham *attribution* source (the UI transfer export, `imports/arkham.py`, carries no
attribution). CONFIRMED 2026-06-28 (docs/findings/paid_api_integrations.md §3). Arkham is "entity-first,
confidence-scored": a **confirmed** `arkhamEntity` and a **probabilistic** `predictedEntity` must NEVER
be collapsed — they are written as separate entities/memberships/attributions at different confidence
(Invariant #4). `userEntity`/`userLabel` are the caller's OWN private labels → ignored.

`GET /intelligence/address/{address}` → Address schema:
  - `arkhamEntity` (Entity)   → confirmed `Entity` + `EntityMembership` (high confidence).
  - `predictedEntity` (Entity)→ predicted `Entity` + `EntityMembership` + `Attribution` (LOW confidence).
  - `arkhamLabel` (Label)     → `Attribution.label` (confirmed).
  - `depositServiceID`        → deposit-address → service `Attribution`.
  - Entity = {id, name, type, service, …}; Label = {name}.

`GET /risk/address/{address}` → RiskScoreResponse → `RiskAssessment(score=max_score, score_scale='0-100',
category=greatest_risk_category, rationale=risk_level + the per-category breakdown)`; the breakdown is
kept **raw** (rationale + the source_query raw_response), never collapsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONFIRMED_CONFIDENCE = 0.95   # arkhamEntity / arkhamLabel
PREDICTED_CONFIDENCE = 0.50   # predictedEntity — deliberately lower; never merged into confirmed

# Per-category risk fields on RiskScoreResponse — stored RAW in rationale (don't collapse, Inv #4).
RISK_CATEGORY_FIELDS = [
    "hacker_score", "mixer_score", "sanctions_score", "ransomware_score", "scam_score", "ponzi_score",
    "darkweb_score", "gambling_score", "privacy_score", "non_kyc_service_score",
    "mixed_kyc_service_score", "token_blacklist_score", "sanctioned_1hop_score",
]


@dataclass
class ArkEntity:
    external_id: str | None
    name: str
    entity_type: str | None
    confidence: float
    method: str                 # 'arkham-entity' (confirmed) | 'arkham-predicted'


@dataclass
class ArkAttribution:
    label: str
    category: str | None
    confidence: float | None
    note: str | None


@dataclass
class ArkAddressPlan:
    entities: list[ArkEntity] = field(default_factory=list)
    attributions: list[ArkAttribution] = field(default_factory=list)


@dataclass
class ArkRiskDetail:
    signal: str          # per-category key, e.g. 'mixer','hacker','sanctions' (the '_score' suffix stripped)
    score: float         # the category's numeric score (same 0-100 scale as the headline)


@dataclass
class ArkRisk:
    score: float | None
    score_scale: str
    category: str | None
    rationale: str | None
    details: list[ArkRiskDetail] = field(default_factory=list)  # FN-15: the per-category breakdown, structured


def entity_key(e: ArkEntity) -> str:
    """Stable external id for resolving the entity. When Arkham supplies no `id`, the fallback EMBEDS the
    method ('arkham-entity' vs 'arkham-predicted'), so a confirmed and a predicted entity with the SAME
    name never collapse onto one entity row (Invariant #4)."""
    return e.external_id or f"arkham:{e.name}:{e.method}"


def adapt_address(payload: dict) -> ArkAddressPlan:
    """Map the Address intelligence payload. Confirmed vs predicted entities are kept SEPARATE
    (different confidence + membership method), never collapsed (Invariant #4)."""
    plan = ArkAddressPlan()
    if not isinstance(payload, dict):
        return plan

    ent = payload.get("arkhamEntity")
    if isinstance(ent, dict) and ent.get("name"):
        plan.entities.append(ArkEntity(
            external_id=ent.get("id"), name=str(ent["name"]), entity_type=ent.get("type"),
            confidence=CONFIRMED_CONFIDENCE, method="arkham-entity"))

    pred = payload.get("predictedEntity")
    if isinstance(pred, dict) and pred.get("name"):
        # Probabilistic — a SEPARATE entity/membership AND a separate, lower-confidence attribution.
        plan.entities.append(ArkEntity(
            external_id=pred.get("id"), name=str(pred["name"]), entity_type=pred.get("type"),
            confidence=PREDICTED_CONFIDENCE, method="arkham-predicted"))
        plan.attributions.append(ArkAttribution(
            label=str(pred["name"]), category=pred.get("type"), confidence=PREDICTED_CONFIDENCE,
            note="arkham predicted entity (probabilistic)"))

    lab = payload.get("arkhamLabel")
    if isinstance(lab, dict) and lab.get("name"):
        plan.attributions.append(ArkAttribution(
            label=str(lab["name"]), category=None, confidence=CONFIRMED_CONFIDENCE, note="arkham label"))

    dep = payload.get("depositServiceID")
    if isinstance(dep, str) and dep.strip():
        plan.attributions.append(ArkAttribution(
            label=dep.strip(), category="deposit_service", confidence=None,
            note="deposit address -> service"))

    return plan


def adapt_risk(payload: dict) -> ArkRisk | None:
    """Map RiskScoreResponse → ArkRisk. Numeric `max_score` (0-100), category = `greatest_risk_category`,
    and the per-category breakdown preserved verbatim in `rationale`."""
    if not isinstance(payload, dict):
        return None
    max_score = payload.get("max_score")
    risk_level = payload.get("risk_level")
    greatest = payload.get("greatest_risk_category")

    parts: list[str] = []
    if risk_level:
        parts.append(str(risk_level))
    breakdown = [f"{f[:-6]}={payload.get(f)}" for f in RISK_CATEGORY_FIELDS if payload.get(f)]
    if breakdown:
        parts.append("; ".join(breakdown))
    extras = [f"{f}={payload.get(f)}" for f in ("hop_distance", "is_seed") if payload.get(f) is not None]
    if extras:
        parts.append("; ".join(extras))
    rationale = " | ".join(parts) or None

    # FN-15: the same per-category scores, promoted to structured sub-signals (each becomes a first-class
    # `risk_detail` row). Parity with the rationale breakdown: a zero/absent category is omitted (truthy
    # numeric only). The rationale summary is KEPT alongside (back-compat) — the rows are the queryable form.
    details = [ArkRiskDetail(signal=f[:-6], score=float(payload[f]))
               for f in RISK_CATEGORY_FIELDS
               if isinstance(payload.get(f), (int, float)) and payload.get(f)]

    return ArkRisk(
        score=float(max_score) if isinstance(max_score, (int, float)) else None,
        score_scale="0-100", category=greatest, rationale=rationale, details=details)
