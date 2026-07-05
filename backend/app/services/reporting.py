"""Reporting (phase_09; render reworked in P3): immutable report snapshots that render the real
Cytoscape view.

The graph is rendered by the SAME library and style the frontend uses (``report_templates/
cytoscape.min.js`` is pinned to the frontend's Cytoscape 3.34). The page is fully self-contained (CSS +
JS + graph data inlined) so it renders offline and travels with the case (Phase 10 export). In P3 the
PDF is printed by the **OS browser engine** (Edge/WebView2 on Windows, system Chrome/Chromium on
macOS/Linux — see ``report_render.py``) instead of a bundled Chromium; Playwright is an optional
fallback. A machine with no engine still produces a fully valid report (HTML + hashed row) — only the
convenience PDF is skipped.

Honesty (Invariant #4 / #6): missing valuations are shown as missing (never a fabricated zero),
multi-source claims are shown side-by-side and contested entities labeled as such, FIFO links are
labeled as a convention, and the local-clock timestamp caveat + applied bounds (``scope_spec``) are
always printed so a report never implies completeness.

Immutability: each report is a frozen row whose SHA-256 ``content_hash`` is taken over the canonical
self-contained **HTML** (the reproducible source of truth) — NOT the PDF bytes, which are not
deterministic across engines/versions. The HTML is the ``rendered_file_ref`` (always present, always
hash-matching, what supersession + the export manifest + cross-machine re-verification key off); the
PDF is a derived artifact rendered alongside when an engine is available. A later report SUPERSEDES an
earlier one (``supersedes_report_id``) — an existing report is never edited.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

from ..app_paths import resource_path
from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Report
from .entities import resolve
from .entity_display import entity_display
from .graph import build_graph

# report.css + the vendored cytoscape.min.js — bundled READ-ONLY resources (P7): _MEIPASS/... when frozen,
# else repo backend/app/report_templates in source (via resource_path).
_TEMPLATES = resource_path("backend/app/report_templates")


# --------------------------------------------------------------------------- context assembly

def _collect_traces(conn) -> list[dict]:
    from .investigator import current_labels
    from .tracing import trace_bridge_links, trace_btc_links, trace_transfers
    # The display name = the trace's name, overridden by the investigator's latest custom label
    # (feature 5; migration 0008) — so a renamed path reads the same in the report as on the graph.
    # FN-04: `trace_btc_links`/`trace_transfers` both exclude retracted edges, so a retracted edge/link
    # never appears in the report (the row persists in-DB; it is just no longer part of the trace).
    custom = current_labels(conn, "trace")
    traces = []
    # v1.3.1: a soft-deleted (retracted) trace is withdrawn from the report just like a retracted edge.
    for t in conn.execute(
            "SELECT id, name, description FROM trace t "
            "WHERE NOT EXISTS (SELECT 1 FROM trace_retraction r WHERE r.trace_id=t.id) "
            "ORDER BY t.created_at, t.id").fetchall():
        links = trace_btc_links(conn, t["id"])
        transfers = trace_transfers(conn, t["id"])
        bridges = trace_bridge_links(conn, t["id"])  # FN-17: cross-chain investigator claims
        traces.append({"name": custom.get(t["id"]) or t["name"], "description": t["description"],
                       "btc_links": links, "transfers": transfers, "bridge_links": bridges})
    return traces


def _collect_findings(conn) -> list[dict]:
    # Enriched with a readable label per ref (shared with the live composer's /api/findings).
    from .investigator import list_findings
    return list_findings(conn)


def _collect_exhibits(conn) -> list[dict]:
    """Every exhibit with its STABLE number (FN-10) — the List-of-Exhibits source + the number a finding
    cites. Numbering is deterministic (``exhibits.numbered_exhibits`` sorts by captured_at, id)."""
    from .exhibits import numbered_exhibits
    return numbered_exhibits(conn)


def _collect_notes(conn) -> list[dict]:
    # The investigator-notes appendix: annotations + label overrides + tags grouped by target.
    from .investigator import collect_notes
    return collect_notes(conn)


def _collect_risk(conn) -> list[dict]:
    """Every ``risk_assessment`` claim joined to its address, GROUPED by address with each source's claim
    kept SIDE-BY-SIDE (Invariant #4 — multi-source risk is never collapsed/averaged). Sanctioned addresses
    sort first (the headline of a sanctions screen). This is DISTINCT from ``_collect_entities`` (GraphSense
    attribution / entity membership) — a sanctions screen and an attribution are different claims."""
    rows = conn.execute(
        "SELECT a.chain, a.address, a.address_display, r.category, r.source, r.score, r.score_scale, "
        "r.rationale, r.retrieved_at FROM risk_assessment r JOIN address a ON a.id=r.address_id "
        "ORDER BY a.chain, a.address, r.source, r.category").fetchall()
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r["chain"], r["address"])
        g = groups.setdefault(key, {"chain": r["chain"], "address": r["address"],
                                    "address_display": r["address_display"], "claims": [],
                                    "sanctioned": False})
        g["claims"].append({"category": r["category"], "source": r["source"], "score": r["score"],
                            "score_scale": r["score_scale"], "rationale": r["rationale"]})
        if r["category"] == "sanctioned":
            g["sanctioned"] = True
    # sanctioned addresses first, then by address — the screen's most important rows lead.
    return sorted(groups.values(), key=lambda g: (not g["sanctioned"], g["chain"], g["address"]))


def _collect_entities(conn) -> list[dict]:
    """Display every canonical (non-merged) entity that has at least one active membership."""
    out = []
    for e in conn.execute("SELECT id FROM entity ORDER BY created_at, id").fetchall():
        if resolve(conn, e["id"]) != e["id"]:
            continue  # a tombstone (merged into another) — shown under its survivor
        d = entity_display(conn, e["id"])
        if d["memberships"]:
            out.append(d)
    return out


def _valuation_honesty(conn) -> dict:
    """Counts that let the report state coverage honestly (never fabricate a missing price), plus whether a
    valuation pass is RUNNING at generation time (P8.7.3 #4) — so a report printed mid-valuation says so
    instead of silently freezing a half-priced snapshot."""
    from . import jobs

    total = conn.execute("SELECT COUNT(*) FROM v_value_movement").fetchone()[0]
    valued = conn.execute(
        "SELECT COUNT(*) FROM v_value_movement m "
        "WHERE EXISTS (SELECT 1 FROM valuation v WHERE v.subject_id=m.movement_id)").fetchone()[0]
    multi = conn.execute(
        "SELECT COUNT(*) FROM (SELECT subject_id FROM valuation GROUP BY subject_id HAVING COUNT(*)>1)"
    ).fetchone()[0]
    active = jobs.active()
    in_progress = bool(active and active.kind == "valuation" and active.state == "running")
    return {"movements": total, "valued": valued, "missing": total - valued, "multi_source": multi,
            "in_progress": in_progress}


def _facts_produced_by_query(conn) -> dict[str, int]:
    """Count, per ``source_query``, the fact/claim rows it produced — summed across every
    provenance-bearing table (any base table carrying a ``source_query_id`` column, discovered from the
    schema so tables added by later migrations are counted automatically). This is the provenance spine
    (Invariant #3) read straight back out of the DB; it never merges or interprets, only counts."""
    counts: dict[str, int] = {}
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:  # table names come from the schema, not user input — safe to interpolate.
        cols = [c["name"] for c in conn.execute(f'PRAGMA table_info("{t}")')]
        if "source_query_id" not in cols:
            continue
        for row in conn.execute(
                f'SELECT source_query_id AS sqid, COUNT(*) AS n FROM "{t}" '
                "WHERE source_query_id IS NOT NULL GROUP BY source_query_id"):
            counts[row["sqid"]] = counts.get(row["sqid"], 0) + row["n"]
    return counts


def _collect_custody(conn) -> list[dict]:
    """The chain-of-custody appendix (FN-02): every ``source_query`` in the case, in a DETERMINISTIC order
    (``requested_at``, ``id``), each serialized via the FN-01 provenance serializer (connector, capability,
    endpoint, params/bounds, retrieval time, FULL raw-response hash) plus ``produced`` = the count of
    fact/claim rows that query wrote. A query with no surviving rows is still listed — nothing is hidden.
    Read-only surfacing of the provenance spine (Invariant #3)."""
    from . import provenance_display

    produced = _facts_produced_by_query(conn)
    out = []
    for r in conn.execute("SELECT id FROM source_query ORDER BY requested_at, id").fetchall():
        rec = provenance_display.source_query(conn, r["id"])
        if rec is None:  # unreachable (the id came from the table) — defensive only.
            continue
        rec["produced"] = produced.get(r["id"], 0)
        out.append(rec)
    return out


def _collect_methodology(conn) -> dict:
    """Facts backing the Methodology section (FN-08): the per-chain finality thresholds ACTUALLY USED in
    this case. The chains come from ``transaction_`` — every EVM ``transfer`` and BTC ``tx_output`` hangs
    off a ``transaction_`` row, so this is exactly the set of chains whose finality was evaluated (Invariant
    #6) — each paired with the threshold from the LIVE app config (``config.py``), never a hardcoded literal.
    So the report states the exact policy that flipped its own facts provisional→final, and a
    ``BIH_FINALITY_THRESHOLDS`` override is reflected verbatim. Deterministic (chains sorted) so the report
    ``content_hash`` stays stable."""
    from ..config import get_settings

    settings = get_settings()
    chains = [r["chain"] for r in conn.execute(
        "SELECT DISTINCT chain FROM transaction_ ORDER BY chain").fetchall()]
    return {"finality_thresholds": [
        {"chain": c, "threshold": settings.finality_threshold(c)} for c in chains]}


# The glossary catalog (FN-08/P17): (term, definition-HTML, trigger-SQL). A term is listed ONLY when its
# trigger finds the kind of evidence it describes actually present in THIS case — so the glossary defines
# the case's real vocabulary, never boilerplate. Definitions are fixed, trusted HTML (no user data, so no
# escaping); the term goes in a <dt>, the definition in a <dd>. Order is fixed (schema/model terms first)
# → deterministic. Each trigger is a cheap EXISTS-style probe; a term whose table is empty is skipped.
_GLOSSARY_CATALOG: list[tuple[str, str, str]] = [
    ("UTXO (unspent transaction output)",
     "Bitcoin&rsquo;s ledger model: value exists as transaction <i>outputs</i> that later transactions "
     "consume as <i>inputs</i>. There is no direct sender&rarr;receiver &ldquo;transfer&rdquo; recorded "
     "on-chain &mdash; only inputs and outputs.",
     "SELECT 1 FROM transaction_ WHERE chain='bitcoin' LIMIT 1"),
    ("FIFO (first-in-first-out)",
     "A convention for ordering commingled funds when tracing (the first coins in are treated as the "
     "first out), applied as a labeled <b>claim</b>, never as ground-truth flow. Legal basis: "
     "<i>Clayton&rsquo;s Case</i>.",
     "SELECT 1 FROM trace_btc_link WHERE basis='fifo' LIMIT 1"),
    ("Provisional vs. final",
     "A transaction&rsquo;s facts are <b>provisional</b> (still correctable) until it reaches the "
     "per-chain confirmation threshold, after which they are treated as <b>final</b> and immutable. "
     "Provisional facts are drawn dashed/faded in the graph.",
     "SELECT 1 FROM transaction_ WHERE finality_status='provisional' LIMIT 1"),
    ("Value at time of movement",
     "Each value movement is priced at (or near) the timestamp of its transaction&rsquo;s block using a "
     "historical price for that moment &mdash; not a current price &mdash; so a figure reflects the value "
     "when the funds actually moved.",
     "SELECT 1 FROM valuation LIMIT 1"),
    ("Attribution vs. fact",
     "An <b>attribution</b> (e.g. &ldquo;this address belongs to Exchange&nbsp;X&rdquo;) is a sourced "
     "<i>claim</i>, distinct from an on-chain <i>fact</i> (a recorded transaction). Attributions carry "
     "their source and are never merged into a single verdict.",
     "SELECT 1 FROM attribution LIMIT 1"),
    ("CoinJoin",
     "A Bitcoin transaction that combines many participants&rsquo; inputs and outputs to break the "
     "common-input (co-spend) heuristic. Addresses in such a transaction are flagged so clustering is "
     "not over-trusted.",
     "SELECT 1 FROM entity_membership WHERE flags LIKE '%possible-coinjoin%' LIMIT 1"),
    ("Sanctioned (OFAC SDN)",
     "An address a sanctions source (e.g. the OFAC Specially Designated Nationals list) has designated. "
     "A sanctions hit is a sourced <b>claim</b>, not a fact about the chain.",
     "SELECT 1 FROM risk_assessment WHERE category='sanctioned' LIMIT 1"),
    ("Co-spend cluster",
     "A set of Bitcoin addresses inferred to share a controller because they were spent together as "
     "inputs to one transaction (the common-input heuristic). An inference &mdash; weakened when a "
     "transaction is a CoinJoin.",
     "SELECT 1 FROM entity_membership WHERE method='co-spend' LIMIT 1"),
    ("Cross-chain bridge",
     "A protocol that moves value between blockchains. A bridge crossing is recorded only as a labeled "
     "investigator <b>claim</b> inside a trace &mdash; never synthesized as an on-chain transfer.",
     "SELECT 1 FROM trace_bridge_link LIMIT 1"),
]


def _collect_glossary(conn) -> list[dict]:
    """The case-scoped glossary (FN-11): every catalog term whose trigger finds matching evidence in this
    case, in fixed catalog order (deterministic). Read-only; defines the case's vocabulary honestly (only
    terms actually used) so the report never pads with definitions it doesn't rely on."""
    out = []
    for term, definition, trigger in _GLOSSARY_CATALOG:
        if conn.execute(trigger).fetchone():
            out.append({"term": term, "definition": definition})
    return out


def build_report_context(conn, *, title: str, scope_spec: dict, generated_at: str,
                         graph: dict | None = None) -> dict:
    case = conn.execute("SELECT id, title, description FROM case_meta LIMIT 1").fetchone()
    # FN-10: assign every exhibit its stable number, then cross-reference that number from any finding that
    # refers to it (the report cites "Exhibit N", never the raw exhibit id).
    exhibits = _collect_exhibits(conn)
    exhibit_labels = {e["id"]: e["label"] for e in exhibits}
    findings = _collect_findings(conn)
    for f in findings:
        for r in f["refs"]:
            if r["ref_type"] == "exhibit":
                r["label"] = exhibit_labels.get(r["ref_id"], r["label"])
    return {
        "title": title,
        "case": dict(case) if case else {"title": "(uninitialized case)"},
        "generated_at": generated_at,
        "scope_spec": scope_spec,
        # P8.7.1 #2 — the report renders the investigator's CURRENT bounded VIEW when one is supplied
        # (focus/hops/dust/denominations/spam-collapse/poison-fold/value_basis), else the full case graph.
        "graph": graph if graph is not None else build_graph(conn),
        "methodology": _collect_methodology(conn),
        "traces": _collect_traces(conn),
        "findings": findings,
        "exhibits": exhibits,
        "notes": _collect_notes(conn),
        "risk": _collect_risk(conn),
        "entities": _collect_entities(conn),
        "valuation": _valuation_honesty(conn),
        "custody": _collect_custody(conn),
        "glossary": _collect_glossary(conn),
    }


# --------------------------------------------------------------------------- HTML rendering

def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _script_safe(json_text: str) -> str:
    """Escape a JSON string for safe embedding inside an inline ``<script>`` element (SEC-01/SEC-11).

    ``json.dumps`` does NOT escape ``<`` / ``>`` / ``&``, so an attacker-controlled token symbol /
    attribution name / label containing ``</script>`` (or ``<!--``) closes the element per the HTML
    script-data-end rule and its trailing markup is parsed as HTML — stored code-execution into the
    court-facing report. Escaping these to ``\\uXXXX`` (still-valid JSON string escapes the browser
    decodes back to the same characters) keeps the payload as DATA, never markup. U+2028/U+2029 are
    already escaped by the default ``ensure_ascii=True`` but are handled here too as defense-in-depth."""
    return (json_text
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))


def _short(s, head: int = 10, tail: int = 8) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= head + tail + 1 else f"{s[:head]}…{s[-tail:]}"


def _methodology_section(m: dict) -> str:
    """The Methodology section (FN-08): a court-ready, self-contained statement of HOW to read this report
    — the Bitcoin input/output tracing convention + its legal basis, the value-at-time valuation method,
    the finality thresholds actually applied, the side-by-side (never-averaged) claim policy, the scope
    bounds, and the honest limits. Everything here is fixed prose EXCEPT the finality table, whose values
    are the case's real config thresholds (``_collect_methodology``) — so the section documents existing
    behavior honestly and renders deterministically."""
    thr = m["finality_thresholds"]
    if thr:
        rows = "".join(
            f"<tr><td>{_esc(t['chain'])}</td>"
            f"<td class='count'>{_esc(t['threshold'])}+ confirmations</td></tr>" for t in thr)
        finality_table = ("<table><tr><th>chain</th><th>treated as final at</th></tr>"
                          f"{rows}</table>")
    else:
        finality_table = ('<p class="empty">No on-chain transactions are recorded in this case, so no '
                          "finality threshold was applied.</p>")
    return (
        "<h3>How movement is traced</h3>"
        "<p>On Bitcoin the ledger records transaction inputs and outputs, not a direct "
        "sender&rarr;receiver transfer, and a specific input is never inherently linked to a specific "
        "output. Where a trace connects one to another it applies a <b>First-In-First-Out (FIFO)</b> "
        "ordering as a labeled <b>convention</b> (basis <span class='pill fifo'>fifo</span>), or records "
        "an explicit investigator assertion (basis <span class='pill investigator'>investigator</span>). "
        "Neither is presented as ground-truth flow of funds. FIFO applied to commingled funds follows the "
        "rule in <i>Clayton&rsquo;s Case</i> (<i>Devaynes v Noble</i> (1816)), applied to mixed "
        "cryptoassets in <i>D&rsquo;Aloia v Persons Unknown</i> [2022] EWHC 1723 (Ch). On EVM chains a "
        "transfer <i>is</i> a recorded on-chain fact (A&rarr;B) and is shown as such.</p>"
        "<h3>Valuation (value at the time of movement)</h3>"
        "<p>Each value movement is priced at, or near, the timestamp of its transaction&rsquo;s block, "
        "using a source&rsquo;s historical price for that asset. Where more than one source prices the "
        "same movement, every source&rsquo;s value is kept side-by-side &mdash; never averaged or blended "
        "into one number. A movement with no available price is shown as missing, never as a fabricated "
        "zero.</p>"
        "<h3>Finality before immutability</h3>"
        "<p>A transaction&rsquo;s facts are treated as immutable only once the transaction is <b>final</b> "
        "(confirmations at or above the per-chain threshold); provisional (tip) facts remain correctable "
        "and are drawn dashed/faded in the graph. The thresholds below are the policy actually applied in "
        "this case, read from this installation&rsquo;s active configuration:</p>"
        f"{finality_table}"
        "<h3>Sources are kept side-by-side</h3>"
        "<p>When sources disagree &mdash; on a risk or sanctions score, an entity attribution, or a "
        "valuation &mdash; every source&rsquo;s claim is retained separately with its own provenance. This "
        "tool never merges, averages, or synthesizes a single verdict from conflicting sources.</p>"
        "<h3>Scope</h3>"
        "<p>This report reflects only the data acquired under the applied bounds recorded in "
        "&ldquo;Scope &amp; applied bounds&rdquo; below. It does not imply the complete transaction "
        "history of any address or entity.</p>"
        "<h3>Limits &amp; honesty</h3>"
        "<p>Timestamps are recorded from the local machine clock and are <b>not</b> independently notarized "
        "or timestamped by a trusted third party. The chain-of-custody appendix lists the full SHA-256 of "
        "every raw response, so any recipient can independently verify each exhibit against the source "
        "data. Nothing here should be read as legal advice or as a conclusive attribution of identity.</p>")


def _trace_section(traces: list[dict]) -> str:
    if not traces:
        return '<p class="empty">No traces in this case.</p>'
    parts = []
    for t in traces:
        parts.append(f"<h3>{_esc(t['name'])}</h3>")
        if t["description"]:
            parts.append(f'<p class="sub">{_esc(t["description"])}</p>')
        if t["btc_links"]:
            rows = "".join(
                f"<tr><td class='mono'>{_esc(_short(l['source_output_id']))}</td>"
                f"<td class='mono'>{_esc(_short(l['dest_output_id']))}</td>"
                f"<td><span class='pill {_esc(l['basis'])}'>{_esc(l['basis'])}</span></td>"
                f"<td>{_esc(l['note'])}</td></tr>"
                for l in t["btc_links"])
            parts.append(
                "<table><tr><th>source output</th><th>dest output</th><th>basis</th>"
                f"<th>note</th></tr>{rows}</table>")
        if t["transfers"]:
            parts.append(f'<p class="sub">{len(t["transfers"])} EVM transfer edge(s) referenced.</p>')
        if t.get("bridge_links"):
            # FN-17: cross-chain bridge crossings — labeled investigator claims, never ledger facts.
            rows = "".join(
                f"<tr><td class='mono'>{_esc(b['src_chain'])}: {_esc(_short(b['src_subject_id']))}</td>"
                f"<td class='mono'>{_esc(b['dst_chain'])}: {_esc(_short(b['dst_subject_id']))}</td>"
                f"<td><span class='pill {_esc(b['basis'])}'>{_esc(b['basis'])}</span></td>"
                f"<td>{_esc(b['note'])}</td></tr>"
                for b in t["bridge_links"])
            parts.append(
                "<table><tr><th>from (chain A)</th><th>to (chain B)</th><th>basis</th>"
                f"<th>note</th></tr>{rows}</table>")
        if not t["btc_links"] and not t["transfers"] and not t.get("bridge_links"):
            parts.append('<p class="empty">empty trace</p>')
    return "".join(parts)


def _findings_section(findings: list[dict]) -> str:
    if not findings:
        return '<p class="empty">No findings composed.</p>'
    parts = []
    for f in findings:
        parts.append(f"<h3>{_esc(f['statement'])}</h3>")
        if f["assessment"]:
            parts.append(f'<p class="sub">assessment: {_esc(f["assessment"])}</p>')
        if f["refs"]:
            rows = "".join(
                f"<tr><td>{_esc(r['ref_type'])}</td><td>{_esc(r.get('label') or _short(r['ref_id']))}</td>"
                f"<td>{_esc(r['note'])}</td></tr>" for r in f["refs"])
            parts.append(f"<table><tr><th>refers to</th><th>target</th><th>note</th></tr>{rows}</table>")
    return "".join(parts)


def _exhibits_section(exhibits: list[dict]) -> str:
    """The List of Exhibits (FN-10): each hashed artifact (screenshot/file/export) with its STABLE number,
    type, source, capture time, description, and FULL content hash for tamper-checking. Numbers are assigned
    by a deterministic sort (report immutability) and are what findings cite ("Exhibit N")."""
    if not exhibits:
        return ('<p class="empty">No exhibits attached — this case cites no screenshot/file/export '
                "artifacts.</p>")
    lead = (f"<p>{len(exhibits)} exhibit(s), numbered by capture time. Each is a hashed artifact; the full "
            "SHA-256 lets any recipient verify the file has not changed. Findings cite these numbers.</p>")
    rows = "".join(
        f"<tr><td><b>{_esc(e['label'])}</b></td><td>{_esc(e['exhibit_type'])}</td>"
        f"<td>{_esc(e['source'])}</td><td>{_esc(e['captured_at'])}</td>"
        f"<td>{_esc(e['description'])}</td><td class='hash'>{_esc(e['content_hash'])}</td></tr>"
        for e in exhibits)
    table = ("<table class='exhibits'><tr><th>exhibit</th><th>type</th><th>source</th><th>captured</th>"
             f"<th>description</th><th>content hash (SHA-256)</th></tr>{rows}</table>")
    return lead + table


def _notes_section(notes: list[dict]) -> str:
    """The investigator-notes appendix: every annotation / label override / tag, grouped by target —
    so free-text notes flow narrative-ready into the immutable PDF."""
    groups = [g for g in notes if g.get("annotations") or g.get("tags") or g.get("label_override")]
    if not groups:
        return '<p class="empty">No investigator notes.</p>'
    parts = []
    for g in groups:
        head = f"{_esc(g['label'])} <span class='sub muted'>({_esc(g['target_type'])})</span>"
        parts.append(f"<h3>{head}</h3>")
        if g.get("label_override"):
            parts.append(f'<p class="sub">investigator label: {_esc(g["label_override"])}</p>')
        if g.get("tags"):
            parts.append(f'<p class="sub">tags: {_esc(", ".join(g["tags"]))}</p>')
        if g.get("annotations"):
            items = "".join(f"<li>{_esc(a['content'])}</li>" for a in g["annotations"])
            parts.append(f"<ul>{items}</ul>")
    return "".join(parts)


def _risk_section(risk: list[dict]) -> str:
    """The Risk & sanctions screen — the headline of an intel check. Lists every screened address that
    carries a risk/sanctions claim, each SOURCE side-by-side (Invariant #4), sanctioned addresses first.
    Distinct from the GraphSense Entities section below (attribution vs a sanctions/risk claim)."""
    if not risk:
        return ('<p class="empty">No sanctions or risk claims — no address in this case matched a '
                "sanctions/risk source (OFAC SDN / GraphSense abuse / Chainalysis).</p>")
    sanctioned = [g for g in risk if g["sanctioned"]]
    lead = (f"<p><b>{len(sanctioned)} address(es) screened as SANCTIONED</b>; {len(risk)} address(es) "
            "carry a risk/sanctions claim. Each source's claim is shown side-by-side, never merged or "
            "averaged (Invariant #4). A sanctions hit is a sourced claim, not a fact about the chain.</p>"
            if sanctioned else
            f"<p>{len(risk)} address(es) carry a non-sanctions risk claim; none screened as sanctioned. "
            "Each source's claim is shown side-by-side (Invariant #4).</p>")
    rows = []
    for g in risk:
        for c in g["claims"]:
            cat = c["category"] or "risk"
            pill_cls = "sanctioned" if cat == "sanctioned" else ("elevated" if cat != "sanctioned" else "")
            score = ""
            if c["score"] is not None:
                score = f"{_esc(c['score'])}" + (f"/{_esc(c['score_scale'])}" if c["score_scale"] else "")
            rows.append(
                f"<tr><td class='mono'>{_esc(_short(g['address']))}</td>"
                f"<td>{_esc(g['chain'])}</td>"
                f"<td><span class='pill {pill_cls}'>{_esc(cat)}</span></td>"
                f"<td>{_esc(c['source'])}</td><td>{score}</td>"
                f"<td>{_esc(c['rationale'])}</td></tr>")
    table = ("<table><tr><th>address</th><th>chain</th><th>category</th><th>source</th><th>score</th>"
             f"<th>rationale / designation</th></tr>{''.join(rows)}</table>")
    return lead + table


# How many member addresses to print inline before collapsing the tail (keeps a 166-address cluster to a
# few lines, not a few pages — the rest stay in case.db / the graph).
_CLUSTER_MEMBERS_SHOWN = 60


def _concise_method(method: str | None) -> str:
    """A short, readable label for a membership method — so a cluster header reads "BTC change (5
    heuristics, ≥2 agree)" instead of the full ``address_reuse+address_type+…@agree>=2`` concatenation."""
    m = method or ""
    if m.startswith("change:"):
        body, _, agree = m[len("change:"):].partition("@")  # "h1+h2+...","agree>=N"
        k = len([h for h in body.split("+") if h])
        thr = agree.split(">=")[-1] if ">=" in agree else "?"
        return f"BTC change ({k} heuristic{'s' if k != 1 else ''}, ≥{thr} agree)"
    return {
        "co-spend": "co-spend", "deposit-forward": "deposit-address reuse",
        "airdrop-aggregation": "airdrop multi-participation", "approve-control": "self-authorization",
        "same-address-heuristic": "same-address", "tagpack-actor": "TagPack actor",
        "manual": "investigator", "shared-label": "shared label",
    }.get(m, m)


def _entities_section(entities: list[dict]) -> str:
    """Each entity/cluster as a SUMMARY (P8.8.1): one header line — name · source · concise method ·
    confidence · member count — then a COMPACT member list (addresses only). The per-member
    source/method/confidence are identical across a cluster, so they are NOT repeated per row (a co-spend
    cluster of 166 was 166 identical rows; now it is one summary + a one-line address list)."""
    if not entities:
        return '<p class="empty">No entities resolved.</p>'
    parts = []
    for e in entities:
        name = e["name"] or f"(unnamed {e['origin']} entity)"
        ext = e.get("external_id")
        id_note = f" <span class='sub muted'>(id: {_esc(ext)})</span>" if ext and ext != name else ""
        parts.append(f"<h3>{_esc(name)}{id_note} "
                     f"<span class='pill {_esc(e['status'])}'>{_esc(e['status'])}</span></h3>")
        members = e["memberships"]
        # group by (source, method) — uniform within a heuristic cluster; >1 group only for a mixed/contested
        # entity (each group shown side-by-side, never merged — Invariant #4).
        groups: dict[tuple, list[dict]] = {}
        for m in members:
            groups.setdefault((m["source"], m["method"]), []).append(m)
        for (source, method), grp in groups.items():
            confs = [m["confidence"] for m in grp if m["confidence"] is not None]
            conf = (f"{min(confs):g}" if confs and min(confs) == max(confs)
                    else (f"{min(confs):g}–{max(confs):g}" if confs else "—"))
            flags = sorted({m["flags"] for m in grp if m.get("flags")})
            flag_note = f" · <span class='pill missing'>{_esc(', '.join(flags))}</span>" if flags else ""
            parts.append(
                f"<p class='sub'><b>{_esc(source)}</b> · {_esc(_concise_method(method))} · "
                f"confidence {conf} · {len(grp)} address{'es' if len(grp) != 1 else ''}{flag_note}</p>")
            addrs = [m.get("address") for m in grp if m.get("address")]
            shown = ", ".join(_esc(_short(a)) for a in addrs[:_CLUSTER_MEMBERS_SHOWN])
            more = f" … and {len(addrs) - _CLUSTER_MEMBERS_SHOWN} more" if len(addrs) > _CLUSTER_MEMBERS_SHOWN else ""
            parts.append(f"<p class='mono' style='font-size:10px'>{shown}{more}</p>")
        if e["status"] == "contested":
            parts.append('<p class="sub muted">Multiple sources disagree — shown side-by-side, '
                         "not collapsed into one verdict.</p>")
    return "".join(parts)


def _valuation_section(v: dict) -> str:
    miss = (f" <span class='pill missing'>{v['missing']} movement(s) have no price</span> — shown "
            "without a fabricated value." if v["missing"] else "")
    multi = (f" {v['multi_source']} movement(s) carry more than one source's price, preserved "
             "side-by-side." if v["multi_source"] else "")
    # P8.7.3 #4 — valuation is asynchronous (a background pass); a report can be generated before it
    # finishes. Say so explicitly so a half-valued snapshot is honest, not silently incomplete.
    prog = (f"<p class='caveat'><b>Valuation in progress at generation:</b> {v['valued']} of "
            f"{v['movements']} movement(s) priced so far. USD coverage was still filling in when this "
            "report was frozen — re-generate after valuation completes for full coverage.</p>"
            if v.get("in_progress") else "")
    return (f"<p>{v['valued']} of {v['movements']} value movement(s) are valued in USD.{miss}{multi}</p>{prog}")


def _flatten_scope(scope, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a ``scope_spec`` into DETERMINISTIC ``(key, value)`` rows for the scope table (P14): nested
    dicts become dotted keys, lists join to a compact string, bools read yes/no, ``None`` shows an em dash.
    Keys are sorted at every level so the rendered table (and thus the report ``content_hash``) is stable."""
    rows: list[tuple[str, str]] = []
    if isinstance(scope, dict):
        for k in sorted(scope.keys(), key=str):
            rows.extend(_flatten_scope(scope[k], f"{prefix}{k}."))
        return rows
    key = prefix[:-1] if prefix else "(value)"
    if isinstance(scope, bool):
        val = "yes" if scope else "no"
    elif scope is None:
        val = "—"
    elif isinstance(scope, list):
        val = ", ".join(json.dumps(x, sort_keys=True) if isinstance(x, (dict, list)) else str(x)
                        for x in scope) or "—"
    else:
        val = str(scope)
    rows.append((key, val))
    return rows


def _scope_section(scope: dict) -> str:
    """Render the applied bounds as a court-legible key/value TABLE (P14) instead of a raw ``<pre>`` JSON
    dump. Deterministic (``_flatten_scope`` sorts keys) so the report ``content_hash`` stays stable."""
    rows = _flatten_scope(scope)
    if not rows:
        return '<p class="empty">No scope bounds recorded (full case, as ingested).</p>'
    trs = "".join(f"<tr><td class='mono'>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in rows)
    return f"<table class='scope'><tr><th>bound</th><th>value</th></tr>{trs}</table>"


def _custody_section(custody: list[dict]) -> str:
    """Chain-of-custody appendix (FN-02): a deterministic table of every ``source_query`` behind the case —
    connector / capability / endpoint, params/bounds, retrieval time, status, count of facts/claims
    produced, and the FULL (untruncated) raw-response SHA-256 for tamper-checking. This is what makes every
    exhibit traceable to the exact query that acquired it (Invariant #3) — the core of a defensible case."""
    if not custody:
        return ('<p class="empty">No source queries recorded — this case contains no acquired facts or '
                "claims.</p>")
    lead = (f"<p>{len(custody)} source quer{'y' if len(custody) == 1 else 'ies'} produced the facts and "
            "claims in this case. Each row is the exact query behind one or more exhibits, with the full "
            "SHA-256 of its raw response for tamper-checking. Ordered by acquisition time; a query with no "
            "surviving rows is still listed (nothing is hidden).</p>")
    rows = []
    for q in custody:
        params = q.get("params")
        params_txt = (json.dumps(params, sort_keys=True) if isinstance(params, (dict, list))
                      else ("" if params is None else str(params)))
        h = q.get("raw_response_hash")
        hash_cell = (f"<span class='hash'>{_esc(h)}</span>" if h
                     else "<span class='muted'>&mdash; (no raw response captured)</span>")
        rows.append(
            f"<tr><td>{_esc(q['connector'])}</td><td>{_esc(q['capability'])}</td>"
            f"<td>{_esc(q['endpoint'])}</td><td class='params'>{_esc(params_txt)}</td>"
            f"<td>{_esc(q['requested_at'])}</td><td>{_esc(q['status'])}</td>"
            f"<td class='count'>{_esc(q['produced'])}</td><td>{hash_cell}</td></tr>")
    table = ("<table class='custody'><tr><th>connector</th><th>capability</th><th>endpoint</th>"
             "<th>params / bounds</th><th>retrieved</th><th>status</th><th>facts/claims</th>"
             f"<th>raw-response hash (SHA-256)</th></tr>{''.join(rows)}</table>")
    return lead + table


def _glossary_section(glossary: list[dict]) -> str:
    """The case-scoped glossary appendix (FN-11): a definition list of only the terms this case uses. The
    section is rendered only when non-empty (`_effective_sections` omits it otherwise), so this empty branch
    is defensive. Definitions are trusted static HTML; the term is escaped."""
    if not glossary:
        return '<p class="empty">No specialized terms are used in this case.</p>'
    lead = ("<p>Specialized terms used in this report, defined for the reader. Only terms this case "
            "actually relies on are listed.</p>")
    items = "".join(f"<dt>{_esc(g['term'])}</dt><dd>{g['definition']}</dd>" for g in glossary)
    return lead + f'<dl class="glossary">{items}</dl>'


# --------------------------------------------------------------------------- court-formal scaffolding (FN-12)

# The report's content sections, in render order, as (anchor-slug, heading) pairs. This single list drives
# BOTH the table of contents and the <section id=…> wrappers, so a TOC entry can never drift from its
# heading (a test asserts the two sets are identical). Anchors live on the <section> wrapper, not the <h2>,
# so the headings stay bare <h2>Title</h2> — pre-existing tests that split on a bare heading are unaffected.
_REPORT_SECTIONS: list[tuple[str, str]] = [
    ("methodology", "Methodology"),
    ("graph", "Graph"),
    ("risk-and-sanctions", "Risk & sanctions"),
    ("traces", "Traces"),
    ("findings", "Findings"),
    ("investigator-notes", "Investigator notes"),
    ("entities", "Entities"),
    ("valuation-coverage", "Valuation coverage"),
    ("scope-and-applied-bounds", "Scope & applied bounds"),
    ("list-of-exhibits", "List of Exhibits"),
    ("chain-of-custody", "Chain of custody"),
]

# The graph section's body (the Cytoscape canvas + the fixed legend). Static markup, kept as a constant so
# the section loop can treat every section's body uniformly.
_GRAPH_BODY = (
    '<div id="cy"></div>\n'
    '  <div class="legend">\n'
    "    <span>&#9679; address</span><span>&#9646; bitcoin transaction (routing)</span>\n"
    "    <span>&#9670; external (mint/burn/coinbase)</span><span>&#9654; transfer (filled arrow)</span>\n"
    "    <span>&#8867; tx input (bar arrow)</span><span>&#8250; tx output (chevron)</span><span>fine dots = provisional (tip)</span>\n"
    "    <span>&#9210; red halo = sanctioned/risk</span><span>teal ring = attributed entity</span>\n"
    "    <span>&#9888; possible-coinjoin</span><span>long dash = FIFO trace &middot; long dash-dot = investigator (convention)</span>\n"
    "    <span>&#9733; seed / anchor</span><span>edge label = value; width &prop; value</span>\n"
    "  </div>")


def _css_str(s) -> str:
    """Escape a value for a CSS string literal (``content: "…"``). Case ids are UUIDs, but escape a
    backslash/quote defensively so a value can never break out of the string into arbitrary CSS."""
    return '"' + str("" if s is None else s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _footer_css(case_id) -> str:
    """A running footer on EVERY printed page (FN-12): the case id (page-identification if pages are
    physically separated) at bottom-left, ``Page N of M`` at bottom-right — via CSS ``@page`` margin boxes.

    Injected as a SECOND ``@page`` rule: Blink merges margin boxes across ``@page`` rules with report.css's
    ``@page { size; margin }`` (verified this phase on BOTH the default Edge/Chrome ``--print-to-pdf`` CLI
    and the Playwright fallback — both render ``counter(page)``/``counter(pages)``). The dynamic case id is
    emitted as a LITERAL (Blink does not support the GCPM ``string()``/``string-set`` feature), CSS-escaped.
    ``--no-pdf-header-footer`` stays set on the CLI, so ONLY this footer prints — never the browser's own
    default footer (which would also leak the ``file://`` path)."""
    label = _css_str(f"Case {case_id}") if case_id else _css_str("Case (uninitialized)")
    ff = '"Segoe UI", system-ui, -apple-system, Arial, sans-serif'
    box = f"font-family: {ff}; font-size: 8px; color: var(--bih-report-footer-text);"
    return ("\n@page {\n"
            f"  @bottom-left {{ content: {label}; {box} }}\n"
            f'  @bottom-right {{ content: "Page " counter(page) " of " counter(pages); {box} }}\n'
            "}\n")


def _cover_section(ctx: dict) -> str:
    """The cover page (FN-12): report title, case identity, generated-at, a court-formal ``Prepared by``
    signature line, and an integrity statement.

    Watch item (documented): the report's OWN SHA-256 ``content_hash`` is deliberately NOT printed here — it
    is taken over this very HTML, so embedding it would change the bytes it certifies (a document cannot
    contain a verifiable hash of itself). The cover instead states how to recompute + check the hash against
    the immutable report registry; the value lives in the ``report`` row + export manifest."""
    case = ctx["case"]
    case_id = case.get("id") or "(uninitialized)"
    selection = (ctx.get("scope_spec") or {}).get("selection", "full-case")
    return (
        '<section class="cover">'
        '<div class="cover-org">Blockchain Investigation Hub</div>'
        f'<h1 class="cover-title">{_esc(ctx["title"])}</h1>'
        '<div class="cover-kind">Provenance-first blockchain investigation report</div>'
        '<table class="cover-meta">'
        f'<tr><td class="k">Case</td><td class="v">{_esc(case.get("title"))}</td></tr>'
        f'<tr><td class="k">Case ID</td><td class="v mono">{_esc(case_id)}</td></tr>'
        f'<tr><td class="k">Generated</td><td class="v">{_esc(ctx["generated_at"])} '
        '<span class="sub">(local machine clock)</span></td></tr>'
        f'<tr><td class="k">Scope</td><td class="v">{_esc(selection)}</td></tr>'
        '<tr><td class="k">Prepared by</td><td class="v sig">&nbsp;</td></tr>'
        '</table>'
        '<div class="cover-integrity"><b>Integrity &amp; verification.</b> This report is a frozen '
        "snapshot. Its authenticity is anchored by a <b>SHA-256 content hash</b> taken over this "
        "report&rsquo;s canonical HTML and recorded in the case&rsquo;s immutable report registry and "
        'export manifest. To verify, recompute the SHA-256 of the report&rsquo;s <span class="mono">.html'
        "</span> file and compare it against the registry. The hash value is deliberately not printed on "
        "this page &mdash; a document cannot contain a verifiable hash of itself.</div>"
        "</section>")


def _effective_sections(ctx: dict) -> list[tuple[str, str]]:
    """The sections to render for THIS report: the fixed content sections, plus the Glossary appendix ONLY
    when the case actually uses a glossary term (FN-11). Both the TOC and the section bodies iterate this
    same list, so a conditional appendix stays in lockstep — the P16 invariant holds (TOC targets ==
    rendered section anchors)."""
    sections = list(_REPORT_SECTIONS)
    if ctx.get("glossary"):
        sections.append(("glossary", "Glossary"))
    return sections


def _toc_section(sections: list[tuple[str, str]]) -> str:
    """The table of contents (FN-12): every rendered section, linked to its ``<section>`` anchor. Driven by
    the SAME effective list that emits the sections, so it can never list a section the report doesn't
    render. The ``Contents`` heading is intentionally not itself a section anchor."""
    items = "".join(f'<li><a href="#{slug}">{_esc(title)}</a></li>' for slug, title in sections)
    return f'<nav class="toc"><h2>Contents</h2><ol>{items}</ol></nav>'


def _sections_html(ctx: dict, sections: list[tuple[str, str]]) -> str:
    """Render each section in ``sections`` as ``<section id="slug"><h2>Title</h2>…body…</section>``. The
    anchor is on the wrapper; the heading stays a bare ``<h2>`` (so tests that split on a heading are
    unaffected). Body content + order are unchanged from the pre-FN-12 report (plus the FN-11 glossary)."""
    bodies = {
        "methodology": f'<div class="methodology">{_methodology_section(ctx["methodology"])}</div>',
        "graph": _GRAPH_BODY,
        "risk-and-sanctions": _risk_section(ctx["risk"]),
        "traces": _trace_section(ctx["traces"]),
        "findings": _findings_section(ctx["findings"]),
        "investigator-notes": _notes_section(ctx["notes"]),
        "entities": _entities_section(ctx["entities"]),
        "valuation-coverage": _valuation_section(ctx["valuation"]),
        "scope-and-applied-bounds": _scope_section(ctx["scope_spec"]),
        "list-of-exhibits": _exhibits_section(ctx["exhibits"]),
        "chain-of-custody": _custody_section(ctx["custody"]),
        "glossary": _glossary_section(ctx.get("glossary") or []),
    }
    return "".join(
        f'\n  <section id="{slug}">\n  <h2>{_esc(title)}</h2>{bodies[slug]}\n  </section>'
        for slug, title in sections)


def render_html(ctx: dict) -> str:
    from ..theme import css_root_block, cytoscape_style_json

    css = (_TEMPLATES / "report.css").read_text(encoding="utf-8")
    cyjs = (_TEMPLATES / "cytoscape.min.js").read_text(encoding="utf-8")
    elements = [{"data": n} for n in ctx["graph"]["nodes"]] + \
               [{"data": e} for e in ctx["graph"]["edges"]]
    case = ctx["case"]
    # The token catalog as :root custom properties, ahead of report.css (which uses var(--bih-...)) — the
    # single source of truth shared with the live graph (no hardcoded hex in report.css).
    theme_vars = css_root_block()
    # FN-12: a running page footer (case id + "Page N of M") via a 2nd @page rule appended after report.css.
    footer_css = _footer_css(case.get("id"))
    # FN-11/FN-12: the TOC and the section bodies iterate ONE effective list (glossary appended only when used).
    sections = _effective_sections(ctx)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{_esc(ctx['title'])}</title>
<style>{theme_vars}{css}{footer_css}</style></head>
<body>
  {_cover_section(ctx)}
  {_toc_section(sections)}
  <p class="sub">Case: {_esc(case.get('title'))} &middot; generated {_esc(ctx['generated_at'])}</p>

  <div class="caveat">
    <b>Read this first.</b> This report is a <b>frozen snapshot</b> generated at the local-clock time
    above; on-chain state may have changed since. It reflects only data acquired under the
    <b>applied bounds</b> below and therefore does <b>not</b> imply completeness. Provisional (tip)
    facts are drawn dashed/faded. Source claims that disagree are shown side-by-side, never merged.
    Bitcoin input&rarr;output links are a labeled tracing <b>convention</b> (FIFO) or an explicit
    investigator assertion — never ground-truth flow.
  </div>
  {_sections_html(ctx, sections)}

  <div class="footer">Blockchain Investigation Hub — provenance-first case report. Every fact in the
    underlying case references the source query that produced it.</div>

  <script>{cyjs}</script>
  <script>
    var ELEMENTS = {_script_safe(json.dumps(elements))};
    var STYLE = {_script_safe(cytoscape_style_json())};
    var cy = cytoscape({{ container: document.getElementById('cy'), elements: ELEMENTS, style: STYLE }});
    window.__cy = cy;
    function done() {{ if (window.__CY_READY__) return; try {{ cy.fit(undefined, 20); }} catch (e) {{}} window.__CY_READY__ = true; }}
    if (ELEMENTS.length === 0) {{ done(); }}
    else {{
      var layout = cy.layout({{ name: 'cose', animate: false, padding: 45, nodeRepulsion: 12000,
        idealEdgeLength: 110, nodeOverlap: 24, componentSpacing: 90, gravity: 0.2,
        nodeDimensionsIncludeLabels: true }});
      // P8.7.1 #3 — make readiness robust on a DENSE graph: cose with animate:false is a SYNCHRONOUS,
      // event-loop-blocking run, so (a) ARM the hard-timeout fallback FIRST (always < the engine's
      // virtual-time budget) so a never-settling layout still prints something, then (b) DEFER the
      // blocking run off this turn (so the timer is registered before it starts) and set ready in a
      // finally — guaranteeing __CY_READY__ the instant the synchronous run returns, even if the
      // 'layoutstop' event never fires or the layout throws.
      setTimeout(done, 8000);
      layout.one('layoutstop', done);
      setTimeout(function () {{ try {{ layout.run(); }} finally {{ done(); }} }}, 0);
    }}
  </script>
</body></html>"""


# --------------------------------------------------------------------------- hash + persistence

def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _scope_from_view(vp: dict, meta: dict) -> dict:
    """An honest scope_spec from the rendered VIEW (P8.7.1 #2): every applied filter + the COUNTS HIDDEN,
    so the exhibit is reproducible and explicit about what's shown vs omitted."""
    h = meta.get("hidden", {}) or {}
    scope = {
        "selection": "current-view",
        "focus": meta.get("focus_label") or vp.get("focus"),
        "hops": vp.get("hops"),
        "value_basis": meta.get("value_basis"),
        "displayed": meta.get("displayed"),
        "total": meta.get("total"),
        "bounded": meta.get("bounded"),
        "group_dust": vp.get("group_dust"),
        "group_denominations": vp.get("group_denominations"),
        "denomination_groups": meta.get("denomination_groups"),
        "show_unverified": vp.get("show_unverified"),
        "fold_poison": vp.get("fold_poison"),
        "hidden": {
            "dust_folded": h.get("dust", 0),
            "value_filtered": h.get("user_dust", 0),
            "unverified_collapsed": h.get("unverified", 0),
            "poison_folded": h.get("poison", 0),
        },
    }
    for k in ("value_floor_usd", "user_dust_usd", "node_cap"):
        if vp.get(k):
            scope[k] = vp[k]
    if vp.get("only_flagged"):
        scope["only_flagged"] = True
    if vp.get("denom_filters"):
        scope["denom_filters"] = vp["denom_filters"]
    return scope


def generate_report(conn, *, case_dir, title: str, scope_spec: dict | None = None,
                    view_params: dict | None = None,
                    supersedes_report_id: str | None = None, generated_at: str | None = None,
                    now: str | None = None, render_pdf: bool = True) -> dict:
    """Write a report's self-contained HTML, render its PDF (when an engine is available), and append
    an immutable ``report`` row.

    ``case_dir`` is the case folder (parent of case.db); files land under ``case_dir/reports/``.
    ``scope_spec`` records the applied expansion bounds (defaults to a 'full case' marker).

    The ``content_hash`` is taken over the canonical **HTML** (the reproducible artifact); the row's
    ``rendered_file_ref`` is the HTML, which always exists. The PDF is rendered by the OS browser engine
    when one is present (``report_render``); when absent (``render_pdf=False`` or no engine) the report
    is still complete — ``pdf_path`` is ``None`` and ``engine`` reports why. Returns
    ``{report_id, html_path, pdf_path, content_hash, engine}``.
    """
    from . import report_render

    case_dir = Path(case_dir)
    generated_at = generated_at or utc_now_iso()

    # P8.7.1 #2 — render the investigator's CURRENT bounded view (build_view) when view params are given,
    # and derive an honest scope_spec from it; else the full case graph + a 'full-case' marker (back-compat).
    graph = None
    if view_params:
        from .graph_view import build_view

        graph = build_view(conn, **view_params)
        scope_spec = scope_spec or _scope_from_view(view_params, graph.get("meta", {}))
    scope_spec = scope_spec or {"selection": "full-case", "bounds": "as-ingested",
                                "note": "no scope filter applied"}

    ctx = build_report_context(conn, title=title, scope_spec=scope_spec, generated_at=generated_at,
                               graph=graph)
    page_html = render_html(ctx)

    reports_dir = case_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = Report(title=title, scope_spec=scope_spec, rendered_file_ref="",
                    content_hash="", supersedes_report_id=supersedes_report_id)
    html_path = reports_dir / f"{report.id}.html"
    pdf_path = reports_dir / f"{report.id}.pdf"
    html_path.write_text(page_html, encoding="utf-8")

    # Freeze the hash over the HTML SOURCE (engine-independent), and point the row at the HTML — the
    # always-present, hash-matching artifact (Phase-10 export + cross-machine re-verify key off it).
    report.content_hash = _sha256_text(page_html)
    report.rendered_file_ref = f"reports/{report.id}.html"  # relative — portable with the case

    # The PDF is a convenience artifact. A missing/failed engine is NOT an error — keep the HTML-only
    # report and surface WHY the PDF was skipped (so the CLI can report it honestly).
    engine: str | None = None
    pdf_skip_reason: str | None = None
    if render_pdf:
        try:
            engine = report_render.render_pdf(html_path, pdf_path)
        except report_render.NoRendererError as exc:
            pdf_path = None
            pdf_skip_reason = str(exc)
    else:
        pdf_path = None
        pdf_skip_reason = "PDF rendering not requested"

    repo.insert_report(conn, report, now=now or generated_at)

    return {"report_id": report.id, "html_path": html_path, "pdf_path": pdf_path,
            "content_hash": report.content_hash, "engine": engine,
            "pdf_skip_reason": pdf_skip_reason}
