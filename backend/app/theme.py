"""Theme — the BACKEND half of the single color-token catalog (frontend/src/theme/tokens.json).

The report renders the SAME encodings as the live graph, but it lives in a different runtime (Python +
Playwright), so it can't import the TS theme module. Instead it reads the SAME canonical ``tokens.json``
and exposes:

  - ``css_root_block()`` — a ``:root { --bih-...: <value>; }`` block injected ahead of ``report.css`` so
    the report stylesheet's ``var(--bih-...)`` references resolve (no hardcoded hex in report.css).
  - ``cytoscape_style()`` — the report's inlined Cytoscape stylesheet, MIRRORING
    ``theme.ts::buildCytoscapeStyle`` so the report's graph matches the app's (address/tx rims, risk halo
    + badge, entity ring, possible-coinjoin marker, seed corner badge, FIFO-trace-as-convention edges,
    provisional dashing). Only the colors are centralized across the runtime boundary; keep this
    structure in lockstep with the TS twin.

NAMED THEMES: ``tokens.json`` defines two themes sharing the same token ids — ``neo-tokyo-night`` (the
app's dark canvas) and ``print-light``. The report INTENTIONALLY resolves ``print-light`` so exported
case files stay ink-light and legible on paper, regardless of the (dark) theme the live app shows.

Single source of truth: ``tokens.json``. The future color-customization UI selects a theme and overrides
token values there.
"""

from __future__ import annotations

import functools
import json
import re
from urllib.parse import quote

from .app_paths import resource_path

# --- SEC-12: <style>-interpolation safety -----------------------------------------------------------
# Every token value is emitted verbatim into the report's inline `<style>` (css_root_block). Bundled
# tokens are all hex colors or sizing numerics, but the planned customize-UI "will override token values"
# (module docstring) — so any value bound for `<style>` is validated so it cannot break out of the style
# element (</style>), open a nested rule/at-rule, or inject a url()/expression() sink.
_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")
_FUNC_COLOR_RE = re.compile(r"(?:rgb|rgba|hsl|hsla)\(\s*[0-9.,%/\s]+\)\Z", re.IGNORECASE)
_NAMED_COLOR_RE = re.compile(r"[a-zA-Z]{1,40}\Z")
_CSS_BREAKOUT_RE = re.compile(r"[<>{};]|</?style|url\(|expression\(|@import|/\*", re.IGNORECASE)


def validate_color_value(value) -> bool:
    """Strict CSS-color grammar for a value bound for the report's inline ``<style>`` (SEC-12). Accepts a
    hex color, ``rgb()``/``rgba()``/``hsl()``/``hsla()``, or a bare CSS color name; rejects anything that
    could escape the style context (``</style>``, ``;``, ``}``, ``url(...)``, ``expression(...)``, angle
    brackets). The customize-UI must run user color overrides through this before they reach the report."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or len(v) > 64:
        return False
    return bool(_HEX_COLOR_RE.match(v) or _FUNC_COLOR_RE.match(v) or _NAMED_COLOR_RE.match(v))


def _css_value_safe(value) -> str:
    """A token value bound for the report's inline ``<style>`` must not carry a CSS/style breakout.
    Bundled tokens are all hex/sizing numerics (safe); this fails LOUD if a future/overridden value could
    escape the ``<style>`` element (SEC-12) rather than emitting a poisoned stylesheet into a court
    artifact."""
    s = str(value)
    if _CSS_BREAKOUT_RE.search(s):
        raise ValueError(f"unsafe token value for <style> interpolation: {value!r}")
    return s

# tokens.json — a bundled READ-ONLY resource (P7): the single color catalog the report's print-light set
# also reads. _MEIPASS/... when frozen, else repo-root/... in source (via resource_path).
_TOKENS_PATH = resource_path("frontend/src/theme/tokens.json")

# The report always renders the print-light theme (ink-light, paper-legible) — NOT the app's dark theme.
_REPORT_THEME = "print-light"


@functools.lru_cache(maxsize=4)
def theme_tokens(theme: str) -> dict[str, str]:
    """All token ids -> color value for a NAMED theme (e.g. ``neo-tokyo-night`` for the launcher
    splash, ``print-light`` for the report). Resolves from the SAME single source of truth
    (``tokens.json``) so non-report consumers never hardcode hex either."""
    if not _TOKENS_PATH.exists():
        raise FileNotFoundError(
            f"theme token catalog not found at {_TOKENS_PATH} — the report shares colors with the "
            "frontend's tokens.json (single source of truth).")
    doc = json.loads(_TOKENS_PATH.read_text(encoding="utf-8"))
    # Each token carries a per-theme `values` map; fall back to the first value if a theme is missing.
    return {tk["id"]: (tk["values"].get(theme) or next(iter(tk["values"].values())))
            for tk in doc["tokens"]}


@functools.lru_cache(maxsize=1)
def _tokens() -> dict[str, str]:
    # The report resolves the print-light value of every token.
    return theme_tokens(_REPORT_THEME)


def seed_badge_image() -> str:
    """A ★ seed/anchor corner-badge as an SVG data-URI, built from catalog tokens (mirrors
    theme.ts::seedBadgeImage). Drawn on the node glyph so the seed marker stays OFF the text label."""
    fill = token("node.seed.marker")
    stroke = token("ui.text")
    # Explicit width/height + a padded 32x32 viewBox (star translated to centre) so background-fit:contain
    # can size it and it never clips — mirrors theme.ts::seedBadgeImage.
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32' viewBox='0 0 32 32'>"
        "<g transform='translate(4,5)'>"
        "<path d='M12 2.2 L14.7 8.6 L21.6 9.1 L16.3 13.6 L18 20.3 L12 16.7 L6 20.3 L7.7 13.6 L2.4 9.1 L9.3 8.6 Z' "
        f"fill='{fill}' stroke='{stroke}' stroke-width='1.4' stroke-linejoin='round'/></g></svg>")
    return f"data:image/svg+xml;utf8,{quote(svg, safe='')}"


def token(token_id: str) -> str:
    """Resolve a token id to its color value (throws on unknown — no silent fallback)."""
    try:
        return _tokens()[token_id]
    except KeyError as exc:
        raise KeyError(f"unknown theme token: {token_id}") from exc


def dimension(token_id: str, default: float) -> float:
    """Resolve a SIZING token (e.g. ``edge.thickness.min``) to a float. Sizing tokens are theme-
    independent (the same value under every theme) but live in the catalog so the P6 customize UI can
    expose them alongside colors. Falls back to ``default`` if absent/unparseable (never throws — a
    missing sizing token must not break rendering)."""
    try:
        return float(_tokens()[token_id])
    except (KeyError, TypeError, ValueError):
        return default


def css_var_name(token_id: str) -> str:
    """node.address.fill -> --bih-node-address-fill (mechanical: dots -> dashes, '--bih-' prefix)."""
    return f"--bih-{token_id.replace('.', '-')}"


def css_root_block() -> str:
    """Every token as a CSS custom property under ``:root`` (consumed by report.css's var(--bih-...))."""
    lines = "\n".join(f"  {css_var_name(tid)}: {_css_value_safe(value)};" for tid, value in _tokens().items())
    return f":root {{\n{lines}\n}}\n"


def cytoscape_style() -> list[dict]:
    """The report's Cytoscape stylesheet — mirrors frontend/src/theme/theme.ts::buildCytoscapeStyle so
    the report's graph renders the same intelligence as the live canvas. Colors from the catalog."""
    t = token
    return [
        {"selector": "node", "style": {
            "label": "data(label)", "color": t("node.label.color"), "font-size": 11,
            "text-valign": "bottom", "text-halign": "center", "text-margin-y": 4, "text-wrap": "wrap",
            "text-max-width": 120, "line-height": 1.25,
            "text-background-color": t("node.label.bg"), "text-background-opacity": 0.85,
            "text-background-padding": 2, "text-background-shape": "roundrectangle",
            "min-zoomed-font-size": 5}},
        {"selector": 'node[kind="address"]', "style": {
            "shape": "ellipse", "background-color": t("node.address.fill"), "width": 30, "height": 30,
            "border-width": 1.5, "border-color": t("node.address.rim")}},
        {"selector": 'node[kind="transaction"]', "style": {
            "shape": "round-rectangle", "background-color": t("node.transaction.fill"),
            "width": 40, "height": 22, "border-width": 1.5, "border-color": t("node.transaction.rim")}},
        {"selector": 'node[kind="external"]', "style": {
            "shape": "diamond", "background-color": t("node.external.fill"), "width": 18, "height": 18}},
        {"selector": 'node[kind="group"]', "style": {
            "shape": "round-rectangle", "text-valign": "top", "text-halign": "center", "font-size": 11,
            "padding": 14, "color": t("group.label.color"), "background-opacity": 0.55}},
        {"selector": 'node[group_type="cospend"]', "style": {
            "background-color": t("group.cospend.fill"), "border-width": 1.5,
            "border-color": t("group.cospend.border")}},
        {"selector": 'node[group_type="entity"]', "style": {
            "background-color": t("group.entity.fill"), "border-width": 1.5,
            "border-color": t("group.entity.border")}},
        {"selector": 'node[group_type="denomination"]', "style": {
            "background-color": t("group.denomination.fill"), "border-width": 1.5,
            "border-color": t("group.denomination.border")}},
        # P8.8 Leiden community — VISUAL structure only (dashed violet box); mirrors theme.ts.
        {"selector": 'node[group_type="community"]', "style": {
            "background-color": t("group.community.fill"), "border-width": 1.5, "border-style": "dashed",
            "border-color": t("group.community.border")}},
        # P8.7.1 #6 — `z-compound-depth: top` lifts a flagged node ABOVE its parent group box so its
        # halo/ring composites against the canvas (a sanctioned node inside a denomination pool keeps its
        # red halo + badge). Mirrors theme.ts; the report twin must stay in lockstep.
        {"selector": "node[?has_attribution]", "style": {
            "border-width": 3, "border-color": t("node.entity.ring"),
            "text-outline-color": t("node.entity.ring"), "text-outline-width": 1,
            "z-compound-depth": "top"}},
        {"selector": "node[?coinjoin]", "style": {
            "border-width": 3, "border-color": t("node.flag.coinjoin.ring"), "border-style": "dashed"}},
        {"selector": "node[?has_annotation]", "style": {
            "outline-color": t("node.annotation.ring"), "outline-width": 3, "outline-offset": 2}},
        {"selector": 'node[risk_level="elevated"]', "style": {
            "underlay-color": t("node.risk.elevated.halo"), "underlay-padding": 6,
            "underlay-opacity": 0.4, "z-compound-depth": "top"}},
        {"selector": 'node[risk_level="sanctioned"]', "style": {
            "underlay-color": t("node.risk.sanctioned.halo"), "underlay-padding": 8,
            "underlay-opacity": 0.5, "border-width": 3, "border-color": t("node.risk.sanctioned.badge"),
            "border-style": "solid", "z-compound-depth": "top"}},
        # Seed ★ marker CENTERED on the node (50%/50%) and SCALED to fit via background-fit:contain so it is
        # never cut off at the node edge — mirrors theme.ts.
        {"selector": "node[?seed]", "style": {
            "background-image": seed_badge_image(), "background-image-containment": "over",
            "background-clip": "none", "background-fit": "contain", "background-repeat": "no-repeat",
            "background-width": 24, "background-height": 24,
            "background-position-x": "50%", "background-position-y": "50%"}},
        {"selector": "edge", "style": {
            "width": 1.6, "line-color": t("edge.default.line"),
            "target-arrow-color": t("edge.default.line"), "target-arrow-shape": "triangle",
            "curve-style": "bezier", "arrow-scale": 0.85}},
        # P35/UX-02 — a SECOND, COLOR-INDEPENDENT channel mirrored from theme.ts::buildCytoscapeStyle: each
        # fact-edge kind carries a DISTINCT target-arrow shape (triangle = EVM transfer · tee/bar = BTC input ·
        # vee = BTC output) so the three read apart in the grayscale/paper report, not by hue alone. tx_input is
        # also shifted off the amber band (tokens.json). Keep in lockstep with the TS twin.
        {"selector": 'edge[kind="transfer"]', "style": {
            "line-color": t("edge.transfer.line"), "target-arrow-color": t("edge.transfer.line"),
            "target-arrow-shape": "triangle"}},
        {"selector": 'edge[kind="tx_input"]', "style": {
            "line-color": t("edge.tx_input.line"), "target-arrow-color": t("edge.tx_input.line"),
            "target-arrow-shape": "tee"}},
        {"selector": 'edge[kind="tx_output"]', "style": {
            "line-color": t("edge.tx_output.line"), "target-arrow-color": t("edge.tx_output.line"),
            "target-arrow-shape": "vee"}},
        {"selector": "edge[?value_label]", "style": {
            "width": "data(ew)", "label": "data(value_label)", "font-size": 7,
            "color": t("edge.value.label"), "text-background-color": t("edge.value.labelBg"),
            "text-background-opacity": 0.85, "text-background-padding": 2,
            "text-background-shape": "roundrectangle", "text-rotation": "autorotate",
            "min-zoomed-font-size": 8}},
        # USD value-at-time wins the edge label when present (mirrors the live canvas).
        {"selector": "edge[?value_usd_label]", "style": {
            "label": "data(value_usd_label)", "font-size": 7, "color": t("edge.value.label"),
            "text-background-color": t("edge.value.labelBg"), "text-background-opacity": 0.85,
            "text-background-padding": 2, "text-background-shape": "roundrectangle",
            "text-rotation": "autorotate", "min-zoomed-font-size": 8}},
        # An investigator RENAME of a flow wins the edge label (drawn after the value rules).
        {"selector": "edge[?custom_label]", "style": {
            "label": "data(custom_label)", "font-size": 7, "color": t("edge.value.label"),
            "text-background-color": t("edge.value.labelBg"), "text-background-opacity": 0.85,
            "text-background-padding": 2, "text-background-shape": "roundrectangle",
            "text-rotation": "autorotate", "min-zoomed-font-size": 6}},
        # An ANNOTATED flow gets a green glow (the edge analogue of the annotated-node emerald outline).
        {"selector": "edge[?has_annotation]", "style": {
            "underlay-color": t("edge.annotation.glow"), "underlay-padding": 3,
            "underlay-opacity": 0.45}},
        {"selector": 'edge[trace="fifo"]', "style": {
            "line-color": t("edge.trace.fifo.line"), "target-arrow-color": t("edge.trace.fifo.line"),
            "line-style": "dashed", "line-dash-pattern": [12, 4], "width": 2.4, "label": "fifo", "font-size": 8,
            "color": t("edge.trace.fifo.label"), "text-background-color": t("edge.trace.fifo.labelBg"),
            "text-background-opacity": 0.85, "text-background-padding": 2,
            "text-background-shape": "roundrectangle", "text-rotation": "autorotate",
            "text-margin-y": "data(label_dy)", "min-zoomed-font-size": 6}},
        {"selector": 'edge[trace="investigator"]', "style": {
            "line-color": t("edge.trace.investigator.line"),
            "target-arrow-color": t("edge.trace.investigator.line"), "line-style": "dashed",
            "line-dash-pattern": [12, 4, 2, 4], "width": 2.4, "label": "manual", "font-size": 8,
            "color": t("edge.trace.investigator.line"), "text-rotation": "autorotate",
            "text-margin-y": "data(label_dy)", "min-zoomed-font-size": 6}},
        # P36/UX-04 — mirror the frontend dash-MEANING families (theme.ts): provisional (finality) = fine dots
        # [1,4]; fifo/investigator (trace convention) = long dashes above. Named in the report legend so a
        # dashed edge decodes on paper without guessing. Keep in lockstep with buildCytoscapeStyle.
        {"selector": 'edge[finality_status="provisional"]', "style": {
            "line-style": "dashed", "line-dash-pattern": [1, 4], "opacity": 0.55}},
        {"selector": 'node[finality_status="provisional"]', "style": {
            "border-width": 2, "border-color": t("node.provisional.border"), "border-style": "dashed",
            "background-opacity": 0.5}},
        # Focus/dim classes — inert in the static report (no interaction), kept for parity with the twin.
        {"selector": ".bih-faded", "style": {"opacity": 0.12, "text-opacity": 0.12}},
        {"selector": "edge.bih-focus", "style": {"opacity": 1, "width": 5}},
        {"selector": "node.bih-focus", "style": {"opacity": 1, "underlay-opacity": 0.35}},
        {"selector": "node:selected", "style": {
            "border-width": 4, "border-color": t("node.selected.border")}},
    ]


def cytoscape_style_json() -> str:
    return json.dumps(cytoscape_style())
