// BIH theme — the frontend half of the single color-token catalog (tokens.json).
//
// THIS is the only place the Cytoscape stylesheet gets its colors; every color resolves through a
// catalog token (no hardcoded hex in Graph.tsx / App.tsx / SidePanel.tsx). The report renders the SAME
// encodings from the Python twin (backend/app/theme.py) reading the SAME tokens.json — keep the two
// style builders' STRUCTURE in sync (only colors are centralized across the runtime boundary; the
// selector shapes are mirrored).
//
// CANVAS THEMES (P6): tokens.json carries four theme value-sets sharing the same token ids — the editable
// Custom base `neo-tokyo-night`, the locked `dark` + `light` modern themes, and the report's `print-light`
// (the canvas NEVER follows it). The app renders one of three CANVAS PRESETS at runtime — `custom` (=
// neo-tokyo base + user overrides), `dark`, `light` — switchable instantly; the report/exhibit always
// resolve print-light. The active preset + Custom overrides persist in localStorage (survive sessions, no
// boot flash). Only Custom is editable; dark/light are locked.

import catalog from "./tokens.json";

type RawToken = { id: string; label: string; category: string; values: Record<string, string> };
export type ThemeMeta = { id: string; label: string; description?: string };
export type ThemeToken = RawToken & { value: string };  // `value` = the Custom-base (neo-tokyo) default
export type PresetMeta = { id: string; label: string; base: string; locked: boolean };
export type CanvasPreset = "custom" | "dark" | "light";

const RAW: RawToken[] = catalog.tokens as RawToken[];
export const THEMES: ThemeMeta[] = catalog.themes as ThemeMeta[];
export const DEFAULT_THEME: string = catalog.defaultTheme;

// The three switchable canvas presets (Dark · Light · Custom). `base` = the catalog theme key the preset
// resolves; `locked` presets are read-only (only Custom is user-editable).
export const CANVAS_PRESETS: PresetMeta[] = catalog.canvasPresets as PresetMeta[];
const PRESET_META: Record<string, PresetMeta> = Object.fromEntries(CANVAS_PRESETS.map((p) => [p.id, p]));
export const CUSTOM_BASE = "neo-tokyo-night";  // the Custom preset's base value-set

export function isLockedPreset(preset: string): boolean {
  return !!PRESET_META[preset]?.locked;
}

/** Resolve a token's value under a RAW catalog theme key (neo-tokyo-night / dark / light / print-light). */
export function themeValue(id: string, theme: string): string {
  const tk = RAW.find((t) => t.id === id);
  if (!tk) throw new Error(`unknown theme token: ${id}`);
  const v = tk.values[theme];
  if (v == null) throw new Error(`token ${id} has no value for theme ${theme}`);
  return v;
}

// --- runtime active preset + Custom overrides (persisted in localStorage) ---------------------
const LS_PRESET = "bih.themePreset";
const LS_OVERRIDES = "bih.themeOverrides";

function _readLS(key: string): string | null {
  try { return typeof localStorage !== "undefined" ? localStorage.getItem(key) : null; } catch { return null; }
}
function _writeLS(key: string, value: string): void {
  try { if (typeof localStorage !== "undefined") localStorage.setItem(key, value); } catch { /* ignore */ }
}
function _initPreset(): CanvasPreset {
  const v = _readLS(LS_PRESET);
  return v === "dark" || v === "light" || v === "custom" ? v : "custom";
}
function _initOverrides(): Record<string, string> {
  const raw = _readLS(LS_OVERRIDES);
  if (!raw) return {};
  try { const o = JSON.parse(raw); return o && typeof o === "object" ? (o as Record<string, string>) : {}; }
  catch { return {}; }
}

let _activePreset: CanvasPreset = _initPreset();
let _overrides: Record<string, string> = _initOverrides();

let _version = 0;
const _subs = new Set<() => void>();
function _bump(): void { _version += 1; _subs.forEach((cb) => cb()); }

/** Subscribe to theme changes (React useSyncExternalStore — re-render on switch/edit). Returns unsub. */
export function subscribeTheme(cb: () => void): () => void { _subs.add(cb); return () => { _subs.delete(cb); }; }
/** A snapshot that changes whenever the active preset or Custom overrides change. */
export function getThemeSnapshot(): number { return _version; }

export function getActivePreset(): CanvasPreset { return _activePreset; }
export function activeTheme(): string { return _activePreset; }

/** Switch the active canvas preset (instant + persisted). Throws on an unknown preset. */
export function setActivePreset(preset: CanvasPreset): void {
  if (!PRESET_META[preset]) throw new Error(`unknown canvas preset: ${preset}`);
  _activePreset = preset;
  _writeLS(LS_PRESET, preset);
  _bump();
}

export function getCustomOverrides(): Record<string, string> { return { ..._overrides }; }
export function hasOverride(id: string): boolean {
  return Object.prototype.hasOwnProperty.call(_overrides, id);
}

// Set ONE Custom-preset override (instant + persisted). REJECTED when a LOCKED preset (dark/light) is
// active — only Custom is editable (the customize editor is disabled for locked presets). Editing implies
// the Custom preset; switch to it first.
export function setCustomOverride(id: string, value: string): void {
  if (isLockedPreset(_activePreset))
    throw new Error(`the "${_activePreset}" preset is locked — switch to Custom to edit colors`);
  if (!_byId[id]) throw new Error(`unknown theme token: ${id}`);
  _overrides = { ..._overrides, [id]: value };
  _writeLS(LS_OVERRIDES, JSON.stringify(_overrides));
  _bump();
}

/** Clear ONE Custom override (that token reverts to its Neo-Tokyo default). Rejected on a locked preset. */
export function clearCustomOverride(id: string): void {
  if (isLockedPreset(_activePreset))
    throw new Error(`the "${_activePreset}" preset is locked — switch to Custom to edit colors`);
  if (!(id in _overrides)) return;
  const next = { ..._overrides };
  delete next[id];
  _overrides = next;
  _writeLS(LS_OVERRIDES, JSON.stringify(_overrides));
  _bump();
}

/** Clear ALL Custom overrides (back to the Neo-Tokyo defaults). */
export function resetCustomOverrides(): void {
  _overrides = {};
  _writeLS(LS_OVERRIDES, JSON.stringify(_overrides));
  _bump();
}

/** The Custom preset's DEFAULT (Neo-Tokyo) value for a token — what "reset this token" restores. */
export function customDefault(id: string): string { return themeValue(id, CUSTOM_BASE); }

// Resolve a token id under a preset (or a raw catalog key). For the Custom preset a user override wins
// over the Neo-Tokyo base; dark/light/print-light resolve their own value-set (overrides never apply).
function resolve(id: string, theme: string, overrides?: Record<string, string>): string {
  if (theme === "custom") {
    const ov = (overrides ?? _overrides)[id];
    if (ov != null && ov !== "") return ov;
    return themeValue(id, CUSTOM_BASE);
  }
  const base = PRESET_META[theme]?.base ?? theme;  // preset -> base key; a raw key passes through
  return themeValue(id, base);
}

export const CATALOG: ThemeToken[] = RAW.map((tk) => ({ ...tk, value: themeValue(tk.id, CUSTOM_BASE) }));
const _byId: Record<string, ThemeToken> = Object.fromEntries(CATALOG.map((t) => [t.id, t]));

/** Resolve a token id to its color value under the ACTIVE canvas preset (incl. Custom overrides). */
export function t(id: string): string { return resolve(id, _activePreset, _overrides); }
/** The current resolved value of a token (alias of t, for the customize UI). */
export function currentColor(id: string): string { return t(id); }

/** Token id -> CSS custom-property name (node.address.fill -> --bih-node-address-fill). */
export function cssVarName(id: string): string { return `--bih-${id.replace(/\./g, "-")}`; }

/** All tokens as a {--bih-...: value} map under a preset (default: the active preset). */
export function cssVars(theme: string = _activePreset): Record<string, string> {
  return Object.fromEntries(RAW.map((tk) => [cssVarName(tk.id), resolve(tk.id, theme, _overrides)]));
}

/** Apply every token as a CSS custom property on an element under the active preset. */
export function applyThemeVars(el: HTMLElement = document.documentElement, theme: string = _activePreset): void {
  for (const [name, value] of Object.entries(cssVars(theme))) el.style.setProperty(name, value);
}

/** Per-source badge color (resolves under the active preset). */
export function sourceColor(source: string): string {
  const id = `source.${source}`;
  return _byId[id] ? t(id) : t("source.default");
}

// A ★ seed/anchor CORNER BADGE drawn on the node glyph (not crammed into the wrapping text label). The
// SVG is built from catalog tokens (fill = seed marker, stroke = UI text so it reads on either theme),
// so there is still no hardcoded hex — the color comes from the catalog. The Python twin builds the
// same data-URI from print-light tokens for the report.
export function seedBadgeImage(theme: string = _activePreset): string {
  const fill = resolve("node.seed.marker", theme);
  const stroke = resolve("ui.text", theme);
  // EXPLICIT width/height (so the image has a defined intrinsic size — without it `background-fit:contain`
  // can't size it and the marker clipped) + a PADDED 32×32 viewBox with the 24-unit star translated to the
  // centre, so even scaled to fill the node the star keeps a margin and sits cleanly inside the circle.
  const svg =
    "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32' viewBox='0 0 32 32'>" +
    "<g transform='translate(4,5)'>" +
    "<path d='M12 2.2 L14.7 8.6 L21.6 9.1 L16.3 13.6 L18 20.3 L12 16.7 L6 20.3 L7.7 13.6 L2.4 9.1 L9.3 8.6 Z' " +
    `fill='${fill}' stroke='${stroke}' stroke-width='1.4' stroke-linejoin='round'/></g></svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

// A strikethrough glyph drawn on a node that ordering left UNORDERED (its connecting edge had no key —
// unpriced for "order by value", NULL-height/mempool for "order by sequence"). Built from the catalog
// (stroke = the ordering-tray token), so there's no hardcoded hex. Shown only while ordering is active.
export function strikeBadgeImage(theme: string = _activePreset): string {
  const stroke = resolve("node.ordering.tray", theme);
  const svg =
    "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32' viewBox='0 0 32 32'>" +
    `<line x1='5' y1='27' x2='27' y2='5' stroke='${stroke}' stroke-width='3.4' stroke-linecap='round'/></svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

// --- Cytoscape stylesheet (built from tokens) ------------------------------------------------
// Node data carries SUMMARY flags from the read-model (services/graph.py): kind, finality_status,
// risk_level ('sanctioned' | 'elevated'), has_attribution, entity_label, coinjoin, seed, and — for
// compound grouping parents — kind='group' + group_type ('cospend' | 'entity'). Trace edges carry a
// `label_dy` (read-model) used to stagger their labels. The order matters: later rules win for the same
// property, so status rings/risk/selection ride on top of the base address/tx rim.
export function buildCytoscapeStyle(fontScale: number = 1, theme: string = _activePreset,
                                    sizeScale: number = 1): any[] {
  // Graph-label font multiplier (feature 5): scales every node/edge label font-size INDEPENDENTLY of the
  // scroll-wheel zoom (which scales the whole canvas). A view-only UI pref — never a case row. Default 1
  // (the report twin renders unscaled). Rounded to 0.1px so the stylesheet stays stable/diff-friendly.
  const fs = (n: number): number => Math.round(n * fontScale * 10) / 10;
  // Global ICON/PATH multiplier (P3.5 feature 1): when ordering is active the layout auto-sizes icons +
  // paths to fit. A SINGLE multiplier kept ORTHOGONAL to the value→thickness mapping (feature 3, which is
  // data(ew)). sizeScale=1 (the default + the report) leaves every size byte-identical to before; a value
  // ≠ 1 scales node bodies + edge widths. `sz` rounds to 0.1px but is an identity at scale 1.
  const sz = (n: number): number => (sizeScale === 1 ? n : Math.round(n * sizeScale * 10) / 10);
  // Edge width driven by value (data(ew), the read-model's per-view thickness) × the global sizeScale.
  // At sizeScale=1 this stays the literal "data(ew)" string (existing behavior + the report); only when
  // ordering scales the layout do we switch to a mapping function that multiplies the per-edge ew.
  const ewWidth: any = sizeScale === 1
    ? "data(ew)"
    : (e: any) => (Number(e.data("ew")) || 1.8) * sizeScale;
  // Resolve every color through the catalog for the requested THEME (default = the active dark theme).
  // A local shadow of the module-level `t` so the existing `t("…")` calls below become theme-aware with
  // no other change — this is what lets a standalone image export render the print-light exhibit palette.
  const t = (id: string): string => resolve(id, theme);
  return [
    { selector: "node", style: {
        label: "data(label)", color: t("node.label.color"),
        // Labels sit BELOW the node so the glyph (with its risk halo/badge + status rings + seed corner
        // badge) is never hidden under text. Status markers live ON the glyph (rings/halo/badge), NOT in
        // the text — the label is a clean entity-over-alias (capped to 2 lines in the read-model). A
        // subtle plate keeps the two lines legible over the neon canvas; wrap + tight max-width stop sprawl.
        "font-size": fs(11), "text-valign": "bottom", "text-halign": "center", "text-margin-y": 4,
        "text-wrap": "wrap", "text-max-width": 120, "line-height": 1.25,
        "text-background-color": t("node.label.bg"), "text-background-opacity": 0.85,
        "text-background-padding": 2, "text-background-shape": "roundrectangle",
        "min-zoomed-font-size": 5 } },

    // Address = deep fill + soft rim. Bitcoin transaction = a VISIBLE routing node (Invariant #5), deep
    // fill + an amber rim. The rims are recessive defaults; status rings below override them when present.
    { selector: 'node[kind="address"]', style: {
        shape: "ellipse", "background-color": t("node.address.fill"), width: sz(30), height: sz(30),
        "border-width": 1.5, "border-color": t("node.address.rim") } },
    { selector: 'node[kind="transaction"]', style: {
        shape: "round-rectangle", "background-color": t("node.transaction.fill"), width: sz(40), height: sz(22),
        "border-width": 1.5, "border-color": t("node.transaction.rim") } },
    { selector: 'node[kind="external"]', style: {
        shape: "diamond", "background-color": t("node.external.fill"), width: sz(18), height: sz(18) } },

    // Dust / high-fan-in AGGREGATE: a display-only summary node standing in for many small counterparties
    // ("12,431 inflows · $0.43 · dust"). Dashed rim = "this is a bundle, click to expand to the real
    // underlying" (the aggregate is never a fact — Inv #5; its members keep their own provenance — Inv #3).
    { selector: 'node[kind="aggregate"]', style: {
        shape: "round-rectangle", "background-color": t("node.aggregate.fill"),
        "border-width": 1.5, "border-color": t("node.aggregate.rim"), "border-style": "dashed",
        color: t("node.aggregate.label"), width: sz(70), height: sz(34), "font-size": fs(9),
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "text-max-width": 88,
        "text-margin-y": 0, "text-background-opacity": 0 } },

    // P3.5 VALUE-FILTER bucket (user_dust): the investigator-chosen "below $X" fold. Same dashed-bundle
    // affordance as the auto-dust node but its OWN tokens + an "below $X" label, so it reads as — and
    // never merges with — the automatic dust aggregate (which collapses tiny/unflagged counterparties).
    { selector: 'node[kind="user_dust"]', style: {
        shape: "round-rectangle", "background-color": t("node.dust.user.fill"),
        "border-width": 1.5, "border-color": t("node.dust.user.rim"), "border-style": "dashed",
        color: t("node.dust.user.label"), width: sz(74), height: sz(34), "font-size": fs(9),
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "text-max-width": 92,
        "text-margin-y": 0, "text-background-opacity": 0 } },

    // Grouping: a compound parent box drawn behind its member addresses (co-spend cluster / sourced entity).
    { selector: 'node[kind="group"]', style: {
        shape: "round-rectangle", "text-valign": "top", "text-halign": "center",
        "font-size": fs(11), "padding": 14, color: t("group.label.color"), "background-opacity": 0.55 } },
    { selector: 'node[group_type="cospend"]', style: {
        "background-color": t("group.cospend.fill"), "border-width": 1.5, "border-color": t("group.cospend.border") } },
    { selector: 'node[group_type="entity"]', style: {
        "background-color": t("group.entity.fill"), "border-width": 1.5, "border-color": t("group.entity.border") } },
    // Denomination pool (P8.6 #7): counterparties sharing ONE exact native amount (a mixer pool) — an
    // amber compound box, distinct from the co-spend (blue) / entity (teal-pink) clusters.
    { selector: 'node[group_type="denomination"]', style: {
        "background-color": t("group.denomination.fill"), "border-width": 1.5,
        "border-color": t("group.denomination.border") } },
    // Leiden community (P8.8): VISUAL STRUCTURE only, never ownership — a DASHED violet box, visually set
    // apart from the evidentiary clusters (co-spend/entity) so it never reads as a control claim.
    { selector: 'node[group_type="community"]', style: {
        "background-color": t("group.community.fill"), "border-width": 1.5, "border-style": "dashed",
        "border-color": t("group.community.border") } },

    // Sourced attribution/entity: "pink = attributed entity" is ONE consistent highlight — a pink rim
    // PLUS a pink label outline. The label TEXT stays the default aqua (node.label.color) so the entity
    // signal reads as the ring/outline, not a recolored text fill (the rim, the legend swatch, and this
    // outline all resolve to node.entity.ring). Kept clearly off the sanctioned-risk fuchsia.
    // P8.7.1 #6 — lift a flagged node ABOVE its parent group box so its halo/ring composites against the
    // canvas, not the semi-opaque denomination/co-spend fill (a sanctioned node inside a "100 ETH ×2"
    // pool must KEEP its red halo + badge, never be demoted to plain by grouping).
    { selector: "node[?has_attribution]", style: {
        "border-width": 3, "border-color": t("node.entity.ring"),
        "text-outline-color": t("node.entity.ring"), "text-outline-width": 1,
        "z-compound-depth": "top" } },

    // Possible-CoinJoin co-spend membership: a dashed amber ring (addresses never carry provisional border).
    { selector: "node[?coinjoin]", style: {
        "border-width": 3, "border-color": t("node.flag.coinjoin.ring"), "border-style": "dashed" } },

    // Investigator ANNOTATION: a clean emerald OUTLINE (drawn OUTSIDE the node's border, so it coexists
    // with the entity-pink / risk-red border rings rather than fighting them) on any node/tx that carries
    // ≥1 durable note. Distinct from the aqua label text and the teal transfer edge.
    { selector: "node[?has_annotation]", style: {
        "outline-color": t("node.annotation.ring"), "outline-width": 3, "outline-offset": 2 } },

    // Risk — an unmistakable halo (underlay glow), distinct from the bright SELECTION border so they never collide.
    { selector: 'node[risk_level="elevated"]', style: {
        "underlay-color": t("node.risk.elevated.halo"), "underlay-padding": 6, "underlay-opacity": 0.4,
        "z-compound-depth": "top" } },
    { selector: 'node[risk_level="sanctioned"]', style: {
        "underlay-color": t("node.risk.sanctioned.halo"), "underlay-padding": 8, "underlay-opacity": 0.5,
        "border-width": 3, "border-color": t("node.risk.sanctioned.badge"), "border-style": "solid",
        "z-compound-depth": "top" } },

    // Seed / anchor — the address the investigation started from: a single clean ★ marker drawn CENTERED
    // ON the node glyph (position 50%/50%, containment "over" + clip "none"). `background-fit: "contain"`
    // SCALES the (explicitly-sized, padded) badge SVG to fit the node so it is NEVER clipped — the earlier
    // `fit: "none"` rendered the SVG at an ambiguous size and the width box clipped it (the reported bug).
    // The marker stays OFF the text (no label outline / no `color` override) so the label is a clean ≤2
    // lines and an attributed seed still shows the default aqua text + its pink entity outline.
    { selector: "node[?seed]", style: {
        "background-image": seedBadgeImage(theme), "background-image-containment": "over",
        "background-clip": "none", "background-fit": "contain", "background-repeat": "no-repeat",
        "background-width": sz(24), "background-height": sz(24),
        "background-position-x": "50%", "background-position-y": "50%" } },

    // Ordering (P3.5 feature 1): a neighbor with NO order key (unpriced for value / NULL-height for
    // sequence) is trayed below the axis with a STRIKETHROUGH glyph (the ordering-tray token) + dimmed,
    // so "this one couldn't be ordered" reads at a glance. Only set while ordering is active.
    { selector: "node[?ordering_trayed]", style: {
        "background-image": strikeBadgeImage(theme), "background-image-containment": "over",
        "background-clip": "none", "background-fit": "contain", "background-repeat": "no-repeat",
        "background-width": sz(26), "background-height": sz(26),
        "background-position-x": "50%", "background-position-y": "50%",
        "border-color": t("node.ordering.tray"), opacity: 0.6 } },

    // Edges — fact types (colors tokenized). Width ∝ value (data(ew), set by the read-model) so dominant
    // flows pop; the base width is a fallback for any edge without a computed width.
    { selector: "edge", style: {
        width: sz(1.6), "line-color": t("edge.default.line"), "target-arrow-color": t("edge.default.line"),
        "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.85 } },
    // P35/UX-02 — a SECOND, COLOR-INDEPENDENT channel: each fact-edge kind carries a DISTINCT target-arrow
    // shape (filled triangle = EVM transfer · tee/bar = BTC input · open vee = BTC output), so the three read
    // apart in grayscale / for color-blind users, not by hue alone. `tx_input` is also shifted OFF the amber
    // band (tokens.json) so it no longer blends into the amber transaction-node rim. Mirrored in the report
    // twin (backend/app/theme.py::cytoscape_style) + both legends.
    { selector: 'edge[kind="transfer"]', style: {
        "line-color": t("edge.transfer.line"), "target-arrow-color": t("edge.transfer.line"),
        "target-arrow-shape": "triangle" } },
    { selector: 'edge[kind="tx_input"]', style: {
        "line-color": t("edge.tx_input.line"), "target-arrow-color": t("edge.tx_input.line"),
        "target-arrow-shape": "tee" } },
    { selector: 'edge[kind="tx_output"]', style: {
        "line-color": t("edge.tx_output.line"), "target-arrow-color": t("edge.tx_output.line"),
        "target-arrow-shape": "vee" } },

    // Dust-aggregate edge (focus <-> summary node): a dashed bundle edge, width by the aggregate's total USD.
    { selector: 'edge[kind="aggregate"]', style: {
        "line-color": t("edge.aggregate.line"), "target-arrow-color": t("edge.aggregate.line"),
        "line-style": "dashed", width: ewWidth } },
    // The value-filter (user_dust) bundle edge — its own token so it stays distinct from auto dust.
    { selector: 'edge[kind="user_dust"]', style: {
        "line-color": t("edge.dust.user.line"), "target-arrow-color": t("edge.dust.user.line"),
        "line-style": "dashed", width: ewWidth } },

    // P8.7 #2 — UNVERIFIED / unpriced-token bundle: a muted grey dashed box (a display de-emphasis of
    // unverified tokens — NOT a malice claim). Click to reveal the real underlying.
    { selector: 'node[kind="unverified"]', style: {
        shape: "round-rectangle", "background-color": t("node.unverified.fill"),
        "border-width": 1.5, "border-color": t("node.unverified.rim"), "border-style": "dashed",
        color: t("node.unverified.label"), width: sz(78), height: sz(34), "font-size": fs(9),
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "text-max-width": 96,
        "text-margin-y": 0, "text-background-opacity": 0 } },
    { selector: 'edge[kind="unverified"]', style: {
        "line-color": t("edge.unverified.line"), "target-arrow-color": t("edge.unverified.line"),
        "line-style": "dashed", width: ewWidth } },

    // P8.7 #3 — POSSIBLE ADDRESS-POISONING bundle: a red dashed box. A reversible HEURISTIC, never a fact.
    { selector: 'node[kind="poison"]', style: {
        shape: "round-rectangle", "background-color": t("node.poison.fill"),
        "border-width": 1.5, "border-color": t("node.poison.rim"), "border-style": "dashed",
        color: t("node.poison.label"), width: sz(78), height: sz(34), "font-size": fs(9),
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "text-max-width": 96,
        "text-margin-y": 0, "text-background-opacity": 0 } },
    { selector: 'edge[kind="poison"]', style: {
        "line-color": t("edge.poison.line"), "target-arrow-color": t("edge.poison.line"),
        "line-style": "dashed", "line-dash-pattern": [6, 3, 1, 3], width: ewWidth } },

    // The poison-suspect FLAG on an individual edge/node (shown when NOT folded): a red dotted outline so
    // "possible address poisoning" reads at a glance without asserting it as fact.
    // P36/UX-04 — POISON heuristic dash family = dash-dot [6,3,1,3] (see the provisional rule below for the
    // full dash-MEANING scheme). The suspect flag joins the poison bundle's language so both read "poison".
    { selector: "edge[?poison_suspect]", style: {
        "line-color": t("edge.poison.line"), "target-arrow-color": t("edge.poison.line"),
        "line-style": "dashed", "line-dash-pattern": [6, 3, 1, 3], opacity: 0.85 } },
    { selector: "node[?poison_suspect]", style: {
        "outline-color": t("node.poison.rim"), "outline-width": 3, "outline-offset": 2,
        "border-style": "dotted" } },

    // A value movement with NO USD price is an honest GAP — keep it visible (native amount still labels it)
    // but de-emphasised, so it never competes with priced flows or reads as a $0 flow.
    { selector: "edge[?no_price]", style: { opacity: 0.45 } },

    // Fact edges carry a value: label the amount (on a small plate, only when zoomed in) and scale the
    // edge width by value so the money is followable. Trace conventions have no value_label -> not matched.
    { selector: "edge[?value_label]", style: {
        width: ewWidth, label: "data(value_label)", "font-size": fs(7), color: t("edge.value.label"),
        "text-background-color": t("edge.value.labelBg"), "text-background-opacity": 0.85,
        "text-background-padding": 2, "text-background-shape": "roundrectangle",
        "text-rotation": "autorotate", "min-zoomed-font-size": 8 } },

    // USD value-at-time (the DeFiLlama payoff) WINS the edge label when present — the cross-asset figure is
    // more followable than native units. Drawn after the native rule so it overrides the text; aggregate
    // edges (no native label) also pick up the plate here. No price -> this rule doesn't match -> native shows.
    { selector: "edge[?value_usd_label]", style: {
        label: "data(value_usd_label)", "font-size": fs(7), color: t("edge.value.label"),
        "text-background-color": t("edge.value.labelBg"), "text-background-opacity": 0.85,
        "text-background-padding": 2, "text-background-shape": "roundrectangle",
        "text-rotation": "autorotate", "min-zoomed-font-size": 8 } },

    // Investigator RENAME of a flow (feature A): a custom display label on a transfer / tx_output edge
    // WINS the edge label (drawn after the value rules so it overrides them). The value still drives the
    // edge WIDTH; only the text changes. The underlying movement + its facts are untouched (Inv #5/#6).
    { selector: "edge[?custom_label]", style: {
        label: "data(custom_label)", "font-size": fs(7), color: t("edge.value.label"),
        "text-background-color": t("edge.value.labelBg"), "text-background-opacity": 0.85,
        "text-background-padding": 2, "text-background-shape": "roundrectangle",
        "text-rotation": "autorotate", "min-zoomed-font-size": 6 } },

    // Investigator ANNOTATION on a flow: a green GLOW (underlay) around the edge — the edge analogue of
    // the annotated-node emerald outline — so a noted flow reads as annotated without losing its fact
    // color (transfer-teal / output-magenta / input-amber). Resolves from the catalog (Annotation token).
    { selector: "edge[?has_annotation]", style: {
        "underlay-color": t("edge.annotation.glow"), "underlay-padding": 3, "underlay-opacity": 0.45 } },

    // Trace overlay: a FIFO link is a labeled CONVENTION — dashed, distinct color, a "fifo" tag — so it
    // can NEVER be mistaken for a ledger fact (Invariant integrity). Label only appears at closer zoom, and
    // is staggered by the read-model's per-edge `label_dy` so adjacent trace labels don't stack at a hub.
    { selector: 'edge[trace="fifo"]', style: {
        "line-color": t("edge.trace.fifo.line"), "target-arrow-color": t("edge.trace.fifo.line"),
        "line-style": "dashed", "line-dash-pattern": [12, 4], width: sz(2.4), label: "fifo", "font-size": fs(8), color: t("edge.trace.fifo.label"),
        "text-background-color": t("edge.trace.fifo.labelBg"), "text-background-opacity": 0.85,
        "text-background-padding": 2, "text-background-shape": "roundrectangle",
        "text-rotation": "autorotate", "text-margin-y": "data(label_dy)", "min-zoomed-font-size": 6 } },
    { selector: 'edge[trace="investigator"]', style: {
        "line-color": t("edge.trace.investigator.line"), "target-arrow-color": t("edge.trace.investigator.line"),
        "line-style": "dashed", "line-dash-pattern": [12, 4, 2, 4], width: sz(2.4), label: "manual", "font-size": fs(8),
        color: t("edge.trace.investigator.line"), "text-rotation": "autorotate",
        "text-margin-y": "data(label_dy)", "min-zoomed-font-size": 6 } },

    // Provisional (tip) facts: FINE-DOTTED + faded (Invariant #6). P36/UX-04 — the three dash MEANING families
    // are now visually distinct so a dashed edge decodes without guessing: FINALITY = fine dots [1,4] ·
    // TRACE-CONVENTION = long dashes (fifo [12,4] / investigator long-dash-dot [12,4,2,4]) · POISON heuristic
    // = dash-dot [6,3,1,3]. (The bundle edges — aggregate/user_dust/unverified — stay plain-dashed; their
    // meaning is the bundle NODE, not the dash.) Documented in the legend, live + report.
    { selector: 'edge[finality_status="provisional"]', style: {
        "line-style": "dashed", "line-dash-pattern": [1, 4], opacity: 0.55 } },
    { selector: 'node[finality_status="provisional"]', style: {
        "border-width": 2, "border-color": t("node.provisional.border"),
        "border-style": "dashed", "background-opacity": 0.5 } },

    // Trace focus mode: dim everything off the active trace and emphasize the spine, so the investigator
    // follows one flow. Toggled by adding the classes in Graph.tsx (opacity/width — not colors).
    { selector: ".bih-faded", style: { opacity: 0.12, "text-opacity": 0.12 } },
    { selector: "edge.bih-focus", style: { opacity: 1, width: 5 } },
    { selector: "node.bih-focus", style: { opacity: 1, "underlay-opacity": 0.35 } },

    // Selection ring — drawn last so it tops everything; bright (white/cyan), so it never reads as the magenta risk ring.
    { selector: "node:selected", style: { "border-width": 4, "border-color": t("node.selected.border") } },
  ];
}

// --- context-aware legend (generated from the catalog) ---------------------------------------
export type LegendItem = { label: string; color: string; marker: "node" | "edge" | "halo" | "ring" };

type LegendGraph = {
  nodes: { kind?: string; risk_level?: string; has_attribution?: boolean; coinjoin?: boolean;
    seed?: boolean; has_annotation?: boolean; group_type?: string; poison_suspect?: boolean }[];
  edges: { kind?: string; trace?: string; finality_status?: string; no_price?: boolean;
    has_annotation?: boolean; poison_suspect?: boolean }[];
};

/** Build a legend showing ONLY the element types/flags actually present in the current case. */
export function legendItems(g: LegendGraph): LegendItem[] {
  const nodeKinds = new Set(g.nodes.map((n) => n.kind));
  const edgeKinds = new Set(g.edges.map((e) => e.kind));
  const has = {
    seed: g.nodes.some((n) => n.seed),
    sanctioned: g.nodes.some((n) => n.risk_level === "sanctioned"),
    elevated: g.nodes.some((n) => n.risk_level === "elevated"),
    attribution: g.nodes.some((n) => n.has_attribution),
    annotation: g.nodes.some((n) => n.has_annotation) || g.edges.some((e) => e.has_annotation),
    coinjoin: g.nodes.some((n) => n.coinjoin),
    fifo: g.edges.some((e) => e.trace === "fifo"),
    investigator: g.edges.some((e) => e.trace === "investigator"),
    provisional: g.edges.some((e) => e.finality_status === "provisional"),
  };
  const items: LegendItem[] = [];
  const lbl = (id: string) => _byId[id]?.label ?? id;
  if (has.seed) items.push({ label: "★ Seed / anchor", color: t("node.seed.marker"), marker: "ring" });
  if (nodeKinds.has("address")) items.push({ label: lbl("node.address.fill"), color: t("node.address.fill"), marker: "node" });
  if (nodeKinds.has("transaction")) items.push({ label: lbl("node.transaction.fill"), color: t("node.transaction.rim"), marker: "node" });
  if (nodeKinds.has("external")) items.push({ label: lbl("node.external.fill"), color: t("node.external.fill"), marker: "node" });
  if (nodeKinds.has("aggregate")) items.push({ label: "Dust aggregate (click to expand)", color: t("node.aggregate.rim"), marker: "node" });
  if (nodeKinds.has("user_dust")) items.push({ label: "Value-filtered (below threshold)", color: t("node.dust.user.rim"), marker: "node" });
  if (nodeKinds.has("unverified")) items.push({ label: "Unverified / unpriced tokens", color: t("node.unverified.rim"), marker: "node" });
  if (nodeKinds.has("poison") || g.nodes.some((n) => n.poison_suspect) || g.edges.some((e) => e.poison_suspect))
    items.push({ label: "Possible address-poisoning — dash-dot (heuristic)", color: t("node.poison.rim"), marker: "ring" });
  if (g.nodes.some((n) => n.group_type === "denomination")) items.push({ label: "Denomination pool", color: t("group.denomination.border"), marker: "ring" });
  // P35/UX-02 — name each fact-edge's ARROW SHAPE in the label so the legend documents the color-independent
  // channel (filled triangle / tee bar / open vee), not just the hue (mirrored in the report's static legend).
  if (edgeKinds.has("transfer")) items.push({ label: "EVM transfer (filled arrow)", color: t("edge.transfer.line"), marker: "edge" });
  if (edgeKinds.has("tx_input")) items.push({ label: "Bitcoin input (bar arrow)", color: t("edge.tx_input.line"), marker: "edge" });
  if (edgeKinds.has("tx_output")) items.push({ label: "Bitcoin output (chevron)", color: t("edge.tx_output.line"), marker: "edge" });
  if (has.sanctioned) items.push({ label: "Sanctioned (OFAC)", color: t("node.risk.sanctioned.halo"), marker: "halo" });
  if (has.elevated) items.push({ label: "Other risk", color: t("node.risk.elevated.halo"), marker: "halo" });
  if (has.attribution) items.push({ label: "Attributed entity", color: t("node.entity.ring"), marker: "ring" });
  if (has.annotation) items.push({ label: "Annotated (has note)", color: t("node.annotation.ring"), marker: "ring" });
  if (has.coinjoin) items.push({ label: lbl("node.flag.coinjoin.ring"), color: t("node.flag.coinjoin.ring"), marker: "ring" });
  // P36/UX-04 — name each dash MEANING family so a dashed edge decodes from the legend (finality vs
  // trace-convention vs poison), not by guessing.
  if (has.fifo) items.push({ label: "FIFO trace — long dash (convention)", color: t("edge.trace.fifo.line"), marker: "edge" });
  if (has.investigator) items.push({ label: "Investigator trace — long dash-dot", color: t("edge.trace.investigator.line"), marker: "edge" });
  if (has.provisional) items.push({ label: "Provisional — fine dots (tip)", color: t("node.provisional.border"), marker: "edge" });
  return items;
}
