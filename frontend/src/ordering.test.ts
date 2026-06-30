import { describe, expect, it } from "vitest";
import { computeOrdering } from "./ordering";
import type { GraphData } from "./Graph";

// A focus anchor with three inbound transfers: B ($100, block 300), C ($5, block 100), D (unpriced /
// NULL height). D has neither an order key for "value" (no value_usd) nor for "sequence" (no seq).
function data(): GraphData {
  return {
    nodes: [
      { id: "addr:A", kind: "address", label: "A" },
      { id: "addr:B", kind: "address", label: "B" },
      { id: "addr:C", kind: "address", label: "C" },
      { id: "addr:D", kind: "address", label: "D" },
    ],
    edges: [
      { id: "e1", source: "addr:B", target: "addr:A", kind: "transfer", paradigm: "evm", value_usd: 100, seq: 300 },
      { id: "e2", source: "addr:C", target: "addr:A", kind: "transfer", paradigm: "evm", value_usd: 5, seq: 100 },
      { id: "e3", source: "addr:D", target: "addr:A", kind: "transfer", paradigm: "evm", no_price: true, seq_missing: true },
    ],
  };
}

describe("ordered layout (P3.5 feature 1) — arrange neighbors by the connecting edge", () => {
  it("orders neighbors ascending by the connecting edge's USD value; lowest on the left", () => {
    const r = computeOrdering(data(), "addr:A", "value");
    // C ($5) < B ($100): C sits to the LEFT of B along the x-axis.
    expect(r.positions["addr:C"].x).toBeLessThan(r.positions["addr:B"].x);
    // D has no value key -> it is NOT ordered; it is trayed.
    expect(r.trayed.has("addr:D")).toBe(true);
    expect(r.trayed.has("addr:B")).toBe(false);
  });

  it("orders neighbors by the connecting edge's tx block_height for 'sequence'", () => {
    const r = computeOrdering(data(), "addr:A", "sequence");
    // C (block 100) is older than B (block 300): C is to the LEFT.
    expect(r.positions["addr:C"].x).toBeLessThan(r.positions["addr:B"].x);
    // D has a missing height -> trayed (can't be ordered by sequence either).
    expect(r.trayed.has("addr:D")).toBe(true);
  });

  it("trays the missing-key neighbors BELOW the ordered axis", () => {
    const r = computeOrdering(data(), "addr:A", "value");
    // the trayed node sits below the ordered row (a larger y), visually separated.
    expect(r.positions["addr:D"].y).toBeGreaterThan(r.positions["addr:C"].y);
    expect(r.positions["addr:D"].y).toBeGreaterThan(r.positions["addr:B"].y);
  });

  it("returns a global size multiplier in a sane range (auto-fit, orthogonal to value→thickness)", () => {
    const r = computeOrdering(data(), "addr:A", "value");
    expect(r.sizeScale).toBeGreaterThanOrEqual(0.6);
    expect(r.sizeScale).toBeLessThanOrEqual(1.4);
  });

  it("ignores trace + aggregate edges (only real fact edges order neighbors)", () => {
    const d = data();
    d.nodes.push({ id: "addr:E", kind: "address", label: "E" });
    d.edges.push({ id: "t1", source: "addr:E", target: "addr:A", kind: "trace", paradigm: "trace", trace: "fifo" });
    const r = computeOrdering(d, "addr:A", "value");
    // E is reachable only via a trace convention -> never ordered and never trayed (not a fact neighbor).
    expect(r.trayed.has("addr:E")).toBe(false);
    expect(r.positions["addr:E"].y).toBeLessThan(r.positions["addr:C"].y); // pushed to the context row (top)
  });

  // P8.6 #4 — order by NATIVE amount, ranked PER ASSET.
  it("orders neighbors by native amount; unpriced large native still ranks (not trayed)", () => {
    const d: GraphData = {
      nodes: [
        { id: "addr:A", kind: "address", label: "A" },
        { id: "addr:B", kind: "address", label: "B" },
        { id: "addr:C", kind: "address", label: "C" },
      ],
      edges: [
        // both UNPRICED (no value_usd) — they'd both tray under "value", but native ranks them by amount.
        { id: "e1", source: "addr:B", target: "addr:A", kind: "transfer", paradigm: "evm", no_price: true, value_num: 100, asset_symbol: "ETH" },
        { id: "e2", source: "addr:C", target: "addr:A", kind: "transfer", paradigm: "evm", no_price: true, value_num: 1, asset_symbol: "ETH" },
      ],
    };
    const r = computeOrdering(d, "addr:A", "native");
    expect(r.trayed.size).toBe(0);                                  // both have a native key
    expect(r.positions["addr:C"].x).toBeLessThan(r.positions["addr:B"].x);  // 1 ETH left of 100 ETH
  });

  it("groups native ordering PER ASSET (same-asset together, never one combined native scale)", () => {
    const d: GraphData = {
      nodes: [
        { id: "addr:A", kind: "address", label: "A" },
        { id: "addr:BTC", kind: "address", label: "BTC" },
        { id: "addr:E1", kind: "address", label: "E1" },
        { id: "addr:E2", kind: "address", label: "E2" },
      ],
      edges: [
        { id: "b", source: "addr:BTC", target: "addr:A", kind: "transfer", paradigm: "evm", value_num: 0.5, asset_symbol: "BTC" },
        { id: "e1", source: "addr:E1", target: "addr:A", kind: "transfer", paradigm: "evm", value_num: 2, asset_symbol: "ETH" },
        { id: "e2", source: "addr:E2", target: "addr:A", kind: "transfer", paradigm: "evm", value_num: 9, asset_symbol: "ETH" },
      ],
    };
    const r = computeOrdering(d, "addr:A", "native");
    // BTC (asset "BTC") sorts before the ETH group (asset locale-order), regardless of raw amount; within
    // ETH, 2 is left of 9. A 0.5 BTC is NOT placed between the ETH amounts on one fake scale.
    expect(r.positions["addr:BTC"].x).toBeLessThan(r.positions["addr:E1"].x);
    expect(r.positions["addr:E1"].x).toBeLessThan(r.positions["addr:E2"].x);
  });
});
