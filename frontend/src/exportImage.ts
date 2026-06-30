// Standalone graph image export (P3) — save the CURRENT focused/filtered graph view as a court-ready
// exhibit. SVG (vector, preferred for exhibits) and PNG (raster). This is a VIEW ARTIFACT: it reads the
// live Cytoscape view and NEVER mutates case.db. The network viz is already embedded in the PDF report;
// this is the saveable standalone accompaniment.
//
// Theme-aware: the exhibit renders in the print-light palette so colors read on paper / in a filing,
// distinct from the dark on-screen canvas. Every color resolves through the token catalog (theme.ts) —
// no hardcoded hex. cytoscape-svg's `cy.svg()` is registered in the browser entry (App.tsx); this module
// stays import-safe for the (node) unit tests, which drive the wrapper with a stub Core.

import type { Core } from "cytoscape";
import { buildCytoscapeStyle, themeValue } from "./theme/theme";

export type ImageFormat = "png" | "svg";

// The exhibit palette: an ink-light theme so the saved image is legible on paper (the report uses the
// same print-light theme). Defined in the ONE token catalog — switching it switches both.
export const EXHIBIT_THEME = "print-light";

export const EXHIBIT_MIME: Record<ImageFormat, string> = {
  png: "image/png",
  svg: "image/svg+xml",
};

export type ExportOptions = {
  theme?: string; // catalog theme id for the exhibit (default: print-light)
  fontScale?: number; // the live graph-label scale, so the restored style matches the UI pref
  scale?: number; // raster/vector scale factor (sharper exhibit)
};

// Minimal structural type for the cytoscape-svg-augmented core (the plugin adds `svg()`; png() is built-in).
type ExportableCore = Core & {
  svg?: (opts: Record<string, unknown>) => string;
};

/**
 * Render the CURRENT graph view to an exhibit image and return it (Blob for PNG, SVG string for SVG).
 *
 * Temporarily applies the exhibit (print-light) stylesheet so the saved image reads on paper, exports,
 * then RESTORES the live (active-theme) stylesheet — even if the export throws. Pure with respect to the
 * case: it only re-styles + reads the in-memory Cytoscape view; it writes nothing to case.db.
 */
export function exportGraphImage(cy: ExportableCore, format: ImageFormat, opts: ExportOptions = {}): Blob | string {
  const theme = opts.theme ?? EXHIBIT_THEME;
  const fontScale = opts.fontScale ?? 1;
  const scale = opts.scale ?? 2;
  const bg = themeValue("canvas.background", theme);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  cy.style(buildCytoscapeStyle(fontScale, theme) as any);
  try {
    if (format === "svg") {
      if (typeof cy.svg !== "function")
        throw new Error("cytoscape-svg is not registered (cy.svg unavailable)");
      return cy.svg({ full: true, bg, scale });
    }
    return cy.png({ full: true, bg, scale, output: "blob" }) as Blob;
  } finally {
    // Restore the on-screen (active-theme) style so the live canvas is never left in exhibit colors.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    cy.style(buildCytoscapeStyle(fontScale) as any);
  }
}

/** Trigger a browser download of an exported image (a view artifact — no network, no case.db). */
export function downloadImage(data: Blob | string, format: ImageFormat, basename = "bih-graph"): void {
  const blob = typeof data === "string" ? new Blob([data], { type: EXHIBIT_MIME[format] }) : data;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${basename}.${format}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
