import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import cytoscape, { type Core } from "cytoscape";
// @ts-expect-error — cytoscape-svg ships no bundled types; it registers cy.svg() (browser-only).
import cytoscapeSvg from "cytoscape-svg";
import AddAddress from "./AddAddress";
import CasePicker from "./CasePicker";
import DenomPanel from "./DenomPanel";
import ClusteringPanel from "./ClusteringPanel";
import { checkIntel, intelSummary } from "./intel";
import { getActiveJob, jobProgressLine } from "./jobs";
import { caseLabel, getActiveCase, type CaseMeta } from "./cases";
import FindingsPanel from "./FindingsPanel";
import ReportButton from "./ReportButton";
import SettingsPanel from "./SettingsPanel";
import ThemeCustomize from "./ThemeCustomize";
import Graph, { type GraphData, type GraphEdge, type GraphNode, type ViewMeta } from "./Graph";
import SidePanel, { type Annotation, type AddressClaims, type NodeSummary, type TraceInfo } from "./SidePanel";
import { exportGraphImage, downloadImage, type ImageFormat } from "./exportImage";
import type { OrderMode } from "./ordering";
import { DEFAULT_VIEW, loadCasePrefs, saveCasePrefs, type ValueBasis, type ViewState, viewLoadSignature, viewToReportParams } from "./viewState";
import {
  applyThemeVars, CANVAS_PRESETS, type CanvasPreset, getActivePreset, getThemeSnapshot, legendItems,
  setActivePreset, subscribeTheme, t, type LegendItem,
} from "./theme/theme";

// Register the SVG exporter once (idempotent across HMR / repeated module eval; cytoscape.use throws on
// a re-register). Browser-only — never imported by the (node) unit tests, which import exportImage.ts
// directly with a stub Core.
const sw = window as unknown as { __cySvgRegistered?: boolean };
if (!sw.__cySvgRegistered) {
  cytoscape.use(cytoscapeSvg);
  sw.__cySvgRegistered = true;
}

// Independent font-size UI PREFS (features 5–7): the graph-label multiplier and the side-panel
// multiplier are persisted in localStorage — distinct from the scroll-wheel zoom AND from the
// view-history stack (they are display prefs, not case rows and not part of undo/redo).
const clampFont = (v: number): number => Math.min(2, Math.max(0.6, Math.round(v * 10) / 10));
const loadFont = (key: string): number => clampFont(Number(localStorage.getItem(key)) || 1);

// A durable target the investigator can rename / annotate — a node (address / transaction) or a flow
// (transfer / tx_output edge). View artifacts (external / group / aggregate / tx_input / trace) -> null.
function targetForNode(n: GraphNode): { ttype: string; tid: string } | null {
  if (n.kind === "address") return { ttype: "address", tid: n.id.replace(/^addr:/, "") };
  if (n.kind === "transaction") return { ttype: "transaction", tid: n.id.replace(/^tx:/, "") };
  return null;
}

// Apply the catalog as CSS custom properties on :root once, so any CSS (and the future customization
// UI) can read var(--bih-*). All colors below resolve through the catalog — no hardcoded hex.
applyThemeVars();

// A legend swatch whose shape hints at the element it labels (node / edge / halo / ring).
function Swatch({ item }: { item: LegendItem }) {
  const base: React.CSSProperties = { display: "inline-block", width: 12, height: 12, marginRight: 5,
    verticalAlign: "middle" };
  if (item.marker === "edge")
    return <span style={{ ...base, height: 0, borderTop: `3px solid ${item.color}`, marginBottom: 2 }} />;
  if (item.marker === "halo")
    return <span style={{ ...base, borderRadius: "50%", background: item.color, opacity: 0.5,
      boxShadow: `0 0 0 2px ${item.color}` }} />;
  if (item.marker === "ring")
    return <span style={{ ...base, borderRadius: "50%", border: `2.5px solid ${item.color}` }} />;
  return <span style={{ ...base, borderRadius: "50%", background: item.color }} />;
}

// A −/+ font-size stepper (graph labels / side panel). A view-only UI pref, shown as a percentage.
function FontStepper({ label, value, onChange }: {
  label: string; value: number; onChange: (v: number) => void;
}) {
  const ctrl: React.CSSProperties = { color: t("ui.muted"), fontSize: 12, display: "flex",
    alignItems: "center", gap: 3, whiteSpace: "nowrap" };
  const btn: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "1px 7px", fontSize: 13,
    lineHeight: 1.1, cursor: "pointer" };
  return (
    <span style={ctrl} title={`${label} font size (independent of zoom)`}>
      <span>{label}</span>
      <button style={btn} onClick={() => onChange(clampFont(value - 0.1))} aria-label={`${label} font smaller`}>−</button>
      <span style={{ minWidth: 34, textAlign: "center", color: t("ui.text.secondary") }}>{Math.round(value * 100)}%</span>
      <button style={btn} onClick={() => onChange(clampFont(value + 0.1))} aria-label={`${label} font larger`}>+</button>
    </span>
  );
}

// ViewState + DEFAULT_VIEW + viewLoadSignature now live in ./viewState (DOM-free + unit-tested).

export default function App() {
  const [data, setData] = useState<GraphData | null>(null);
  const [meta, setMeta] = useState<ViewMeta | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null);
  const [claims, setClaims] = useState<AddressClaims | null>(null);
  const [summary, setSummary] = useState<NodeSummary | null>(null);
  const [traces, setTraces] = useState<TraceInfo[]>([]);
  const [focusTrace, setFocusTrace] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [view, setView] = useState<ViewState>(DEFAULT_VIEW);
  const setV = (patch: Partial<ViewState>) => setView((v) => ({ ...v, ...patch }));

  // Annotations for the selected node/flow + the Findings & Notes panel.
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [showFindings, setShowFindings] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showThemeCustomize, setShowThemeCustomize] = useState(false);
  const [showAddAddress, setShowAddAddress] = useState(false);
  const [showDenomPanel, setShowDenomPanel] = useState(false);
  const [showClusteringPanel, setShowClusteringPanel] = useState(false);
  const [intelBusy, setIntelBusy] = useState(false);
  const [intelNote, setIntelNote] = useState<string | null>(null);

  // P6 canvas theme: the theme store lives outside React, so subscribe to re-render the whole app (every
  // `t(...)` re-resolves) on a Dark/Light/Custom switch or a Custom-color edit; re-apply the CSS vars too.
  const themeVersion = useSyncExternalStore(subscribeTheme, getThemeSnapshot);
  const activePreset = getActivePreset();
  useEffect(() => { applyThemeVars(); }, [themeVersion]);
  const [investRefresh, setInvestRefresh] = useState(0);  // bump to re-pull notes/findings

  // The ACTIVE case (P4). "loading" until the first /api/cases/active resolves; null -> show the entry
  // screen (empty state); a CaseMeta -> the app. `showPicker` opens the picker as a switcher overlay.
  const [activeCase, setActiveCase] = useState<CaseMeta | null | "loading">("loading");
  const [showPicker, setShowPicker] = useState(false);
  const refreshActiveCase = useCallback(() => {
    getActiveCase().then(setActiveCase).catch(() => setActiveCase(null));
  }, []);
  useEffect(() => { refreshActiveCase(); }, [refreshActiveCase]);
  // After a case is opened/created/imported: switch to it and RESET everything that belonged to the old
  // case. Clearing `data`/`meta` drops the previous case's canvas immediately (no stale graph); the load
  // effect then re-fetches /api/view for the new case (its seed or empty-state) with no page reload.
  const handleCaseOpened = useCallback((a: CaseMeta) => {
    setData(null); setMeta(null);                 // drop the old graph at once (canvas shows "Loading…")
    setActiveCase(a);
    setShowPicker(false);
    setView({ ...DEFAULT_VIEW });                  // fresh seed/empty-state view (clears ordering/filter/dust/expand/focus)
    setNav({ stack: [], index: -1 });              // undo/redo history belonged to the old case
    setSelected(null); setSelectedEdge(null); setClaims(null); setSummary(null); setAnnotations([]);
    setTraces([]); setSearch(""); setFocusTrace(false);
    setInvestRefresh((x) => x + 1);
  }, []);

  // Independent font-size prefs (localStorage; NOT in ViewState, so untouched by undo/redo + zoom).
  const [graphFont, setGraphFont] = useState<number>(() => loadFont("bih.graphFont"));
  const [panelFont, setPanelFont] = useState<number>(() => loadFont("bih.panelFont"));
  useEffect(() => { localStorage.setItem("bih.graphFont", String(graphFont)); }, [graphFont]);
  useEffect(() => { localStorage.setItem("bih.panelFont", String(panelFont)); }, [panelFont]);

  // Client-side view HISTORY (Home / step back / forward). Pure navigation over EPHEMERAL view params —
  // it NEVER touches case.db (durable annotations/labels/findings re-render unchanged on every view).
  const [nav, setNav] = useState<{ stack: ViewState[]; index: number }>({ stack: [], index: -1 });
  const isHistoryNav = useRef(false);
  useEffect(() => {
    if (isHistoryNav.current) { isHistoryNav.current = false; return; }
    setNav((n) => {
      const base = n.stack.slice(0, n.index + 1);   // a new action after stepping back truncates the redo branch
      return { stack: [...base, view], index: base.length };
    });
  }, [view]);
  const canBack = nav.index > 0;
  const canForward = nav.index < nav.stack.length - 1;
  const stepTo = (i: number) => { isHistoryNav.current = true; setNav((n) => ({ ...n, index: i })); setView(nav.stack[i]); };
  const back = () => { if (canBack) stepTo(nav.index - 1); };
  const forward = () => { if (canForward) stepTo(nav.index + 1); };
  const home = () => setView({ ...DEFAULT_VIEW });   // reset to the seed-focused start (pushes to history)

  const loadTraces = useCallback(() => {
    fetch("/api/traces")
      .then((r) => (r.ok ? r.json() : { traces: [] }))
      .then((d: { traces: TraceInfo[] }) => setTraces(d.traces ?? []))
      .catch(() => setTraces([]));
  }, []);

  // The bounded, scale-aware view: never auto-renders the full graph for a high-degree node. Depends on
  // the SERVER-relevant view fields ONLY (NOT `ordering`, which is a pure frontend layout) so changing
  // the ordering never triggers a refetch — it just re-lays-out the same data.
  const { focus, hops, nodeCap, groupDust, dustFloor, valueFloor, onlyFlagged, userDustOn, userDustUsd,
          expand, valueBasis, groupDenominations, showUnverified, foldPoison, denomFilters, community } = view;
  const loadView = useCallback(() => {
    setError(null);
    const p = new URLSearchParams();
    if (focus) p.set("focus", focus);
    p.set("hops", String(hops));
    p.set("node_cap", String(nodeCap));
    p.set("group_dust", String(groupDust));
    p.set("dust_floor_usd", String(dustFloor));
    p.set("value_floor_usd", String(valueFloor));
    if (onlyFlagged) p.set("only_flagged", "true");
    if (userDustOn && userDustUsd > 0) p.set("user_dust_usd", String(userDustUsd));
    if (expand.length) p.set("expand", expand.join(","));
    if (valueBasis === "native") p.set("value_basis", "native");           // P8.6 USD<->native toggle
    if (groupDenominations) p.set("group_denominations", "true");          // P8.6 denomination grouping
    if (showUnverified) p.set("show_unverified", "true");                  // P8.7 reveal unverified tokens
    if (!foldPoison) p.set("fold_poison", "false");                       // P8.7 stop folding poison
    if (Object.keys(denomFilters).length) p.set("denom_filters", JSON.stringify(denomFilters));  // P8.7 #1
    if (community) p.set("community", "true");                              // P8.8 Leiden community overlay
    fetch(`/api/view?${p.toString()}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: GraphData) => { setData(d); setMeta(d.meta ?? null); })
      .catch((e) => setError(String(e)));
  }, [focus, hops, nodeCap, groupDust, dustFloor, valueFloor, onlyFlagged, userDustOn, userDustUsd, expand,
      valueBasis, groupDenominations, showUnverified, foldPoison, denomFilters, community]);

  // Only query a case once one is active (the entry screen shows when there is none — no 503 flash). The
  // load is keyed on `viewLoadSignature`, which includes the active CASE PATH — so switching cases
  // re-fetches even when the view params are identical (the empty-state renders for a fresh case with no
  // reload), while an ordering-only change does NOT refetch (P3.5).
  const activeCasePath = activeCase && activeCase !== "loading" ? activeCase.path : null;
  const loadSig = useMemo(() => viewLoadSignature(activeCasePath, view), [activeCasePath, view]);
  useEffect(() => { if (loadSig) loadView(); }, [loadView, loadSig]);
  useEffect(() => { if (activeCasePath) loadTraces(); }, [loadTraces, activeCasePath]);

  // Persist per case (P8.6 #8): restore the remembered value basis + ordering when the active case
  // changes (initial load OR a switch); saving happens imperatively in the basis/ordering handlers.
  const restoredFor = useRef<string | null>(null);
  useEffect(() => {
    if (activeCasePath && restoredFor.current !== activeCasePath) {
      restoredFor.current = activeCasePath;
      const prefs = loadCasePrefs(activeCasePath);
      if (prefs) setView((v) => ({ ...v, valueBasis: prefs.valueBasis, ordering: prefs.ordering }));
    }
  }, [activeCasePath]);

  // Switch the value basis (USD <-> native) and remember it for this case.
  const setBasis = useCallback((b: ValueBasis) => {
    setView((v) => ({ ...v, valueBasis: b }));
    saveCasePrefs(activeCasePath, { valueBasis: b, ordering: view.ordering });
  }, [activeCasePath, view.ordering]);

  // Load the durable notes for any annotatable target (node or flow) into the side panel.
  const loadAnnotations = useCallback((ttype: string, tid: string) => {
    fetch(`/api/target/${ttype}/${tid}/annotations`)
      .then((r) => (r.ok ? r.json() : { annotations: [] }))
      .then((d: { annotations: Annotation[] }) => setAnnotations(d.annotations ?? []))
      .catch(() => setAnnotations([]));
  }, []);

  // On selecting a node, fetch its value summary + ranked counterparties (the list view), and — for an
  // address — its sourced claims for the panel. Clears any selected flow.
  const handleSelect = useCallback((n: GraphNode | null) => {
    setSelected(n); setSelectedEdge(null); setClaims(null); setSummary(null); setAnnotations([]);
    if (n && (n.kind === "address" || n.kind === "transaction")) {
      fetch(`/api/node/${n.id}/summary`)
        .then((r) => (r.ok ? r.json() : null))
        .then((s: NodeSummary | null) => setSummary(s))
        .catch(() => setSummary(null));
    }
    const tgt = n ? targetForNode(n) : null;
    if (tgt) loadAnnotations(tgt.ttype, tgt.tid);
    if (n && n.kind === "address") {
      const addressId = n.id.replace(/^addr:/, "");
      fetch(`/api/address/${addressId}/claims`)
        .then((r) => (r.ok ? r.json() : null))
        .then((c: AddressClaims | null) => setClaims(c))
        .catch(() => setClaims(null));
    }
  }, [loadAnnotations]);

  // On selecting a FLOW (edge), show its facts + (for a transfer / tx_output) its durable notes. Clears
  // any selected node. tx_input / trace / aggregate edges have no durable object -> no notes fetch.
  const handleSelectEdge = useCallback((e: GraphEdge | null) => {
    setSelectedEdge(e); setSelected(null); setClaims(null); setSummary(null); setAnnotations([]);
    if (e && e.ann_type && e.ann_id) loadAnnotations(e.ann_type, e.ann_id);
  }, [loadAnnotations]);

  // Re-center the view on a node (deliberate expansion outward), resetting any dust expansions.
  const focusOn = useCallback((nodeId: string) => setView((v) => ({ ...v, focus: nodeId, expand: [] })), []);
  // Expand a dust aggregate to its real underlying counterparties (provenance preserved).
  const handleExpand = useCallback((aggId: string) =>
    setView((v) => (v.expand.includes(aggId) ? v : { ...v, expand: [...v.expand, aggId] })), []);
  // Jump to a specific address in a large graph (the focus resolver accepts an address or a node id).
  // This CENTERS on a node already in the case — it does NOT fetch new data (that's "Add address").
  const handleSearch = useCallback(() => {
    const q = search.trim();
    if (q) setView((v) => ({ ...v, focus: q, expand: [] }));
  }, [search]);

  // After ingesting a NEW address (P8.5 add-address): center the view on it (the focus resolver accepts a
  // raw address) and refresh the case header counts. The view reload is driven by the focus change.
  const handleIngested = useCallback((address: string, _partial: boolean) => {
    setView({ ...DEFAULT_VIEW, focus: address });   // recenters + reloads /api/view on the new address
    refreshActiveCase();                            // address/tx counts in the header update
  }, [refreshActiveCase]);

  // P8.7 #4 — run the free intel pillars (OFAC + GraphSense, + Chainalysis if keyed) against the case;
  // it WRITES sourced claims, then the graph re-render shows the sanctioned halo + entity overlay.
  const runIntel = useCallback(() => {
    setIntelBusy(true); setIntelNote(null);
    checkIntel()
      .then((r) => { setIntelNote(intelSummary(r)); loadView(); setInvestRefresh((x) => x + 1); })
      .catch((e) => setError(`intel check failed: ${String(e instanceof Error ? e.message : e)}`))
      .finally(() => setIntelBusy(false));
  }, [loadView]);

  // P8.7.2 — start a BACKGROUND valuation pass, then poll the job for live progress ("valuing M of N" /
  // "rate-limited — backing off") and reload when it finishes so USD fills in. Non-blocking + cancelable.
  const runValuation = useCallback(() => {
    setIntelBusy(true); setIntelNote("Valuation: starting…");
    fetch("/api/valuation/run", { method: "POST" })
      .then((r) => r.json().then((d) => (r.ok ? d : Promise.reject(new Error(d?.detail || `HTTP ${r.status}`)))))
      .then((d: { started?: boolean }) => {
        if (!d.started) { setIntelBusy(false); setIntelNote(null); return; }
        const iv = window.setInterval(async () => {
          const j = await getActiveJob();
          if (j && j.kind === "valuation") setIntelNote(`Valuation: ${jobProgressLine(j)}`);
          if (!j || j.state !== "running") {
            window.clearInterval(iv);
            setIntelBusy(false);
            setIntelNote(j && j.kind === "valuation"
              ? `Valuation ${j.state === "canceled" ? "canceled" : "done"} — ${j.valued} priced.` : null);
            loadView();
          }
        }, 600);
      })
      .catch((e) => { setError(`valuation failed: ${String(e instanceof Error ? e.message : e)}`); setIntelBusy(false); });
  }, [loadView]);

  // Universal investigator RENAME — any target (address / transaction node or transfer / tx_output flow).
  // Re-runs the VIEW so the custom label shows with the current focus, and refreshes the notes panel.
  const handleSaveLabel = useCallback((ttype: string, tid: string, label: string) => {
    fetch(`/api/target/${ttype}/${tid}/label`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label }) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(() => { loadView(); setInvestRefresh((x) => x + 1); })
      .catch((e) => setError(String(e)));
  }, [loadView]);
  // Double-click a node to rename it (works for address + transaction; view artifacts are ignored).
  const handleEditNode = useCallback((n: GraphNode) => {
    const tgt = targetForNode(n);
    if (!tgt) return;
    const next = window.prompt("Label for this node (investigator annotation):", n.label ?? "");
    if (next != null && next.trim()) handleSaveLabel(tgt.ttype, tgt.tid, next.trim());
  }, [handleSaveLabel]);
  // Add a durable investigator note to any target (node or flow); refresh its note list + the view (green
  // outline / glow) + the Findings & Notes panel.
  const handleAddAnnotation = useCallback((ttype: string, tid: string, content: string) => {
    fetch(`/api/target/${ttype}/${tid}/annotations`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { annotations: Annotation[] }) => {
        setAnnotations(d.annotations ?? []);
        loadView();
        setInvestRefresh((x) => x + 1);
      })
      .catch((e) => setError(String(e)));
  }, [loadView]);
  // Edit / delete a durable note (keyed by annotation id). The endpoint returns the target's refreshed
  // list (so the side panel updates); reload the view so a removed last-note clears the green outline/glow.
  // Returns the request promise (rejecting on failure) so the inline editor can stay open + keep the
  // user's text if the save fails, and close only on success.
  const handleEditAnnotation = useCallback((annotationId: string, content: string) => {
    return fetch(`/api/annotations/${annotationId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { annotations: Annotation[] }) => {
        setAnnotations(d.annotations ?? []); loadView(); setInvestRefresh((x) => x + 1);
      })
      .catch((e) => { setError(String(e)); throw e; });
  }, [loadView]);
  const handleDeleteAnnotation = useCallback((annotationId: string) => {
    fetch(`/api/annotations/${annotationId}`, { method: "DELETE" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { annotations: Annotation[] }) => {
        setAnnotations(d.annotations ?? []); loadView(); setInvestRefresh((x) => x + 1);
      })
      .catch((e) => setError(String(e)));
  }, [loadView]);
  // Re-pull the CURRENT selection's notes (used after the Findings panel edits/deletes a note, so the
  // side panel stays in sync regardless of which panel made the change).
  const reloadAnnotations = useCallback(() => {
    let tgt: { ttype: string; tid: string } | null = null;
    if (selected) tgt = targetForNode(selected);
    else if (selectedEdge && selectedEdge.ann_type && selectedEdge.ann_id)
      tgt = { ttype: selectedEdge.ann_type, tid: selectedEdge.ann_id };
    if (tgt) loadAnnotations(tgt.ttype, tgt.tid); else setAnnotations([]);
  }, [selected, selectedEdge, loadAnnotations]);

  const handleSaveTraceLabel = useCallback((traceId: string, label: string) => {
    fetch(`/api/trace/${traceId}/label`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label }) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(() => { loadView(); loadTraces(); setInvestRefresh((x) => x + 1); })
      .catch((e) => setError(String(e)));
  }, [loadView, loadTraces]);

  // Ordered layout (P3.5 feature 1): right-click a node -> a context menu to order THAT node's neighbors
  // by value / sequence. The menu position is the page coords from the DOM event. Ordering lives in the
  // view-state (so it's in the view-history + reset by Home) but is a pure frontend layout (no refetch).
  const [menu, setMenu] = useState<{ nodeId: string; x: number; y: number } | null>(null);
  const handleContextNode = useCallback((nodeId: string, pos: { x: number; y: number }) =>
    setMenu({ nodeId, x: pos.x, y: pos.y }), []);
  const applyOrdering = useCallback((mode: OrderMode | null, anchor?: string) => {
    const ordering = mode && anchor ? { anchor, mode } : null;
    setView((v) => ({ ...v, ordering }));
    saveCasePrefs(activeCasePath, { valueBasis: view.valueBasis, ordering });  // remember per case (#8)
    setMenu(null);
  }, [activeCasePath, view.valueBasis]);

  // Save the CURRENT focused/filtered view as a standalone exhibit image (SVG = vector, preferred for
  // court; PNG = raster). A pure VIEW ARTIFACT: renders the live Cytoscape view in the print-light
  // exhibit palette (restored after) and downloads it — it never calls the API and never touches case.db.
  const exportImage = useCallback((format: ImageFormat) => {
    const cy = (window as unknown as { __cy?: Core }).__cy;
    if (!cy) { setError("the graph is not ready to export yet"); return; }
    try {
      const img = exportGraphImage(cy, format, { fontScale: graphFont });
      const base = meta?.focus_label
        ? `bih-${meta.focus_label.replace(/[^\w.-]+/g, "_").slice(0, 40)}` : "bih-graph";
      downloadImage(img, format, base);
    } catch (e) {
      setError(`image export failed: ${String(e)}`);
    }
  }, [graphFont, meta]);

  const legend = useMemo(() => (data ? legendItems(data) : []), [data]);
  const hasTrace = useMemo(() => !!data?.edges.some((e) => e.kind === "trace"), [data]);
  // The dominant native unit in the current view (for the native-mode filter labels). Native amounts
  // aren't cross-asset comparable; this is just the label hint for the threshold inputs.
  const nativeUnit = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const e of data?.edges ?? []) if (e.asset_symbol) counts[e.asset_symbol] = (counts[e.asset_symbol] || 0) + 1;
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
    return top ? top[0] : "native";
  }, [data]);
  const isNative = view.valueBasis === "native";
  const unitLabel = isNative ? nativeUnit : "$";
  // Node lookup so the SidePanel can resolve a selected flow's endpoints (source/target labels + chain).
  const nodesById = useMemo(
    () => Object.fromEntries((data?.nodes ?? []).map((n) => [n.id, n] as const)), [data]);

  const ctrl: React.CSSProperties = { color: t("ui.muted"), fontSize: 12, display: "flex",
    alignItems: "center", gap: 4, whiteSpace: "nowrap" };
  const field: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "3px 6px", fontSize: 12 };

  // Gate the whole app on an active case (hooks above always run, so this is a safe post-hook branch).
  if (activeCase === "loading") {
    return (
      <div style={{ height: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
                    background: t("ui.app.bg"), color: t("ui.muted"), fontFamily: "system-ui, sans-serif" }}>
        Loading…
      </div>
    );
  }
  if (!activeCase) {
    return <CasePicker active={null} onOpened={handleCaseOpened} />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh",
                  fontFamily: "system-ui, sans-serif",
                  background: t("ui.app.bg"), color: t("ui.text") }}>
      <header style={{ display: "flex", gap: 12, alignItems: "center", padding: "8px 14px",
                       background: t("ui.panel.bg"),
                       borderBottom: `1px solid ${t("ui.border")}`, flexWrap: "wrap" }}>
        <strong>Blockchain Investigation Hub</strong>
        <span style={{ color: t("ui.text.secondary"), fontSize: 12, maxWidth: 220, overflow: "hidden",
                       textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              title={activeCase.path}>{caseLabel(activeCase)}</span>
        <button onClick={() => setShowPicker(true)} style={{ ...field, cursor: "pointer" }}
                title="Switch, create, or import a case">Cases</button>
        <span style={{ display: "flex", gap: 4 }}>
          <input placeholder="search / center address…" value={search}
                 onChange={(e) => setSearch(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") handleSearch(); }}
                 style={{ ...field, width: 200 }} />
          <button onClick={handleSearch} style={{ ...field, cursor: "pointer" }}
                  title="Center the view on an address ALREADY in this case (does not fetch new data)">Find</button>
          <button onClick={() => setShowAddAddress(true)}
                  style={{ ...field, cursor: "pointer", borderColor: t("node.seed.marker") }}
                  title="Ingest a NEW address from chain into this case (seeds an empty case)">+ Add address</button>
        </span>
        <span style={{ display: "flex", gap: 4 }}>
          <button onClick={home} title="Reset to the seed-focused start (view only)"
                  style={{ ...field, cursor: "pointer" }}>⌂ Home</button>
          <button onClick={back} disabled={!canBack} title="Step back"
                  style={{ ...field, cursor: canBack ? "pointer" : "default", opacity: canBack ? 1 : 0.4 }}>◀</button>
          <button onClick={forward} disabled={!canForward} title="Step forward"
                  style={{ ...field, cursor: canForward ? "pointer" : "default", opacity: canForward ? 1 : 0.4 }}>▶</button>
        </span>
        <button onClick={() => setShowFindings(true)} style={{ ...field, cursor: "pointer",
          borderColor: t("node.annotation.ring") }}>Findings &amp; Notes</button>
        <ReportButton viewParams={viewToReportParams(view)} />
        <button onClick={runIntel} disabled={intelBusy}
                style={{ ...field, cursor: intelBusy ? "default" : "pointer", borderColor: t("node.risk.sanctioned.badge"),
                         opacity: intelBusy ? 0.6 : 1 }}
                title="Run the free OFAC sanctions + GraphSense attribution pillars against this case (writes sourced claims, not facts)">
          {intelBusy ? "Checking…" : "Check intel"}</button>
        <button onClick={runValuation} disabled={intelBusy} style={{ ...field, cursor: intelBusy ? "default" : "pointer", opacity: intelBusy ? 0.6 : 1 }}
                title="Value this case's movements at their block time via DeFiLlama (free; offline-aware). Missing prices stay unpriced (honest).">
          Value</button>
        <button onClick={() => setShowSettings(true)} style={{ ...field, cursor: "pointer" }}
                title="Connectors, API keys, cases folder, offline mode">Settings</button>
        <span style={{ display: "inline-flex", border: `1px solid ${t("ui.border")}`, borderRadius: 6,
                       overflow: "hidden" }} title="Canvas theme — Dark / Light / Custom (editable)">
          {CANVAS_PRESETS.map((p) => (
            <button key={p.id} onClick={() => setActivePreset(p.id as CanvasPreset)}
                    title={p.locked ? `${p.label} (locked preset)` : `${p.label} (editable)`}
                    style={{ border: 0, padding: "3px 9px", fontSize: 12, cursor: "pointer",
                             background: activePreset === p.id ? t("node.seed.marker") : t("ui.panel.elevated"),
                             color: activePreset === p.id ? t("ui.onAccent") : t("ui.text") }}>{p.label}</button>
          ))}
        </span>
        <button onClick={() => setShowThemeCustomize(true)} style={{ ...field, cursor: "pointer" }}
                title="Customize the Custom preset's colors">🎨</button>
        <span style={{ display: "flex", gap: 4, alignItems: "center" }}
              title="Save the current view as a standalone exhibit image (print-light; does not change the case)">
          <span style={{ color: t("ui.muted"), fontSize: 12 }}>Exhibit</span>
          <button onClick={() => exportImage("svg")} style={{ ...field, cursor: "pointer" }}
                  title="Vector SVG — preferred for court exhibits">SVG</button>
          <button onClick={() => exportImage("png")} style={{ ...field, cursor: "pointer" }}
                  title="Raster PNG">PNG</button>
        </span>
        <button onClick={loadView} style={{ ...field, cursor: "pointer" }}>Reload</button>
        {/* P8.6 — value basis: USD value-at-time vs raw native units (per-asset). Drives labels, edge
            thickness, the dust/value-filter thresholds, and ordering. Remembered per case. */}
        <span style={{ display: "inline-flex", border: `1px solid ${t("ui.border")}`, borderRadius: 6,
                       overflow: "hidden" }}
              title="Value basis: USD value-at-time, or native units (ETH/BTC). Native ranks/scales within an asset.">
          {(["usd", "native"] as ValueBasis[]).map((b) => (
            <button key={b} onClick={() => setBasis(b)}
                    style={{ border: 0, padding: "3px 9px", fontSize: 12, cursor: "pointer",
                             background: view.valueBasis === b ? t("node.seed.marker") : t("ui.panel.elevated"),
                             color: view.valueBasis === b ? t("ui.onAccent") : t("ui.text") }}>
              {b === "usd" ? "USD" : "Native"}</button>
          ))}
        </span>
        <label style={ctrl}>
          <input type="checkbox" checked={view.groupDust} onChange={(e) => setV({ groupDust: e.target.checked })} />
          Group dust
        </label>
        <label style={ctrl}
               title="Cluster counterparties sharing one exact native amount (mixer pools, e.g. 100 ETH)">
          <input type="checkbox" checked={view.groupDenominations}
                 onChange={(e) => setV({ groupDenominations: e.target.checked })} />
          Group denominations
        </label>
        {/* P8.7 de-noise: reveal collapsed unverified/spam tokens; stop folding poison; per-denom filters. */}
        <label style={ctrl}
               title="Show unverified / unpriced tokens (airdrop & poison spam) — collapsed by default. A display de-emphasis, not a malice claim.">
          <input type="checkbox" checked={view.showUnverified}
                 onChange={(e) => setV({ showUnverified: e.target.checked })} />
          Show spam{meta?.unverified_token_edges ? ` (${meta.unverified_token_edges})` : ""}
        </label>
        <label style={ctrl}
               title="Fold likely address-poisoning (0-value look-alike) transfers out of the way (heuristic, reversible)">
          <input type="checkbox" checked={view.foldPoison}
                 onChange={(e) => setV({ foldPoison: e.target.checked })} />
          Fold poison{meta?.poison_suspect_edges ? ` (${meta.poison_suspect_edges})` : ""}
        </label>
        <button onClick={() => setShowDenomPanel(true)} style={{ ...field, cursor: "pointer" }}
                title="Per-denomination min/fold thresholds (each asset filtered in its own native units)">
          Denoms{Object.keys(view.denomFilters).length ? ` (${Object.keys(view.denomFilters).length})` : ""}
        </button>
        <button onClick={() => setShowClusteringPanel(true)} style={{ ...field, cursor: "pointer" }}
                title="Clustering heuristics (BlockSci change / Victor EVM / Leiden community) — apply, undo, preview">
          Clustering{meta?.community_groups ? ` (${meta.community_groups})` : ""}
        </button>
        <label style={ctrl}>
          <input type="checkbox" checked={view.onlyFlagged} onChange={(e) => setV({ onlyFlagged: e.target.checked })} />
          Flagged only
        </label>
        <label style={ctrl}>
          min&nbsp;{unitLabel}
          <input type="number" min={0} value={view.valueFloor} style={{ ...field, width: 64 }}
                 onChange={(e) => setV({ valueFloor: Math.max(0, Number(e.target.value) || 0) })} />
        </label>
        <label style={ctrl}
               title={isNative
                 ? `Fold movements below this many ${nativeUnit} into a separate bucket`
                 : "Fold PRICED movements below this USD value into a separate user_dust bucket (unpriced stay visible)"}>
          <input type="checkbox" checked={view.userDustOn} onChange={(e) => setV({ userDustOn: e.target.checked })} />
          fold&nbsp;&lt;&nbsp;{unitLabel}
          <input type="number" min={0} value={view.userDustUsd} style={{ ...field, width: 64 }}
                 onChange={(e) => setV({ userDustUsd: Math.max(0, Number(e.target.value) || 0) })} />
        </label>
        {view.ordering && (
          <span style={{ ...ctrl, color: t("ui.text.secondary") }}
                title="Right-click a node to order its neighbors; this orders along the x-axis">
            ordered by {view.ordering.mode}
            <button onClick={() => applyOrdering(null)} style={{ ...field, cursor: "pointer", padding: "1px 7px" }}>clear</button>
          </span>
        )}
        <label style={ctrl}>
          hops
          <input type="number" min={1} max={4} value={view.hops} style={{ ...field, width: 48 }}
                 onChange={(e) => setV({ hops: Math.min(4, Math.max(1, Number(e.target.value) || 1)) })} />
        </label>
        {hasTrace && (
          <label style={ctrl}>
            <input type="checkbox" checked={focusTrace} onChange={(e) => setFocusTrace(e.target.checked)} />
            Focus trace
          </label>
        )}
        <FontStepper label="Graph A" value={graphFont} onChange={setGraphFont} />
        <FontStepper label="Panel A" value={panelFont} onChange={setPanelFont} />
        <span style={{ display: "flex", gap: 12, flexWrap: "wrap", color: t("ui.muted"), fontSize: 12 }}>
          {legend.map((item) => (
            <span key={item.label} style={{ whiteSpace: "nowrap" }}>
              <Swatch item={item} />{item.label}
            </span>
          ))}
        </span>
      </header>

      {intelNote && (
        <div style={{ padding: "5px 14px", fontSize: 12, color: t("ui.text"),
                      background: t("ui.panel.elevated"), borderBottom: `1px solid ${t("node.entity.ring")}`,
                      display: "flex", gap: 8, alignItems: "center" }}>
          <b style={{ color: t("node.entity.ring") }}>Intel:</b> {intelNote}
          <span style={{ color: t("ui.muted") }}>— sourced claims written (side-by-side, never merged).</span>
          <button onClick={() => setIntelNote(null)} style={{ ...field, marginLeft: "auto", cursor: "pointer", padding: "1px 7px" }}>dismiss</button>
        </div>
      )}

      {meta && meta.focus && (
        <div style={{ padding: "4px 14px", fontSize: 12, color: t("ui.text.secondary"),
                      background: t("ui.panel.elevated"), borderBottom: `1px solid ${t("ui.border")}` }}>
          {meta.focus_label ? <>Focused on <b style={{ color: t("ui.text") }}>{meta.focus_label}</b> · </> : null}
          displaying <b style={{ color: t("ui.text") }}>{meta.displayed.toLocaleString()}</b> of{" "}
          <b style={{ color: t("ui.text") }}>{meta.total.toLocaleString()}</b> nodes
          {meta.bounded ? " (bounded view — click a dust group or a node to expand)" : ""}
          {meta.aggregated ? ` · ${meta.aggregated} dust group(s) collapsed` : ""}
        </div>
      )}

      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        {error ? (
          <p style={{ padding: 16, color: t("ui.error") }}>
            Could not load view: {error}. Is the backend running and a case migrated?
          </p>
        ) : !data ? (
          <p style={{ padding: 16 }}>Loading…</p>
        ) : data.nodes.length === 0 ? (
          <div style={{ padding: 16, color: t("ui.muted"), display: "flex", flexDirection: "column",
                        gap: 10, alignItems: "flex-start" }}>
            <p style={{ margin: 0 }}>This case has no on-chain data yet. Ingest an address to populate the graph.</p>
            <button onClick={() => setShowAddAddress(true)}
                    style={{ ...field, cursor: "pointer", borderColor: t("node.seed.marker"),
                             padding: "7px 14px", fontSize: 13 }}>+ Add address</button>
          </div>
        ) : (
          <Graph data={data} onSelect={handleSelect} onSelectEdge={handleSelectEdge}
                 onEditNode={handleEditNode} onExpand={handleExpand} onContextNode={handleContextNode}
                 ordering={view.ordering} focusTrace={focusTrace} labelScale={graphFont}
                 theme={activePreset} />
        )}
        <SidePanel node={selected} edge={selectedEdge} claims={claims} summary={summary} traces={traces}
                   annotations={annotations} nodesById={nodesById} onAddAnnotation={handleAddAnnotation}
                   onEditAnnotation={handleEditAnnotation} onDeleteAnnotation={handleDeleteAnnotation}
                   onSaveLabel={handleSaveLabel} onSaveTraceLabel={handleSaveTraceLabel}
                   onFocus={focusOn} fontScale={panelFont} />
      </div>

      {showFindings && (
        <FindingsPanel
          onClose={() => setShowFindings(false)}
          refreshKey={investRefresh}
          selected={selected}
          onFocus={(nodeId) => { focusOn(nodeId); setShowFindings(false); }}
          onChanged={() => { loadView(); reloadAnnotations(); setInvestRefresh((x) => x + 1); }}
        />
      )}

      {showPicker && (
        <CasePicker active={activeCase} onOpened={handleCaseOpened} onClose={() => setShowPicker(false)} />
      )}

      {showAddAddress && (
        <AddAddress onClose={() => setShowAddAddress(false)} onIngested={handleIngested}
                    currentGraph={data} onOpenSettings={() => setShowSettings(true)}
                    onValued={() => loadView()} />
      )}

      {showDenomPanel && (
        <DenomPanel denominations={meta?.denominations ?? []} filters={view.denomFilters}
                    onChange={(denomFilters) => setV({ denomFilters })}
                    onClose={() => setShowDenomPanel(false)} />
      )}

      {showClusteringPanel && (
        <ClusteringPanel onChanged={() => loadView()} onClose={() => setShowClusteringPanel(false)}
                         community={view.community} onToggleCommunity={(on) => setV({ community: on })}
                         communityNote={meta?.community_note} />
      )}

      {showSettings && <SettingsPanel onClose={() => setShowSettings(false)} />}

      {showThemeCustomize && <ThemeCustomize onClose={() => setShowThemeCustomize(false)} />}

      {menu && (
        <>
          {/* A transparent backdrop: a LEFT click dismisses the menu. We must NOT close on the
              contextmenu event — the right-click that OPENED the menu fires its own `contextmenu`
              immediately afterward, landing on this freshly-rendered backdrop; closing there would shut
              the menu the instant it opened. So just suppress the native menu here. */}
          <div onClick={() => setMenu(null)} onContextMenu={(e) => e.preventDefault()}
               style={{ position: "fixed", inset: 0, zIndex: 60 }} />
          <div style={{ position: "fixed", left: menu.x, top: menu.y, zIndex: 61,
                        background: t("ui.panel.elevated"), border: `1px solid ${t("ui.border")}`,
                        borderRadius: 6, padding: 4, display: "flex", flexDirection: "column",
                        minWidth: 200, boxShadow: "0 4px 18px rgba(0,0,0,0.45)" }}>
            <button onClick={() => applyOrdering("value", menu.nodeId)} style={menuItem}>Order neighbors by value (USD)</button>
            <button onClick={() => applyOrdering("native", menu.nodeId)} style={menuItem}>Order neighbors by {nativeUnit} amount</button>
            <button onClick={() => applyOrdering("sequence", menu.nodeId)} style={menuItem}>Order neighbors by sequence</button>
            {view.ordering && (
              <button onClick={() => applyOrdering(null)} style={menuItem}>Clear ordering</button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// A context-menu item style (catalog-tokened; no hardcoded hex).
const menuItem: React.CSSProperties = {
  background: "transparent", color: t("ui.text"), border: 0, borderRadius: 4,
  padding: "6px 10px", fontSize: 12, textAlign: "left", cursor: "pointer", whiteSpace: "nowrap",
};
