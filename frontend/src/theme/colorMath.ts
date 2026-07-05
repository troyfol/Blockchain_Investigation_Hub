// BIH color math — a DOM-free, dependency-free module of the color computations behind P38/UX-13's
// contrast + palette-spacing guarantees. Kept PURE (no React, no DOM, no localStorage) so the WCAG
// assertions on the token catalog are unit-testable in vitest's node environment (there is no jsdom in
// this repo, so component-render tests aren't available — pure helpers are how we earn honest coverage).
//
// Two families, one per P38 acceptance criterion:
//   • WCAG contrast — relativeLuminance() + contrastRatio(): the sRGB → linear-light → luminance → ratio
//     chain from WCAG 2.x. Used to prove the report's muted/empty greys clear AA (4.5:1) on the white
//     page (report.css `body` has no background), so the court-facing exhibit stays legible.
//   • Perceptual spacing — colorDistance(): Riemersma's "redmean", a low-cost approximation of CIE ΔE
//     (no matrix math). Used to prove two source badges (e.g. Chainalysis vs OFAC-SDN) never read as the
//     same color side-by-side — Invariant #4: sources stay visibly distinct, never merged.

/** "#rrggbb" (or bare "rrggbb") → [r, g, b] as integers 0..255. Throws on a malformed hex. */
export function hexToRgb(hex: string): [number, number, number] {
  const h = hex.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(h)) throw new Error(`not a 6-digit hex color: ${hex}`);
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

/** WCAG relative luminance (0 = black … 1 = white): each sRGB channel → linear-light → Rec.709 sum. */
export function relativeLuminance(hex: string): number {
  const lin = hexToRgb(hex).map((c) => {
    const x = c / 255;
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

/** WCAG contrast ratio between two colors (1 … 21), order-independent. AA body text needs ≥ 4.5. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a), lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** A low-cost perceptual color distance (Riemersma "redmean", ~0..765): larger = more visibly different,
 *  two near-identical colors score ~0. A pragmatic stand-in for CIE ΔE with no color-space conversion. */
export function colorDistance(a: string, b: string): number {
  const [r1, g1, b1] = hexToRgb(a);
  const [r2, g2, b2] = hexToRgb(b);
  const rmean = (r1 + r2) / 2;
  const dr = r1 - r2, dg = g1 - g2, db = b1 - b2;
  return Math.sqrt((2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db);
}
