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
    from .tracing import trace_btc_links
    # The display name = the trace's name, overridden by the investigator's latest custom label
    # (feature 5; migration 0008) — so a renamed path reads the same in the report as on the graph.
    custom = current_labels(conn, "trace")
    traces = []
    for t in conn.execute("SELECT id, name, description FROM trace ORDER BY created_at, id").fetchall():
        links = trace_btc_links(conn, t["id"])
        transfers = conn.execute(
            "SELECT transfer_id, ordering, note FROM trace_transfer WHERE trace_id=? ORDER BY ordering, id",
            (t["id"],)).fetchall()
        traces.append({"name": custom.get(t["id"]) or t["name"], "description": t["description"],
                       "btc_links": links, "transfers": [dict(r) for r in transfers]})
    return traces


def _collect_findings(conn) -> list[dict]:
    # Enriched with a readable label per ref (shared with the live composer's /api/findings).
    from .investigator import list_findings
    return list_findings(conn)


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


def build_report_context(conn, *, title: str, scope_spec: dict, generated_at: str,
                         graph: dict | None = None) -> dict:
    case = conn.execute("SELECT id, title, description FROM case_meta LIMIT 1").fetchone()
    return {
        "title": title,
        "case": dict(case) if case else {"title": "(uninitialized case)"},
        "generated_at": generated_at,
        "scope_spec": scope_spec,
        # P8.7.1 #2 — the report renders the investigator's CURRENT bounded VIEW when one is supplied
        # (focus/hops/dust/denominations/spam-collapse/poison-fold/value_basis), else the full case graph.
        "graph": graph if graph is not None else build_graph(conn),
        "traces": _collect_traces(conn),
        "findings": _collect_findings(conn),
        "notes": _collect_notes(conn),
        "risk": _collect_risk(conn),
        "entities": _collect_entities(conn),
        "valuation": _valuation_honesty(conn),
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
        if not t["btc_links"] and not t["transfers"]:
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


def render_html(ctx: dict) -> str:
    from ..theme import css_root_block, cytoscape_style_json

    css = (_TEMPLATES / "report.css").read_text(encoding="utf-8")
    cyjs = (_TEMPLATES / "cytoscape.min.js").read_text(encoding="utf-8")
    elements = [{"data": n} for n in ctx["graph"]["nodes"]] + \
               [{"data": e} for e in ctx["graph"]["edges"]]
    case = ctx["case"]
    scope_json = json.dumps(ctx["scope_spec"], indent=2, sort_keys=True)
    # The token catalog as :root custom properties, ahead of report.css (which uses var(--bih-...)) — the
    # single source of truth shared with the live graph (no hardcoded hex in report.css).
    theme_vars = css_root_block()

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{_esc(ctx['title'])}</title>
<style>{theme_vars}{css}</style></head>
<body>
  <h1>{_esc(ctx['title'])}</h1>
  <p class="sub">Case: {_esc(case.get('title'))} &middot; generated {_esc(ctx['generated_at'])}</p>

  <div class="caveat">
    <b>Read this first.</b> This report is a <b>frozen snapshot</b> generated at the local-clock time
    above; on-chain state may have changed since. It reflects only data acquired under the
    <b>applied bounds</b> below and therefore does <b>not</b> imply completeness. Provisional (tip)
    facts are drawn dashed/faded. Source claims that disagree are shown side-by-side, never merged.
    Bitcoin input&rarr;output links are a labeled tracing <b>convention</b> (FIFO) or an explicit
    investigator assertion — never ground-truth flow.
  </div>

  <h2>Graph</h2>
  <div id="cy"></div>
  <div class="legend">
    <span>&#9679; address</span><span>&#9646; bitcoin transaction (routing)</span>
    <span>&#9670; external (mint/burn/coinbase)</span><span>green = transfer</span>
    <span>brown = tx input</span><span>red = tx output</span><span>dashed = provisional</span>
    <span>&#9210; red halo = sanctioned/risk</span><span>teal ring = attributed entity</span>
    <span>&#9888; possible-coinjoin</span><span>blue dashed = FIFO trace (convention)</span>
    <span>&#9733; seed / anchor</span><span>edge label = value; width &prop; value</span>
  </div>

  <h2>Risk &amp; sanctions</h2>{_risk_section(ctx['risk'])}
  <h2>Traces</h2>{_trace_section(ctx['traces'])}
  <h2>Findings</h2>{_findings_section(ctx['findings'])}
  <h2>Investigator notes</h2>{_notes_section(ctx['notes'])}
  <h2>Entities</h2>{_entities_section(ctx['entities'])}
  <h2>Valuation coverage</h2>{_valuation_section(ctx['valuation'])}

  <h2>Scope &amp; applied bounds</h2>
  <pre class="mono">{_esc(scope_json)}</pre>

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
