"""OFAC SDN list -> canonical sanctions-risk mapping (pure; no HTTP, no DB).

See `docs/findings/ofac_sanctions_reconciliation.md`. OFAC lists crypto addresses on SDN entries as
**"Digital Currency Address - <TICKER>"** ids, so each carries the **address**, the **entity name**,
and the **sanctions program(s)** — rich provenance for a categorical sanctions-risk claim.

**XML format choice (TODO: confirm — deliberate §6 decision).** The findings cite `sdn_advanced.xml`,
but that file's entity-name/program data sits behind reference-value-ID *indirection* whose exact
nesting can't be confirmed offline — modelling it (and a fixture) from a guess would make a test pass
circularly without validating reality. This adapter parses the **standard `sdn.xml`** instead: it
carries the identical digital-currency data (`<sdnEntry>` with `<idList><id>` `idType`/`idNumber`,
`<lastName>`/`<firstName>`, `<programList>`) in a stable, long-documented structure. An
`sdn_advanced.xml` adapter (resolving its FeatureType/SanctionsMeasure reference sets) is a follow-up
if the operator's only available file is the advanced one; the 0xB10C nightly lists are the documented
addresses-only fast-path either way.

Mapping (findings §"Mapping → BIH"):
  - sanctioned address + `<TICKER>` -> `Address(chain, address_display)` via `TICKER_TO_CHAIN`.
  - the fact of being listed -> categorical `RiskAssessment(category='sanctioned', score=None,
    score_scale=None)` — never a numeric score.
  - entity name + program(s) -> `rationale` = ``OFAC SDN: <entity> (<program>)``.
Unsupported tickers (XMR/LTC/ZEC/…) are **skipped + reported** so they never reach `canonical_address`
(mirrors the Arkham tron / GraphSense unsupported-currency skip). A malformed address on a *supported*
chain is a hard error (the connector raises, all-or-nothing).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .canonical import canonical_address

DCA_PREFIX = "Digital Currency Address - "

# Ticker -> canonical chain. BIH v1 = BTC + EVM only. ERC-20 tickers are *ethereum* addresses (not a
# distinct chain). Any ticker NOT here is skipped+reported (never canonicalized) — see KNOWN_UNSUPPORTED
# for the documented non-BTC/EVM assets; an unrecognized new ticker is treated the same (conservative).
TICKER_TO_CHAIN: dict[str, str] = {
    "XBT": "bitcoin",
    "ETH": "ethereum",
    "ARB": "arbitrum",
    "BSC": "bsc",
    "USDC": "ethereum",   # ERC-20 -> ethereum
    "USDT": "ethereum",   # ERC-20 -> ethereum
}

# Documented non-BTC/EVM assets OFAC lists that BIH v1 cannot canonicalize (skip + report). Not used as
# a gate (anything unmapped is skipped); kept for clarity / reporting. (ETC is EVM-shaped but a distinct
# chain we don't model — skipped unless explicitly added.)
KNOWN_UNSUPPORTED = frozenset(
    {"XMR", "LTC", "ZEC", "DASH", "BTG", "BSV", "BCH", "XVG", "XRP", "TRX", "SOL", "ETC"})


@dataclass
class ParsedSanction:
    chain: str
    ticker: str
    address_display: str
    address_canonical: str        # canonical form (validated) — used for the delisting diff
    entity_name: str
    programs: list[str] = field(default_factory=list)
    rationale: str = ""
    sdn_uid: str | None = None
    sdn_type: str | None = None


def _ln(tag: str) -> str:
    """Local name of a possibly namespaced tag (OFAC's namespace evolves; match by local name)."""
    return tag.rsplit("}", 1)[-1]


def _children(elem, name: str) -> list:
    return [c for c in (elem if elem is not None else []) if _ln(c.tag) == name]


def _child(elem, name: str):
    for c in (elem if elem is not None else []):
        if _ln(c.tag) == name:
            return c
    return None


def _text(elem, name: str) -> str | None:
    c = _child(elem, name)
    if c is not None and c.text and c.text.strip():
        return c.text.strip()
    return None


def _first_descendant_text(root, name: str) -> str | None:
    for el in root.iter():
        if _ln(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return None


def _entity_name(entry, sdn_type: str | None) -> str:
    last = _text(entry, "lastName")
    first = _text(entry, "firstName")
    # The "LAST, First" comma form is the OFAC *individual* convention. For an Entity, the full org name
    # is in lastName (firstName normally empty) — only join with a comma for Individuals, so a stray
    # firstName on an Entity never mangles the org name (e.g. 'ACME, Inc').
    if last and first and (sdn_type or "").strip().lower() == "individual":
        return f"{last}, {first}"
    return last or first or "(unknown)"


def _programs(entry) -> list[str]:
    pl = _child(entry, "programList")
    return [p.text.strip() for p in _children(pl, "program") if p.text and p.text.strip()]


def adapt_sdn_xml(xml_bytes: bytes) -> tuple[list[ParsedSanction], dict]:
    """Parse OFAC `sdn.xml` bytes -> (sanctions, notes).

    ``notes`` surfaces what was skipped/flagged so nothing is silently dropped:
      - ``skipped_unsupported``: digital-currency ids on non-BTC/EVM tickers (never canonicalized).
      - ``errors``: a malformed address on a *supported* chain / unparseable XML -> the connector raises
        (all-or-nothing). ``publish_date`` carries the SDN publication date for per-fetch provenance.
    """
    notes: dict = {"entries": 0, "digital_currency_ids": 0, "sanctions": 0,
                   "skipped_unsupported": [], "errors": [], "publish_date": None}

    # Defense-in-depth: OFAC SDN has no DOCTYPE; reject one to foreclose entity-expansion ("billion
    # laughs") on an untrusted local file (Invariant #1 — structured import, parsed defensively).
    if b"<!DOCTYPE" in xml_bytes:
        notes["errors"].append({"entry": None, "reason": "XML declares a DOCTYPE (rejected)"})
        return [], notes
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        notes["errors"].append({"entry": None, "reason": f"XML parse error: {exc}"})
        return [], notes

    notes["publish_date"] = _first_descendant_text(root, "Publish_Date")
    out: list[ParsedSanction] = []

    for entry in _children(root, "sdnEntry"):
        notes["entries"] += 1
        uid = _text(entry, "uid")
        sdn_type = _text(entry, "sdnType")
        name = _entity_name(entry, sdn_type)
        programs = _programs(entry)
        idlist = _child(entry, "idList")
        for idel in _children(idlist, "id"):
            idtype = _text(idel, "idType") or ""
            if not idtype.startswith(DCA_PREFIX):
                continue  # non-crypto id (email, passport, …) — ignore
            notes["digital_currency_ids"] += 1
            ticker = idtype[len(DCA_PREFIX):].strip().upper()
            address = _text(idel, "idNumber")

            chain = TICKER_TO_CHAIN.get(ticker)
            if chain is None:
                notes["skipped_unsupported"].append({"ticker": ticker, "address": address})
                continue
            if not address:
                notes["errors"].append({"entry": uid, "reason": f"digital-currency id missing address (ticker {ticker})"})
                continue
            try:
                canonical = canonical_address(chain, address)
            except ValueError as exc:
                notes["errors"].append({"entry": uid, "reason": str(exc)})
                continue

            rationale = f"OFAC SDN: {name}"
            if programs:
                rationale += f" ({', '.join(programs)})"
            out.append(ParsedSanction(
                chain=chain, ticker=ticker, address_display=address, address_canonical=canonical,
                entity_name=name, programs=programs, rationale=rationale, sdn_uid=uid, sdn_type=sdn_type))
            notes["sanctions"] += 1

    return out, notes
