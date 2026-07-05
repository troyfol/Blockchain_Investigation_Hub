import { describe, expect, it } from "vitest";
import { colorDistance, contrastRatio, hexToRgb, relativeLuminance } from "./colorMath";

// P38/UX-13 — the pure color-math helpers behind the exhibit's AA-contrast + source-spacing guarantees.
// These lock the formulas to their WCAG / redmean anchors; the token-catalog assertions that USE them
// (report greys clear AA, source badges stay spaced) live in themeCustomize.test.ts.
describe("colorMath — WCAG contrast + perceptual distance (P38/UX-13)", () => {
  it("hexToRgb parses with/without '#' and rejects malformed hex", () => {
    expect(hexToRgb("#ffffff")).toEqual([255, 255, 255]);
    expect(hexToRgb("000000")).toEqual([0, 0, 0]);
    expect(hexToRgb("#4e79a7")).toEqual([78, 121, 167]);
    expect(() => hexToRgb("#fff")).toThrow();       // shorthand not supported
    expect(() => hexToRgb("nope")).toThrow();
  });

  it("relativeLuminance anchors at 0 (black) and 1 (white)", () => {
    expect(relativeLuminance("#000000")).toBeCloseTo(0, 6);
    expect(relativeLuminance("#ffffff")).toBeCloseTo(1, 6);
  });

  it("contrastRatio: black/white = 21, identical = 1, order-independent", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#777777", "#777777")).toBeCloseTo(1, 6);
    expect(contrastRatio("#123456", "#abcdef")).toBeCloseTo(contrastRatio("#abcdef", "#123456"), 6);
  });

  it("colorDistance: identical = 0, black/white ≈ 765 (redmean max), order-independent", () => {
    expect(colorDistance("#3b82f6", "#3b82f6")).toBe(0);
    expect(colorDistance("#000000", "#ffffff")).toBeGreaterThan(700);
    expect(colorDistance("#e05cf0", "#bf6bff")).toBeCloseTo(colorDistance("#bf6bff", "#e05cf0"), 6);
  });
});
