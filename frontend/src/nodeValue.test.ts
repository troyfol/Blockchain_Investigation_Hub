import { describe, expect, it } from "vitest";
import { valuationState } from "./nodeValue";
import type { NodeValue } from "./Graph";

const v = (over: NodeValue): NodeValue => over;

describe("valuationState — unpriced ≠ zero (P30/UX-09)", () => {
  it("a USD figure on either side -> valued", () => {
    expect(valuationState(v({ in_usd: 100 }))).toBe("valued");
    expect(valuationState(v({ out_usd: 50, in_native: 2, native_symbol: "ETH" }))).toBe("valued");
  });
  it("native movement but no USD -> unvalued (the honest 'no valuation yet' case)", () => {
    expect(valuationState(v({ in_native: 5, out_native: 3, native_symbol: "ETH" }))).toBe("unvalued");
    expect(valuationState(v({ in_native: 5, in_usd: null, out_usd: null }))).toBe("unvalued");
  });
  it("a zero-USD sum arrives as null (not 0) from the read-model, so it reads as unvalued not valued", () => {
    // graph.py emits `in_usd: iv or None` — an unpriced node's USD is null, never 0.
    expect(valuationState(v({ in_native: 1, in_usd: null }))).toBe("unvalued");
  });
  it("no value at all -> none (null, undefined, or an all-empty summary → no valuation line)", () => {
    expect(valuationState(null)).toBe("none");
    expect(valuationState(undefined)).toBe("none");
    expect(valuationState(v({}))).toBe("none");
    expect(valuationState(v({ in_usd: null, in_native: null, native_symbol: "ETH" }))).toBe("none");
  });
});
