import { useEffect, useRef } from "react";
import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
// @ts-expect-error — cytoscape-fcose ships no bundled types; it registers the "fcose" layout.
import fcose from "cytoscape-fcose";
import { computeOrdering, type OrderState } from "./ordering";
import { buildCytoscapeStyle, t } from "./theme/theme";

// Register the fast force-directed layout once (idempotent: cytoscape.use throws on a re-register, so
// guard it for HMR / repeated module eval in dev).
const w = window as unknown as { __fcoseRegistered?: boolean };
if (!w.__fcoseRegistered) {
  cytoscape.use(fcose);
  w.__fcoseRegistered = true;
}

export type GraphData = { nodes: GraphNode[]; edges: GraphEdge[]; meta?: ViewMeta };
export type ViewMeta = {
  focus: string | null; focus_label?: string; displayed: number; total: number;
  bounded: boolean; aggregated: number; hops: number; node_cap: number; group_dust: boolean;
  value_basis?: "usd" | "native"; denomination_groups?: number;   // P8.6
  value_min_usd?: number | null; value_max_usd?: number | null;
  // P8.7 de-noise state + signal counts + the denominations present (for the per-denomination panel).
  show_unverified?: boolean; fold_poison?: boolean;
  unverified_token_edges?: number; poison_suspect_edges?: number;
  denominations?: string[];
  community_groups?: number; community_note?: string | null;   // P8.8 Leiden (visual structure only)
};
export type NodeValue = {
  in_usd?: number | null; out_usd?: number | null;
  in_native?: number | null; out_native?: number | null; native_symbol?: string | null;
};
export type GraphNode = {
  id: string;
  kind: "address" | "transaction" | "external" | "group" | "aggregate" | "user_dust" | "unverified" | "poison";
  label: string;
  poison_suspect?: boolean;   // P8.7 #3 — a likely address-poisoning look-alike (heuristic, reversible)
  poison_confidence?: number;
  chain?: string;
  address?: string;
  tx_hash?: string;
  finality_status?: string;
  seq?: number;            // tx block_height (sequence ordering key); absent => seq_missing
  seq_missing?: boolean;   // NULL block_height / mempool — not orderable by sequence
  ordering_trayed?: boolean; // (frontend) set while ordering when this neighbor lacks the order key
  threshold_usd?: number;  // user_dust bucket threshold ("below $X")
  // Summary intelligence flags from the read-model (services/graph.py) that drive on-canvas styling.
  risk_level?: "sanctioned" | "elevated";
  has_attribution?: boolean;
  entity_label?: string;
  coinjoin?: boolean;
  seed?: boolean;          // the address the investigation started from (★ anchor)
  custom_label?: boolean;  // an investigator display-label override is in effect for this node
  val?: NodeValue;         // per-node value summary (received / sent, native + USD-at-time)
  parent?: string;         // compound-grouping parent id (co-spend cluster / sourced entity / denomination)
  group_type?: "cospend" | "entity" | "denomination" | "community";
  denomination?: string;   // (denomination group) the shared native amount label, e.g. "100 ETH"
  pool_size?: number;      // (denomination/community group) how many members
  community_index?: number;// (Leiden community) the community ordinal — VISUAL structure, not ownership
  // Aggregate (dust / high-fan-in summary) fields — display-only, expandable to the real underlying.
  agg_direction?: "in" | "out";
  agg_of?: string;
  count?: number;
  total_usd?: number | null;
  no_price_count?: number;
  is_more?: boolean;       // a ":more" residual bundle from an expand cap (click to show the rest)
};
export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  kind: "transfer" | "tx_input" | "tx_output" | "trace" | "aggregate" | "user_dust" | "unverified" | "poison";
  paradigm: string;
  is_token?: boolean;          // P8.7 — a non-native (ERC-20) movement
  token_unverified?: boolean;  // P8.7 #2 — unpriced + not allowlisted (de-emphasised; not a malice claim)
  poison_suspect?: boolean;    // P8.7 #3 — a 0-value look-alike transfer (heuristic)
  poison_confidence?: number;
  poison_lookalike?: string;   // the real address this mimics (first-K + last-K hex match)
  amount?: string;
  seq?: number;            // the connecting movement's tx block_height (the "order by sequence" key)
  seq_missing?: boolean;   // NULL/mempool — no sequence key (this neighbor goes to the tray)
  user_dust?: boolean;     // a value-filter aggregate edge (distinct from the auto dust aggregate)
  value_label?: string;    // formatted native value, e.g. "0.0115 BTC"
  value_usd?: number;      // USD value-at-time (DeFiLlama) — width + label when present
  value_usd_label?: string;
  value_contested?: boolean; // >1 source priced this movement (see node detail, not collapsed)
  no_price?: boolean;      // a value movement with no USD price (honest gap, de-emphasised)
  value_num?: number;      // numeric native value (drives native-basis width/ordering + unpriced width)
  asset_symbol?: string;   // the native unit (ETH/BTC/USDC) — native amounts compare only within an asset
  ew?: number;             // edge width ∝ value (read-model, log-scaled + clamped)
  finality_status?: string;
  trace?: string;          // 'fifo' | 'investigator' — a labeled convention, never a fact
  trace_id?: string;       // the parent trace's id (trace edges)
  trace_name?: string;     // the parent trace's display name (custom label over its name)
  label_dy?: number;       // per-edge label vertical offset (read-model) so trace labels don't stack
  // Investigator layer on a FLOW (transfer / tx_output): the durable target (ann_type/ann_id) the side
  // panel renames + annotates, plus the resulting custom display label and green-accent flag.
  ann_type?: "transfer" | "tx_output";
  ann_id?: string;
  custom_label?: string;   // investigator name for this flow — wins the on-canvas edge label
  has_annotation?: boolean;// ≥1 durable note on this flow -> green edge glow
  // P8.7.3 #3 — a parallel-edge rollup: ``count`` same-(source,target,asset) movements folded into one
  // legible display edge (value summed; the ×N is baked into value_label). ``underlying`` are the real
  // movement ids it stands for (drill-down) — a display rollup over real facts, never a synthesized fact.
  parallel_aggregate?: boolean;
  count?: number;
  no_price_count?: number;
  underlying?: string[];
};

// The Cytoscape stylesheet is built from the single token catalog (frontend/src/theme/) — no hardcoded
// hex here, and at the active label-font scale (UI pref). The report renders the same encodings from the
// Python twin (backend/app/theme.py).

// Build top→bottom flow constraints from edges (source above target) so address→tx→address reads as a
// downward money-flow rather than a blob. Skipped for compound graphs (fcose constraints + compounds
// don't mix) and very large graphs (perf); plain fcose still lays those out.
function flowConstraints(data: GraphData): { top: string; bottom: string; gap: number }[] {
  if (data.nodes.some((n) => n.kind === "group") || data.nodes.length > 400) return [];
  const seen = new Set<string>();
  const out: { top: string; bottom: string; gap: number }[] = [];
  for (const e of data.edges) {
    // Bundles/overlays don't imply a money-flow direction; skip them.
    if (e.kind === "aggregate" || e.kind === "trace" || e.source === e.target) continue;
    const key = `${e.source}>${e.target}`;
    const rev = `${e.target}>${e.source}`;
    // Skip duplicates AND CONTRADICTIONS: a bidirectional pair (A↔B, common on a high-degree hub) would
    // emit "A above B" and "B above A", which makes fcose's constraint solver fail to converge (it hangs
    // the whole canvas on a dense focused view). Keep only the first direction seen for each pair.
    if (seen.has(key) || seen.has(rev)) continue;
    seen.add(key);
    out.push({ top: e.source, bottom: e.target, gap: 45 });
    if (out.length >= 200) break;
  }
  return out;
}

export default function Graph({ data, onSelect, onSelectEdge, onEditNode, onExpand, onContextNode,
                                ordering = null, focusTrace = false, labelScale = 1, theme = "custom" }: {
  data: GraphData;
  onSelect: (n: GraphNode | null) => void;
  onSelectEdge?: (e: GraphEdge | null) => void;  // tap a flow/edge to inspect + rename + annotate it
  onEditNode?: (n: GraphNode) => void;   // double-click a node to rename it (investigator label)
  onExpand?: (aggId: string) => void;    // click a dust-aggregate node to expand to its real underlying
  onContextNode?: (nodeId: string, pos: { x: number; y: number }) => void;  // right-click -> order menu
  ordering?: OrderState | null;          // active ordered-layout (P3.5 feature 1); null => fcose
  focusTrace?: boolean;
  labelScale?: number;                   // graph-label font multiplier (UI pref; independent of zoom)
  theme?: string;                        // active canvas preset (P6) — restyle on change, no relayout
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  // Track the latest label-font scale in a ref so the (data-driven) cy-rebuild effect picks it up
  // WITHOUT re-running on a font change — the font change re-applies the stylesheet in a separate effect
  // below (no relayout, window.__cy preserved).
  const labelScaleRef = useRef(labelScale);
  labelScaleRef.current = labelScale;
  // Track the active canvas preset in a ref so the data-driven rebuild uses the current theme without
  // re-running on a theme change (the theme switch re-styles in a dedicated effect — no relayout).
  const themeRef = useRef(theme);
  themeRef.current = theme;
  // The active ordering size multiplier, so the font-scale effect can preserve it when it re-styles.
  const sizeScaleRef = useRef(1);

  useEffect(() => {
    if (!containerRef.current) return;
    // P3.5 feature 1: when ordering is active, lay the anchor's neighbors out POSITIONALLY (x = rank by
    // the connecting edge's value/sequence; missing-key neighbors trayed below) — a deterministic preset
    // layout that REPLACES fcose, with a global size multiplier so the icons/paths fit. Reverts to fcose
    // when ordering is null.
    // Only order when the anchor is actually in the current view; otherwise fall back to fcose (e.g. a
    // filter change aggregated the anchor away). Avoids a degenerate all-context layout.
    const ord = ordering && data.nodes.some((n) => n.id === ordering.anchor)
      ? computeOrdering(data, ordering.anchor, ordering.mode) : null;
    sizeScaleRef.current = ord ? ord.sizeScale : 1;
    const elements: ElementDefinition[] = [
      ...data.nodes.map((n) => ({
        data: ord && ord.trayed.has(n.id)
          ? ({ ...n, ordering_trayed: true } as unknown as Record<string, unknown>)
          : (n as unknown as Record<string, unknown>),
        ...(ord ? { position: { ...ord.positions[n.id] } } : {}),
      })),
      ...data.edges.map((e) => ({ data: e as unknown as Record<string, unknown> })),
    ];
    const constraints = flowConstraints(data);
    // fcose everywhere, with nodeDimensionsIncludeLabels so labels are counted as node footprint and
    // nodes are spaced to stop colliding. Kept deterministic (animate:false) so the window.__cy render
    // settles to a stable layout for E2E. Relative-placement constraints bias the flow top→bottom.
    const layout: any = ord
      ? { name: "preset", animate: false, fit: true, padding: 60 }   // positional ordering layout
      : {
          name: "fcose", animate: false, quality: "default", randomize: true,
          nodeDimensionsIncludeLabels: true, nodeSeparation: 140, idealEdgeLength: 110,
          nodeRepulsion: 6500, padding: 45,
          ...(constraints.length ? { relativePlacementConstraint: constraints } : {}),
        };
    // Build the stylesheet at the CURRENT label-font scale + the ordering size multiplier (the refs keep
    // them current without forcing a relayout when only the font changes — see the dedicated effect).
    const cy = cytoscape({ container: containerRef.current, elements,
                           style: buildCytoscapeStyle(labelScaleRef.current, themeRef.current, sizeScaleRef.current),
                           layout });

    // Single tap selects; a second tap on the SAME node within the threshold is a double-click that
    // opens the rename (investigator label). Detected manually so it works across Cytoscape versions.
    let lastTap = { id: "", t: 0 };
    cy.on("tap", "node", (evt) => {
      const node = evt.target.data() as GraphNode;
      // Expand a dust bundle OR a value-filter (user_dust) bundle to its real underlying counterparties.
      if (node.kind === "aggregate" || node.kind === "user_dust") { onExpand?.(node.id); return; }
      onSelectEdge?.(null);   // a node tap clears any selected flow
      onSelect(node);
      const now = typeof performance !== "undefined" ? performance.now() : Date.now();
      if (lastTap.id === node.id && now - lastTap.t < 350) {
        lastTap = { id: "", t: 0 };
        onEditNode?.(node);
      } else {
        lastTap = { id: node.id, t: now };
      }
    });
    // Tap a flow/edge to inspect it (its facts + value-at-time) and rename / annotate it (feature A2).
    cy.on("tap", "edge", (evt) => {
      const edge = evt.target.data() as GraphEdge;
      onSelect(null);          // an edge tap clears any selected node
      onSelectEdge?.(edge);
    });
    cy.on("tap", (evt) => {
      if (evt.target === cy) { onSelect(null); onSelectEdge?.(null); }  // background clears both
    });
    // Right-click a node -> open the ordering context menu (order this node's neighbors by value /
    // sequence). Only real nodes (address / transaction) anchor an ordering; bundles/groups don't. Use
    // the DOM event's page coords so App can position the menu; suppress the native browser menu below.
    cy.on("cxttap", "node", (evt) => {
      const node = evt.target.data() as GraphNode;
      if (node.kind !== "address" && node.kind !== "transaction") return;
      const oe = evt.originalEvent as MouseEvent | undefined;
      onContextNode?.(node.id, { x: oe?.clientX ?? 0, y: oe?.clientY ?? 0 });
    });
    const noNativeMenu = (e: Event) => e.preventDefault();
    const containerEl = containerRef.current;
    containerEl.addEventListener("contextmenu", noNativeMenu);

    // Hover tooltip: short labels lose no information — the full address (or tx hash) + key facts show
    // on hover. textContent only (no HTML injection); positioned at the rendered node position.
    const tip = tooltipRef.current;
    const showTip = (evt: any) => {
      if (!tip) return;
      const d = evt.target.data() as GraphNode;
      const lines: string[] = [];
      const usd = (v?: number | null) => (v == null ? null : `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`);
      if (d.kind === "aggregate") {
        lines.push(`${(d.count ?? 0).toLocaleString()} ${d.agg_direction === "in" ? "inflows" : "outflows"} (collapsed dust)`);
        if (d.total_usd) lines.push(`total ${usd(d.total_usd)} at the time`);
        if (d.no_price_count) lines.push(`${d.no_price_count.toLocaleString()} with no USD price`);
        lines.push("click to expand to the real underlying");
      } else if (d.kind === "address") {
        if (d.entity_label) lines.push(d.entity_label);
        if (d.address) lines.push(d.address);
        if (d.seed) lines.push("★ seed / anchor");
        if (d.risk_level === "sanctioned") lines.push("⛔ sanctioned");
        else if (d.risk_level) lines.push(`⚠ ${d.risk_level} risk`);
        if (d.coinjoin) lines.push("⚠ possible-coinjoin");
        if (d.val) {
          const sym = d.val.native_symbol ?? "";
          if (d.val.in_native != null) lines.push(`received ${d.val.in_native} ${sym}${d.val.in_usd != null ? ` (~${usd(d.val.in_usd)})` : ""}`.trim());
          else if (d.val.in_usd != null) lines.push(`received ~${usd(d.val.in_usd)}`);
          if (d.val.out_native != null) lines.push(`sent ${d.val.out_native} ${sym}${d.val.out_usd != null ? ` (~${usd(d.val.out_usd)})` : ""}`.trim());
          else if (d.val.out_usd != null) lines.push(`sent ~${usd(d.val.out_usd)}`);
        }
      } else if (d.kind === "transaction") {
        lines.push("transaction");
        if (d.tx_hash) lines.push(d.tx_hash);
      } else {
        lines.push(d.label || d.id);
      }
      tip.textContent = lines.join("\n");
      const p = evt.renderedPosition || { x: 0, y: 0 };
      tip.style.left = `${p.x + 14}px`;
      tip.style.top = `${p.y + 10}px`;
      tip.style.display = "block";
    };
    const hideTip = () => { if (tip) tip.style.display = "none"; };
    // Trace edges name the path on hover ("near the path") — the trace's display label + its convention.
    const showEdgeTip = (evt: any) => {
      if (!tip) return;
      const d = evt.target.data() as GraphEdge;
      if (d.kind !== "trace") return;
      const lines: string[] = [];
      if (d.trace_name) lines.push(d.trace_name);
      lines.push(d.trace === "fifo" ? "FIFO trace (convention)" : "investigator trace");
      tip.textContent = lines.join("\n");
      const p = evt.renderedPosition || { x: 0, y: 0 };
      tip.style.left = `${p.x + 14}px`;
      tip.style.top = `${p.y + 10}px`;
      tip.style.display = "block";
    };
    cy.on("mouseover", "node", showTip);
    cy.on("mouseout", "node", hideTip);
    cy.on("mouseover", "edge", showEdgeTip);
    cy.on("mouseout", "edge", hideTip);
    cy.on("pan zoom drag", hideTip);

    cyRef.current = cy;
    (window as unknown as { __cy?: Core }).__cy = cy;  // render hook for E2E verification

    // A short fade-in on each view change (focus / expand) so newly-rendered nodes appear rather than
    // snap. Container-level opacity only (does not touch the per-element provisional/no-price opacity
    // encodings, and leaves the settled layout — window.__cy — deterministic for E2E).
    const host = containerRef.current;
    if (host) {
      host.style.opacity = "0";
      requestAnimationFrame(() => {
        host.style.transition = "opacity 220ms ease-out";
        host.style.opacity = "1";
      });
    }

    return () => {
      containerEl.removeEventListener("contextmenu", noNativeMenu);
      cy.destroy();
      cyRef.current = null;
    };
  }, [data, onSelect, onSelectEdge, onEditNode, onExpand, onContextNode, ordering]);

  // Graph-label font size (feature 5): re-apply the stylesheet at the new multiplier WITHOUT rebuilding
  // the layout (so positions + window.__cy are preserved). Independent of the scroll-wheel zoom. Keeps
  // the active ordering size multiplier so a font change never drops the ordered-layout auto-sizing.
  useEffect(() => {
    const cy = cyRef.current;
    if (cy) cy.style(buildCytoscapeStyle(labelScale, themeRef.current, sizeScaleRef.current));
  }, [labelScale]);

  // Canvas theme switch (P6): re-apply the stylesheet for the new preset WITHOUT rebuilding the layout,
  // so node positions + window.__cy are preserved (the container background updates in the render below).
  useEffect(() => {
    const cy = cyRef.current;
    if (cy) cy.style(buildCytoscapeStyle(labelScaleRef.current, theme, sizeScaleRef.current));
  }, [theme]);

  // Trace focus mode: dim everything off the active trace spine and emphasize the spine (trace edges +
  // their endpoint nodes), so the investigator follows one flow. Applied without rebuilding the layout.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().removeClass("bih-faded bih-focus");
      if (!focusTrace) return;
      const spine = cy.edges('[kind="trace"]');
      if (spine.length === 0) return;
      const focusEles = spine.union(spine.connectedNodes());
      cy.elements().not(focusEles).addClass("bih-faded");
      focusEles.addClass("bih-focus");
    });
  }, [focusTrace, data]);

  return (
    <div style={{ position: "relative", flex: 1, height: "100%", minWidth: 0 }}>
      <div ref={containerRef} style={{ width: "100%", height: "100%", background: t("canvas.background") }} />
      <div ref={tooltipRef} style={{
        position: "absolute", display: "none", pointerEvents: "none", zIndex: 10,
        maxWidth: 280, whiteSpace: "pre-line", wordBreak: "break-all",
        background: t("node.label.bg"), color: t("node.label.color"),
        border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "5px 8px",
        fontSize: 11, lineHeight: 1.35,
      }} />
    </div>
  );
}
