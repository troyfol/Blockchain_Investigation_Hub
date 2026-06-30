"""GraphSense TagPack -> canonical attribution / risk / entity mapping (pure; no HTTP, no DB).

See `docs/findings/graphsense_tagpack_reconciliation.md`. A TagPack is a YAML file: a **header**
(`title`/`creator` + any body field abstracted up to be inherited by all tags) plus a **body** list
of `tags`. `!include` resolution (reading a sibling header file) is I/O and lives in the connector;
this adapter takes the already-resolved `dict` and does the pure mapping.

Mapping (findings §"Mapping GraphSense → BIH"):
  - `address` + `currency` -> `Address(chain, address_display)`. **currency→chain filter**: BIH v1 is
    BTC + EVM only, so only confirmed currencies (`BTC`→bitcoin, `ETH`→ethereum) are mapped; everything
    else (BCH/LTC/ZEC/XRP/…) is **skipped + reported** so it never reaches `canonical_address` (which
    would raise) — mirrors the Arkham `tron` skip. A malformed address on a *supported* chain is a hard
    error (the connector raises, all-or-nothing).
  - `label` -> `Attribution.label`; `category` -> `Attribution.category` (raw taxonomy concept).
  - `confidence` is a categorical **id** (e.g. `forensic_investigation`), not a float -> looked up in
    the vendored `confidence.csv` to `level/100`; the raw id is kept in `note`. Unknown/missing id ->
    `confidence=None` (never a guessed level), id still recorded in `note`.
  - `source` (a dereferenceable backlink) + the confidence id -> `Attribution.note` (deterministic
    composite, also the per-tag idempotency discriminator — see the connector's natural-key upsert).
  - `abuse` -> also a categorical `RiskAssessment` (Phase B). `actor` / `is_cluster_definer` -> entity
    membership (Phase C). Both are parsed here so the connector capabilities only select what to write.

Identity (Invariant #7): a tag is `(address, label, source)`. The same `(address, label)` from different
TagPacks/parties is kept side-by-side, never merged (Invariant #4).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass

from ..app_paths import resource_path
from .canonical import canonical_address

# Vendored GraphSense confidence taxonomy (id -> 0..100 level) — a bundled READ-ONLY resource (P7):
# _MEIPASS/... when frozen, else the in-repo data/ dir in source (via resource_path). Loaded once at
# import (a constant table, not per-call I/O). Source URL + refresh TODO in the CSV header.
_CONFIDENCE_CSV = resource_path("backend/app/normalization/data/graphsense_confidence.csv")


def _load_confidence_levels() -> dict[str, int]:
    lines = [ln for ln in _CONFIDENCE_CSV.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    table: dict[str, int] = {}
    for row in csv.DictReader(lines):
        cid = (row.get("id") or "").strip()
        lvl = (row.get("level") or "").strip()
        if cid and lvl:
            table[cid] = int(lvl)
    return table


CONFIDENCE_LEVELS: dict[str, int] = _load_confidence_levels()

# currency code -> canonical chain. BIH v1 = BTC (UTXO) + EVM only (`canonical.py`). Only confirmed
# currencies are mapped; an unmapped currency is skipped+reported (never canonicalized). TODO: confirm
# the full EVM currency-code set GraphSense emits and extend this — conservatively map only what's known.
CURRENCY_TO_CHAIN: dict[str, str] = {"BTC": "bitcoin", "ETH": "ethereum"}

# Tag fields that are NOT inheritable header defaults.
_BODY_ONLY = {"tags", "header"}


@dataclass
class ParsedTag:
    chain: str
    address_display: str       # source display form; canonicalized again by repo.upsert_address
    label: str
    category: str | None
    source_backlink: str | None
    confidence: float | None   # level/100, or None for unknown/missing id
    confidence_id: str | None  # raw categorical id (kept for note + provenance)
    note: str | None           # "source: <backlink> | confidence: <id>" — also the idempotency key
    abuse: str | None          # categorical abuse type (Phase B -> risk_assessment)
    actor: str | None          # actor id reference (Phase C -> entity membership)
    is_cluster_definer: bool    # Phase C -> entity_membership.flags


@dataclass
class ParsedActor:
    external_id: str           # actor id (e.g. "internet_archive") — the tag `actor` reference key
    name: str                  # human label
    entity_type: str | None    # first category, if any


def confidence_to_float(conf_id: str | None) -> tuple[float | None, bool]:
    """(level/100, known) for a known id; (None, False) for an unknown or missing id."""
    if not conf_id:
        return None, False
    lvl = CONFIDENCE_LEVELS.get(conf_id.strip())
    if lvl is None:
        return None, False
    return lvl / 100, True


def _str(v) -> str | None:
    """Normalize a YAML scalar to a stripped non-empty str, else None."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


_FALSY_STRINGS = {"", "false", "no", "0", "off", "none", "null"}


def _truthy(v) -> bool:
    """Interpret a YAML flag as a bool. A real YAML bool passes through; a *string* flag (e.g. a
    pack that quotes ``"false"``) is parsed so ``"false"``/``"no"``/``"0"`` are False — plain
    ``bool("false")`` would be True. Anything else non-empty is truthy."""
    if isinstance(v, str):
        return v.strip().lower() not in _FALSY_STRINGS
    return bool(v)


def _header_and_body(doc: dict) -> tuple[dict, list]:
    """Split a resolved TagPack dict into (header_defaults, tags). Header fields are the top-level
    keys (minus `tags`/`header`); an explicit `header:` block (e.g. from `!include`) supplies further
    defaults, with any top-level field overriding the included block (more specific wins). A present-
    but-non-mapping `header:` (a malformed/misincluded shared header) fails loudly via ValueError —
    silently dropping it would make every header-inherited field vanish and surface as misleading
    'missing required field' / 'unsupported currency' skips downstream."""
    tags = doc.get("tags") or []
    header = {k: v for k, v in doc.items() if k not in _BODY_ONLY}
    block = doc.get("header", None)
    if block is not None:
        if not isinstance(block, dict):
            raise ValueError(f"`header:` is not a YAML mapping (got {type(block).__name__})")
        header = {**block, **header}
    return header, list(tags)


def _build_note(backlink: str | None, confidence_id: str | None) -> str | None:
    parts = []
    if backlink:
        parts.append(f"source: {backlink}")
    if confidence_id:
        parts.append(f"confidence: {confidence_id}")
    return " | ".join(parts) or None


def adapt_tagpack(doc: dict) -> tuple[list[ParsedTag], dict]:
    """Map a resolved TagPack dict to canonical ParsedTag rows. Returns ``(tags, notes)``.

    ``notes`` surfaces what was skipped/flagged so nothing is silently dropped:
      - ``skipped_unsupported``: rows on unsupported currencies (never canonicalized).
      - ``errors``: malformed tags on a *supported* chain (missing required field / bad address) — the
        connector raises on these (all-or-nothing), so a corrupt pack fails loudly, never a partial write.
      - ``unknown_confidence``/``abuse_tags``/``actor_tags``: counters for downstream phases.
    """
    notes: dict = {"tags": 0, "attributions": 0, "skipped_unsupported": [], "unknown_confidence": 0,
                   "abuse_tags": 0, "actor_tags": 0, "errors": []}
    try:
        header, body = _header_and_body(doc)
    except ValueError as exc:
        notes["errors"].append({"tag": -1, "reason": str(exc)})  # malformed header -> all-or-nothing
        return [], notes
    out: list[ParsedTag] = []

    for idx, raw in enumerate(body):
        notes["tags"] += 1
        if not isinstance(raw, dict):
            notes["errors"].append({"tag": idx, "reason": "tag is not a mapping"})
            continue
        merged = {**header, **raw}  # tag fields override inherited header defaults
        currency = (_str(merged.get("currency")) or "")
        address = _str(merged.get("address"))
        label = _str(merged.get("label"))

        # Classify by currency BEFORE touching the address (unsupported addrs never hit canonical_address).
        chain = CURRENCY_TO_CHAIN.get(currency.upper())
        if chain is None:
            notes["skipped_unsupported"].append({"currency": currency or "(missing)", "label": label})
            continue

        missing = [f for f, v in (("address", address), ("label", label)) if not v]
        if missing:
            notes["errors"].append({"tag": idx, "reason": f"missing required field(s): {missing}"})
            continue
        try:
            canonical_address(chain, address)  # validate now so a bad supported-chain addr is a clean error
        except ValueError as exc:
            notes["errors"].append({"tag": idx, "reason": str(exc)})
            continue

        conf_id = _str(merged.get("confidence"))
        confidence, known = confidence_to_float(conf_id)
        if conf_id and not known:
            notes["unknown_confidence"] += 1
        backlink = _str(merged.get("source"))
        abuse = _str(merged.get("abuse"))
        actor = _str(merged.get("actor"))
        if abuse:
            notes["abuse_tags"] += 1
        if actor:
            notes["actor_tags"] += 1

        out.append(ParsedTag(
            chain=chain, address_display=address, label=label,
            category=_str(merged.get("category")), source_backlink=backlink,
            confidence=confidence, confidence_id=conf_id, note=_build_note(backlink, conf_id),
            abuse=abuse, actor=actor, is_cluster_definer=_truthy(merged.get("is_cluster_definer"))))
        notes["attributions"] += 1

    return out, notes


def adapt_actorpack(doc: dict) -> tuple[list[ParsedActor], dict]:
    """Map an ActorPack dict (`actors:` list) to ParsedActor rows (Phase C). entity_type = first
    category. Returns ``(actors, notes)``; malformed actors (no id or no label) collect in errors."""
    notes: dict = {"actors": 0, "errors": []}
    out: list[ParsedActor] = []
    for idx, raw in enumerate(doc.get("actors") or []):
        notes["actors"] += 1
        if not isinstance(raw, dict):
            notes["errors"].append({"actor": idx, "reason": "actor is not a mapping"})
            continue
        ext_id = _str(raw.get("id"))
        name = _str(raw.get("label"))
        if not ext_id or not name:
            notes["errors"].append({"actor": idx, "reason": "actor missing required id/label"})
            continue
        cats = raw.get("categories")
        entity_type = None
        if isinstance(cats, (list, tuple)) and cats:
            entity_type = _str(cats[0])
        elif cats:
            entity_type = _str(cats)
        out.append(ParsedActor(external_id=ext_id, name=name, entity_type=entity_type))
    return out, notes
