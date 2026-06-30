// Ordered layout (P3.5 feature 1) — a PURE positional model. Right-clicking a node orders THAT node's
// neighbors (not a global re-rank) along the x-axis, keyed by the connecting edge: by the movement's USD
// for "order by value", by the movement's tx block_height for "order by sequence". Lowest/oldest on the
// left → biggest/newest on the right. Neighbors whose connecting edge lacks the key (unpriced for value;
// NULL-height/mempool for sequence) are NOT ordered — they drop to a tray below the axis (strikethrough).
//
// This module only COMPUTES positions + which neighbors are trayed + a single global size multiplier; it
// mutates nothing. Graph.tsx applies the result as a Cytoscape `preset` layout (replacing fcose) and
// re-styles at the size multiplier. A view artifact — it never touches case.db.

import type { GraphData, GraphEdge } from "./Graph";

export type OrderMode = "value" | "sequence" | "native";
export type OrderState = { anchor: string; mode: OrderMode };

export type OrderingResult = {
  positions: Record<string, { x: number; y: number }>;
  trayed: Set<string>; // neighbor node ids with no key for this mode → strikethrough tray
  sizeScale: number; // global icon/path multiplier to fit the layout (orthogonal to value→thickness)
};

const DX = 150; // horizontal spacing between ranked neighbors
const ANCHOR_Y = -220; // the anchor sits above the neighbor axis
const ROW_Y = 0; // the ordered-neighbor axis
const TRAY_Y = 170; // missing-key neighbors, a tray BELOW the axis
const CTX_Y = -400; // any non-neighbor context node, a row up top

const FACT_KINDS = new Set(["transfer", "tx_input", "tx_output"]);

/** The ordering key for an edge under a mode: USD value (value), the connecting movement's tx
 *  block_height (sequence), or the NATIVE amount (native — P8.6, ranked within its asset). */
export function edgeKey(e: GraphEdge, mode: OrderMode): number | null {
  if (mode === "value") return typeof e.value_usd === "number" ? e.value_usd : null;
  if (mode === "native") return typeof e.value_num === "number" && e.value_num > 0 ? e.value_num : null;
  return typeof e.seq === "number" ? e.seq : null; // sequence: seq absent ⇒ NULL/mempool ⇒ tray
}

/** The asset of the connecting edge (for per-asset native ordering); "" for non-native modes. */
function edgeAsset(e: GraphEdge, mode: OrderMode): string {
  return mode === "native" ? (e.asset_symbol || "?") : "";
}

export function computeOrdering(data: GraphData, anchor: string, mode: OrderMode): OrderingResult {
  const nodeIds = new Set(data.nodes.map((n) => n.id));
  // The anchor's incident FACT edges → the connected neighbor and its strongest key (largest value /
  // latest height), so a node with several edges to the anchor still gets one deterministic rank.
  const best = new Map<string, number | null>();
  const asset = new Map<string, string>();
  for (const e of data.edges) {
    if (!FACT_KINDS.has(e.kind)) continue; // trace conventions + aggregate bundles don't order
    let nb: string | null = null;
    if (e.source === anchor) nb = e.target;
    else if (e.target === anchor) nb = e.source;
    if (nb == null || nb === anchor || !nodeIds.has(nb)) continue;
    const k = edgeKey(e, mode);
    if (!best.has(nb)) { best.set(nb, k); asset.set(nb, edgeAsset(e, mode)); }
    else {
      const prev = best.get(nb)!;
      if (k != null && (prev == null || k > prev)) { best.set(nb, k); asset.set(nb, edgeAsset(e, mode)); }
    }
  }

  const ordered: { id: string; key: number; asset: string }[] = [];
  const trayed = new Set<string>();
  for (const [nb, k] of best) {
    if (k == null) trayed.add(nb);
    else ordered.push({ id: nb, key: k, asset: asset.get(nb) || "" });
  }
  // Native mode ranks PER ASSET (native amounts aren't comparable across assets): group by asset, then by
  // amount within. USD/sequence ignore asset (one global scale).
  ordered.sort((a, b) => a.asset.localeCompare(b.asset) || a.key - b.key || a.id.localeCompare(b.id));

  const positions: Record<string, { x: number; y: number }> = {};
  const span = Math.max(0, ordered.length - 1) * DX;
  positions[anchor] = { x: span / 2, y: ANCHOR_Y }; // centered above its ranked neighbors
  ordered.forEach((o, i) => {
    // a small alternating vertical jitter spreads labels without breaking the L→R rank
    positions[o.id] = { x: i * DX, y: ROW_Y + (i % 2 ? 34 : 0) };
  });
  const trayList = [...trayed].sort();
  trayList.forEach((id, i) => {
    positions[id] = { x: i * DX, y: TRAY_Y };
  });

  // Any other node (multi-hop context, group parents, aggregates) gets a deterministic top row so the
  // layout stays fully positional (nothing floats with leftover fcose coordinates).
  const placed = new Set<string>([anchor, ...ordered.map((o) => o.id), ...trayList]);
  let ctx = 0;
  for (const n of data.nodes) {
    if (placed.has(n.id)) continue;
    positions[n.id] = { x: ctx * DX, y: CTX_Y };
    ctx += 1;
  }

  // More neighbors → smaller icons so they fit the row; clamped. Orthogonal to the value→thickness map.
  const count = ordered.length + trayList.length;
  const sizeScale = Math.min(1.4, Math.max(0.6, Math.round((1.5 - count * 0.03) * 100) / 100));

  return { positions, trayed, sizeScale };
}
