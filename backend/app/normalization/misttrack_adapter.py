"""MisTrack API -> canonical risk/attribution mapping (pure; no HTTP, no DB).

See `docs/findings/misttrack_reconciliation.md`. MisTrack is an **API** (not the CSV the retired importer
assumed): two endpoints — `/v2/risk_score` (score **3-100**, `risk_level`, nested `risk_detail[]`) and
`/v1/address_labels` (`label_list` + `label_type`). The risk and the attribution live on DIFFERENT
endpoints. Risk is per **blockchain** (all coins on a chain return the same result).

`TODO: confirm` the live envelope + exact field names against the V2/V3 OpenAPI before relying on this
in production (no key at build to record a response); the field reads are defensive (`.get`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical chain -> MisTrack `coin` query value. Risk is per-blockchain; `coin` selects the chain.
# TODO: confirm the full coin list (MisTrack also has token-specific coins like USDT-TRC20).
CHAIN_TO_COIN: dict[str, str] = {
    "ethereum": "ETH", "bitcoin": "BTC", "bsc": "BNB", "tron": "TRX", "polygon": "MATIC",
}

RISK_SCORE_SCALE = "3-100"  # NOT 0-100 — MisTrack scores run 3..100 (reconciliation note)


@dataclass
class MisTrackRiskDetail:
    signal: str                # the risk_type of one nested risk_detail[] entry (e.g. 'mixer','sanctions')
    score: float | None        # `percent` exposure share as the sub-signal score (TODO: confirm vs volume)
    score_scale: str = "percent"  # NOT the 3-100 risk score — this is an exposure % (TODO: confirm)


@dataclass
class MisTrackRisk:
    score: float | None
    score_scale: str
    category: str | None       # the dominant risk_type (by percent); full breakdown kept in rationale
    rationale: str | None      # risk_level + detail_list + the raw risk_detail breakdown
    details: list[MisTrackRiskDetail] = field(default_factory=list)  # FN-15: structured per-sub-signal rows


@dataclass
class MisTrackLabel:
    label: str
    category: str | None       # label_type (exchange/defi/mixer/nft/…)
    note: str | None           # secondary wallet-type tags, if any


def coin_for(chain: str) -> str | None:
    return CHAIN_TO_COIN.get(chain.lower())


def adapt_risk(data: dict) -> MisTrackRisk | None:
    """Map a `/v2/risk_score` `data` object. Returns None if there's no usable score. The nested
    `risk_detail[]` breakdown is preserved verbatim in `rationale` (and fully in the source_query's
    raw_response) — never collapsed away."""
    if not isinstance(data, dict):
        return None
    score = data.get("score")
    risk_level = data.get("risk_level")
    detail_list = data.get("detail_list") or []
    risk_detail = data.get("risk_detail") or []
    hacking = data.get("hacking_event")

    parts: list[str] = []
    if risk_level:
        parts.append(str(risk_level))
    if detail_list:
        parts.append("; ".join(str(d) for d in detail_list))
    if hacking:
        parts.append(f"incident: {hacking}")
    for d in risk_detail:  # raw nested breakdown (entity/risk_type/exposure/hop/volume/percent)
        if isinstance(d, dict):
            parts.append(
                f"{d.get('risk_type')}:{d.get('entity')} "
                f"({d.get('exposure_type')}, hop {d.get('hop_num')}, ${d.get('volume')}, {d.get('percent')}%)")
    rationale = " | ".join(p for p in parts if p) or None

    # Primary category = the highest-percent risk_detail's risk_type (representative, NOT a collapse —
    # the full breakdown survives in rationale + raw_response). None when there's no breakdown.
    primary = None
    detail_dicts = [d for d in risk_detail if isinstance(d, dict)]
    if detail_dicts:
        primary = max(detail_dicts, key=lambda d: d.get("percent") or 0)
    category = primary.get("risk_type") if isinstance(primary, dict) else None

    # FN-15: each nested risk_detail[] entry -> a structured sub-signal row (signal=risk_type, score=percent).
    # Scaffold — gated on a key for LIVE use; the mapping is synthetic-testable now (field names TODO: confirm).
    details = []
    for d in detail_dicts:
        rt = d.get("risk_type")
        if not rt:
            continue
        pct = d.get("percent")
        details.append(MisTrackRiskDetail(
            signal=str(rt), score=float(pct) if isinstance(pct, (int, float)) else None))

    return MisTrackRisk(
        score=float(score) if isinstance(score, (int, float)) else None,
        score_scale=RISK_SCORE_SCALE, category=category, rationale=rationale, details=details)


def adapt_labels(data: dict) -> list[MisTrackLabel]:
    """Map a `/v1/address_labels` `data` object: `label_list` (entity + wallet tags) -> one attribution
    (entity = primary label, remaining tags -> note); `label_type` -> category. Empty if no labels."""
    if not isinstance(data, dict):
        return []
    label_list = [str(x).strip() for x in (data.get("label_list") or []) if str(x).strip()]
    if not label_list:
        return []
    label_type = (data.get("label_type") or "").strip() or None
    primary, *tags = label_list
    note = ("tags: " + ", ".join(tags)) if tags else None
    return [MisTrackLabel(label=primary, category=label_type, note=note)]
