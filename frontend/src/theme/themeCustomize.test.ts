import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildCytoscapeStyle, CANVAS_PRESETS, type CanvasPreset, clearCustomOverride, currentColor,
  customDefault, getActivePreset, getCustomOverrides, hasOverride, isLockedPreset,
  resetCustomOverrides, setActivePreset, setCustomOverride, t, themeValue,
} from "./theme";
import catalog from "./tokens.json";

// P6 — the in-canvas theme switcher + the Customize-colors editor logic (all in theme.ts; the React
// components are thin wrappers over these tested functions). A fake localStorage proves persistence.
const THEME_KEYS = ["neo-tokyo-night", "dark", "light", "print-light"];

let store: Map<string, string>;
beforeEach(() => {
  store = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
    removeItem: (k: string) => { store.delete(k); },
    clear: () => store.clear(), key: () => null, length: 0,
  });
  setActivePreset("custom");
  resetCustomOverrides();
});
afterEach(() => {
  setActivePreset("custom");
  resetCustomOverrides();
  vi.unstubAllGlobals();
});

describe("canvas presets — Dark · Light · Custom (two locked)", () => {
  it("exposes exactly the three presets; dark/light locked, custom editable", () => {
    expect(CANVAS_PRESETS.map((p) => p.id)).toEqual(["dark", "light", "custom"]);
    expect(isLockedPreset("dark")).toBe(true);
    expect(isLockedPreset("light")).toBe(true);
    expect(isLockedPreset("custom")).toBe(false);
  });

  it("the Custom base is the Neo-Tokyo palette (unchanged)", () => {
    setActivePreset("custom");
    expect(t("canvas.background")).toBe(themeValue("canvas.background", "neo-tokyo-night"));
  });
});

describe("switching the active preset is instant + persists", () => {
  it("switching to dark/light re-resolves every token immediately and persists the choice", () => {
    setActivePreset("dark");
    expect(getActivePreset()).toBe("dark");
    expect(t("canvas.background")).toBe(themeValue("canvas.background", "dark"));
    expect(store.get("bih.themePreset")).toBe("dark");  // persisted to localStorage

    setActivePreset("light");
    expect(t("ui.text")).toBe(themeValue("ui.text", "light"));
    expect(store.get("bih.themePreset")).toBe("light");
  });

  it("the built Cytoscape stylesheet follows the active preset", () => {
    setActivePreset("dark");
    const addr = buildCytoscapeStyle().find((r) => r.selector === 'node[kind="address"]');
    expect(addr!.style["background-color"]).toBe(themeValue("node.address.fill", "dark"));
    setActivePreset("light");
    const addr2 = buildCytoscapeStyle().find((r) => r.selector === 'node[kind="address"]');
    expect(addr2!.style["background-color"]).toBe(themeValue("node.address.fill", "light"));
  });
});

describe("customize editor — edits ONLY the Custom preset", () => {
  it("an override applies live, persists, and never leaks into dark/light", () => {
    setActivePreset("custom");
    setCustomOverride("node.address.fill", "#123456");
    expect(t("node.address.fill")).toBe("#123456");                       // live
    expect(getCustomOverrides()["node.address.fill"]).toBe("#123456");
    expect(JSON.parse(store.get("bih.themeOverrides")!)["node.address.fill"]).toBe("#123456"); // persisted

    setActivePreset("dark");
    expect(t("node.address.fill")).toBe(themeValue("node.address.fill", "dark"));  // no leak into locked
    setActivePreset("light");
    expect(t("node.address.fill")).toBe(themeValue("node.address.fill", "light"));
  });

  it("an override attempt while a LOCKED preset is active is REJECTED", () => {
    setActivePreset("dark");
    expect(() => setCustomOverride("node.address.fill", "#000000")).toThrow(/locked/i);
    expect(() => clearCustomOverride("node.address.fill")).toThrow(/locked/i);
    setActivePreset("light");
    expect(() => setCustomOverride("ui.text", "#000000")).toThrow(/locked/i);
  });

  it("'Reset Custom to defaults' restores the Neo-Tokyo palette", () => {
    setActivePreset("custom");
    setCustomOverride("canvas.background", "#abcabc");
    expect(t("canvas.background")).toBe("#abcabc");
    resetCustomOverrides();
    expect(getCustomOverrides()).toEqual({});
    expect(hasOverride("canvas.background")).toBe(false);
    expect(t("canvas.background")).toBe(themeValue("canvas.background", "neo-tokyo-night"));
  });

  it("a per-token reset reverts just that token, leaving other overrides", () => {
    setActivePreset("custom");
    setCustomOverride("ui.text", "#ffffff");
    setCustomOverride("ui.border", "#eeeeee");
    clearCustomOverride("ui.text");
    expect(hasOverride("ui.text")).toBe(false);
    expect(t("ui.text")).toBe(customDefault("ui.text"));   // back to Neo-Tokyo default
    expect(currentColor("ui.text")).toBe(customDefault("ui.text"));
    expect(hasOverride("ui.border")).toBe(true);           // the other override stays
  });
});

describe("report/exhibit stay print-light under every canvas preset", () => {
  it("the print-light (exhibit/report) palette is independent of the active canvas preset", () => {
    for (const preset of ["custom", "dark", "light"] as CanvasPreset[]) {
      setActivePreset(preset);
      const exhibit = buildCytoscapeStyle(1, "print-light");
      const addr = exhibit.find((r) => r.selector === 'node[kind="address"]');
      expect(addr!.style["background-color"]).toBe(themeValue("node.address.fill", "print-light"));
    }
    // ...while the live default still follows the (dark) canvas preset.
    setActivePreset("dark");
    const live = buildCytoscapeStyle().find((r) => r.selector === 'node[kind="address"]');
    expect(live!.style["background-color"]).toBe(themeValue("node.address.fill", "dark"));
  });
});

describe("full token coverage + no hardcoded hex across all themes", () => {
  it("every token defines a value under all four catalog themes", () => {
    for (const tk of catalog.tokens as { id: string; values: Record<string, string> }[]) {
      for (const th of THEME_KEYS) {
        expect(tk.values[th], `token ${tk.id} missing value for ${th}`).toBeTruthy();
      }
    }
  });

  it("the dark + light stylesheets resolve EVERY color to a catalog value (no stray hex)", () => {
    for (const th of ["dark", "light"] as CanvasPreset[]) {
      setActivePreset(th);
      const catalogValues = new Set((catalog.tokens as { values: Record<string, string> }[])
        .map((tk) => tk.values[th]));
      const colors = buildCytoscapeStyle().flatMap((r) =>
        Object.entries(r.style).filter(([k]) => /color/i.test(k)).map(([, v]) => String(v)));
      expect(colors.length).toBeGreaterThan(0);
      for (const v of colors) expect(catalogValues.has(v), `${v} not a ${th} catalog value`).toBe(true);
    }
  });
});

describe("dark + light — legibility + semantic distinctness", () => {
  const lum = (hex: string) => {
    const c = hex.replace("#", "");
    const ch = [0, 2, 4].map((i) => parseInt(c.slice(i, i + 2), 16) / 255)
      .map((x) => (x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4)));
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
  };
  const contrast = (a: string, b: string) => {
    const la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };

  it("UI + node-label text is legible on the background (WCAG contrast)", () => {
    for (const th of ["dark", "light"]) {
      expect(contrast(themeValue("ui.text", th), themeValue("ui.app.bg", th))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(themeValue("node.label.color", th), themeValue("canvas.background", th)))
        .toBeGreaterThanOrEqual(3);
    }
  });

  it("the semantic channels (risk/entity/fifo/value/annotation/seed/transfer/input) never collide", () => {
    for (const th of ["dark", "light"]) {
      const channels = [
        "node.risk.sanctioned.halo", "node.entity.ring", "edge.trace.fifo.line", "edge.tx_output.line",
        "node.annotation.ring", "node.seed.marker", "edge.transfer.line", "edge.tx_input.line",
      ].map((id) => themeValue(id, th).toLowerCase());
      expect(new Set(channels).size).toBe(channels.length);
      // risk must also stay clear of the value/output edge (a flow can't read as a risk glow).
      expect(themeValue("node.risk.sanctioned.halo", th)).not.toBe(themeValue("edge.tx_output.line", th));
    }
  });
});
