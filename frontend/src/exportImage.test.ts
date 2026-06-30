import type { Core } from "cytoscape";
import { describe, expect, it, vi } from "vitest";
import { exportGraphImage, EXHIBIT_THEME } from "./exportImage";
import { buildCytoscapeStyle, themeValue } from "./theme/theme";

// The wrapper accepts a (Core & { svg? }); a structural stub stands in for it. Cast at the call site
// (the stub deliberately implements only the 3 methods the exporter touches).
const asCore = (c: unknown) => c as unknown as Core;

// The ACTUAL pixel/vector render (cy.png / cy.svg) is browser-only: cytoscape-svg needs `window` and a
// headless cytoscape "can not render images" — so it cannot run in the node unit env. We instead assert
// the export WRAPPER's real contract (the part that decides the exhibit's colors + format + that the
// live canvas is restored), plus the exhibit STYLESHEET that determines the rendered colors. A stub Core
// records the stylesheet applied at each step and returns representative artifacts.

type AppliedStyle = { selector: string; style: Record<string, unknown> }[];

function stubCy() {
  const styleCalls: AppliedStyle[] = [];
  const cy = {
    style(s: AppliedStyle) {
      styleCalls.push(s);
      return cy;
    },
    // cytoscape-svg's cy.svg(): build a representative SVG embedding the bg it was handed, so the test
    // can assert the exhibit background/colors flow through the wrapper.
    svg(opts: { bg: string; full: boolean; scale: number }) {
      return `<svg xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="${opts.bg}"/></svg>`;
    },
    // cytoscape's cy.png({output:'blob'}) returns a Blob in the browser; emulate a non-empty one.
    png(opts: { output: string; bg: string }) {
      if (opts.output !== "blob") throw new Error("expected output:'blob'");
      return new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: "image/png" });
    },
  };
  return { cy, styleCalls };
}

describe("graph image export — standalone exhibit (P3)", () => {
  it("SVG export is well-formed and uses the print-light catalog palette", () => {
    const { cy } = stubCy();
    const svgSpy = vi.spyOn(cy, "svg");

    const out = exportGraphImage(asCore(cy), "svg", { fontScale: 1 });

    expect(typeof out).toBe("string");
    const svg = out as string;
    expect(svg.startsWith("<svg")).toBe(true);
    expect(svg.includes("</svg>")).toBe(true);
    // The exhibit background is the print-light canvas token (not the dark on-screen one) — proving the
    // colors come from the catalog and the exhibit theme is applied.
    const lightBg = themeValue("canvas.background", EXHIBIT_THEME);
    const darkBg = themeValue("canvas.background", "neo-tokyo-night");
    expect(svg).toContain(lightBg);
    expect(svg).not.toContain(darkBg);
    expect(svgSpy).toHaveBeenCalledWith(expect.objectContaining({ bg: lightBg, full: true }));
  });

  it("PNG export returns a non-empty image Blob", () => {
    const { cy } = stubCy();
    const out = exportGraphImage(asCore(cy), "png", { fontScale: 1 });
    expect(out).toBeInstanceOf(Blob);
    expect((out as Blob).size).toBeGreaterThan(0);
    expect((out as Blob).type).toBe("image/png");
  });

  it("restores the live (dark) stylesheet after exporting — the on-screen canvas is never left light", () => {
    const { cy, styleCalls } = stubCy();
    exportGraphImage(asCore(cy), "svg", { fontScale: 1 });
    // Two style() applications: first the exhibit (print-light), then the restore (active dark theme).
    expect(styleCalls.length).toBe(2);
    const exhibitAddr = styleCalls[0].find((r) => r.selector === 'node[kind="address"]');
    const restoredAddr = styleCalls[1].find((r) => r.selector === 'node[kind="address"]');
    expect(exhibitAddr!.style["background-color"]).toBe(themeValue("node.address.fill", EXHIBIT_THEME));
    expect(restoredAddr!.style["background-color"]).toBe(themeValue("node.address.fill", "neo-tokyo-night"));
  });

  it("restores the live stylesheet even when the export throws", () => {
    const { cy, styleCalls } = stubCy();
    vi.spyOn(cy, "svg").mockImplementation(() => { throw new Error("render boom"); });
    expect(() => exportGraphImage(asCore(cy), "svg", { fontScale: 1 })).toThrow("render boom");
    expect(styleCalls.length).toBe(2);  // exhibit applied, then restored in `finally`
  });

  it("the exhibit stylesheet resolves entirely to print-light catalog tokens (no dark hex leaks)", () => {
    const exhibit = buildCytoscapeStyle(1, EXHIBIT_THEME);
    const flat = JSON.stringify(exhibit);
    // A few representative print-light token values are present, and their dark counterparts are not.
    for (const id of ["node.address.fill", "edge.transfer.line", "node.risk.sanctioned.halo"]) {
      expect(flat).toContain(themeValue(id, EXHIBIT_THEME));
      expect(flat).not.toContain(themeValue(id, "neo-tokyo-night"));
    }
  });
});
