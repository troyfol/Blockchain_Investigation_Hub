import { describe, expect, it } from "vitest";
import {
  buildCytoscapeStyle, CATALOG, DEFAULT_THEME, legendItems, seedBadgeImage, sourceColor,
  strikeBadgeImage, t, THEMES, themeValue,
} from "./theme";
import catalog from "./tokens.json";

// Find the stylesheet rule for an exact selector.
function rule(selector: string) {
  return buildCytoscapeStyle().find((r) => r.selector === selector);
}

// Find a rule within a specific built stylesheet (for comparing font scales).
function ruleIn(style: ReturnType<typeof buildCytoscapeStyle>, selector: string) {
  return style.find((r) => r.selector === selector);
}

describe("P8.7 de-noise styling + legend", () => {
  it("unverified + poison aggregate nodes resolve their own catalog tokens (no hardcoded hex)", () => {
    expect(rule('node[kind="unverified"]')!.style["background-color"]).toBe(t("node.unverified.fill"));
    expect(rule('node[kind="poison"]')!.style["border-color"]).toBe(t("node.poison.rim"));
  });
  it("a poison-suspect edge/node gets the heuristic poison outline", () => {
    expect(rule("edge[?poison_suspect]")!.style["line-color"]).toBe(t("edge.poison.line"));
    expect(rule("node[?poison_suspect]")!.style["outline-color"]).toBe(t("node.poison.rim"));
  });
  it("a sanctioned/attributed node draws ABOVE its parent group box (P8.7.1 #6 — halo survives grouping)", () => {
    // z-compound-depth:top lifts the flagged child over the semi-opaque denomination/co-spend fill so the
    // red halo + entity ring composite against the canvas, not the group box.
    expect(rule('node[risk_level="sanctioned"]')!.style["z-compound-depth"]).toBe("top");
    expect(rule('node[risk_level="elevated"]')!.style["z-compound-depth"]).toBe("top");
    expect(rule("node[?has_attribution]")!.style["z-compound-depth"]).toBe("top");
  });
  it("the legend surfaces unverified + poison ONLY when present", () => {
    const none = legendItems({ nodes: [{ kind: "address" }], edges: [] });
    expect(none.some((i) => /unverified/i.test(i.label))).toBe(false);
    const withSpam = legendItems({ nodes: [{ kind: "unverified" }], edges: [] });
    expect(withSpam.some((i) => /unverified/i.test(i.label))).toBe(true);
    const withPoison = legendItems({ nodes: [{ kind: "address", poison_suspect: true }], edges: [] });
    expect(withPoison.some((i) => /poison/i.test(i.label))).toBe(true);
  });
});

describe("graph theme — risk encoding", () => {
  it("a sanctioned node carries the risk style (halo + badge ring) from the catalog", () => {
    const r = rule('node[risk_level="sanctioned"]');
    expect(r).toBeDefined();
    // Unmistakable halo (underlay) + badge ring, both resolved from dedicated risk tokens.
    expect(r!.style["underlay-color"]).toBe(t("node.risk.sanctioned.halo"));
    expect(r!.style["underlay-opacity"]).toBeGreaterThan(0);
    expect(r!.style["border-color"]).toBe(t("node.risk.sanctioned.badge"));
    expect(r!.style["border-width"]).toBeGreaterThan(0);
  });

  it("the risk treatment is visually distinct from the selection ring (no collision)", () => {
    const selected = rule("node:selected");
    expect(selected!.style["border-color"]).toBe(t("node.selected.border"));
    // The selection ring color must differ from the sanctioned halo/badge, so they never read alike.
    expect(t("node.selected.border")).not.toBe(t("node.risk.sanctioned.halo"));
    expect(t("node.selected.border")).not.toBe(t("node.risk.sanctioned.badge"));
  });

  it("elevated (non-sanctioned) risk uses its own halo token", () => {
    const r = rule('node[risk_level="elevated"]');
    expect(r!.style["underlay-color"]).toBe(t("node.risk.elevated.halo"));
    expect(t("node.risk.elevated.halo")).not.toBe(t("node.risk.sanctioned.halo"));
  });
});

describe("graph theme — traceability (value + seed)", () => {
  it("fact edges expose their value: width ∝ value and the amount as a label", () => {
    const r = rule("edge[?value_label]");
    expect(r).toBeDefined();
    expect(r!.style["width"]).toBe("data(ew)");          // width scaled by value (read-model `ew`)
    expect(r!.style["label"]).toBe("data(value_label)"); // the formatted amount, e.g. "0.0115 BTC"
    expect(r!.style["text-background-color"]).toBe(t("edge.value.labelBg"));  // a plate, via the catalog
    expect(r!.style["min-zoomed-font-size"]).toBeGreaterThan(0);             // only shown at closer zoom
  });

  it("the seed/anchor node carries the seed marker as a centered badge image from the catalog", () => {
    const r = rule("node[?seed]");
    expect(r).toBeDefined();
    // The seed marker rides as a ★ badge IMAGE (not a text outline), so the label stays clean.
    expect(r!.style["background-image"]).toBe(seedBadgeImage());
    expect(r!.style["background-image"]).toContain(t("node.seed.marker").replace("#", "%23"));
    // the seed marker is its own distinct color, not reused from risk/entity encodings
    expect(t("node.seed.marker")).not.toBe(t("node.risk.sanctioned.halo"));
    expect(t("node.seed.marker")).not.toBe(t("node.entity.ring"));
  });

  it("the legend surfaces a seed entry when a seed node is present", () => {
    const items = legendItems({ nodes: [{ kind: "address", seed: true }], edges: [] });
    expect(items.some((i) => /seed/i.test(i.label) && i.color === t("node.seed.marker"))).toBe(true);
  });
});

describe("graph theme — convention vs. fact", () => {
  it("a FIFO trace edge is dashed + labeled, never styled like a ledger fact", () => {
    const fifo = rule('edge[trace="fifo"]');
    expect(fifo!.style["line-style"]).toBe("dashed");
    expect(fifo!.style["label"]).toBe("fifo");
    expect(fifo!.style["line-color"]).toBe(t("edge.trace.fifo.line"));
    // A convention must not share a fact edge's color.
    expect(t("edge.trace.fifo.line")).not.toBe(t("edge.transfer.line"));
    expect(t("edge.trace.fifo.line")).not.toBe(t("edge.tx_output.line"));
  });
});

describe("graph theme — no hardcoded color escapes the catalog", () => {
  it("every color value in the built stylesheet is a catalog token value", () => {
    const catalogValues = new Set(CATALOG.map((tk) => tk.value));
    const colorValues = buildCytoscapeStyle().flatMap((r) =>
      Object.entries(r.style)
        .filter(([k]) => /color/i.test(k))
        .map(([, v]) => String(v)),
    );
    expect(colorValues.length).toBeGreaterThan(0);
    for (const v of colorValues) {
      expect(catalogValues.has(v)).toBe(true);   // no stray literal hex — all colors via the catalog
    }
  });
});

describe("legend — context-aware from the catalog", () => {
  it("shows only element types present, and surfaces sanctioned risk", () => {
    const g = {
      nodes: [
        { kind: "address", risk_level: "sanctioned", has_attribution: true },
        { kind: "transaction" },
      ],
      edges: [{ kind: "tx_output" }, { kind: "tx_input" }],
    };
    const items = legendItems(g);
    const labels = items.map((i) => i.label);
    expect(labels.some((l) => /sanctioned/i.test(l))).toBe(true);     // headline risk surfaced
    expect(items.some((i) => i.color === t("node.risk.sanctioned.halo"))).toBe(true);
    // Only present types: there are no transfer edges, so no transfer legend entry.
    expect(labels.some((l) => /transfer/i.test(l))).toBe(false);
    // Present BTC edge types ARE shown (the old static legend omitted these). P35/UX-02 — the labels now name
    // the arrow SHAPE channel ("Bitcoin input/output (…)") rather than the raw token label.
    expect(labels.some((l) => /bitcoin input/i.test(l))).toBe(true);
    expect(labels.some((l) => /bitcoin output/i.test(l))).toBe(true);
  });
});

describe("source badges resolve from the same catalog", () => {
  it("known sources map to their token; unknown falls back to source.default", () => {
    expect(sourceColor("graphsense")).toBe(t("source.graphsense"));
    expect(sourceColor("ofac-sdn")).toBe(t("source.ofac-sdn"));
    expect(sourceColor("totally-unknown")).toBe(t("source.default"));
  });
});

describe("named themes — the customization-UI contract", () => {
  it("every token defines a value under EVERY declared theme (so a theme switch is always total)", () => {
    const themeIds = THEMES.map((th) => th.id);
    expect(themeIds).toContain("neo-tokyo-night");
    expect(themeIds).toContain("print-light");
    for (const tk of catalog.tokens as { id: string; values: Record<string, string> }[]) {
      for (const th of themeIds) {
        expect(tk.values[th], `token ${tk.id} missing value for theme ${th}`).toBeTruthy();
      }
    }
  });

  it("the app resolves the dark default (neo-tokyo-night); the report palette (print-light) stays light", () => {
    expect(DEFAULT_THEME).toBe("neo-tokyo-night");
    // t() resolves the active (default) theme.
    expect(t("canvas.background")).toBe(themeValue("canvas.background", "neo-tokyo-night"));
    // The two themes are genuinely different palettes — dark canvas vs light canvas.
    const darkBg = themeValue("canvas.background", "neo-tokyo-night");
    const lightBg = themeValue("canvas.background", "print-light");
    expect(darkBg).not.toBe(lightBg);
    expect(darkBg.toLowerCase()).toBe("#100c1e");   // near-black indigo (cool/violet)
    expect(lightBg.toLowerCase()).toBe("#fafafa");  // ink-light (report keeps this)
  });

  it("neon is reserved for meaning and stays mutually distinct under the dark theme", () => {
    // The seven semantic channels must each read differently (color-blind safety leans on shape too).
    const channels = {
      textAqua: t("node.label.color"),
      entityPink: t("node.entity.ring"),
      riskRed: t("node.risk.sanctioned.halo"),
      traceRed: t("edge.trace.fifo.line"),
      outputMagenta: t("edge.tx_output.line"),
      inputAmber: t("edge.tx_input.line"),
      txGold: t("node.transaction.rim"),
    };
    const values = Object.values(channels);
    expect(new Set(values).size).toBe(values.length);          // all seven distinct
    // The graph default text is aqua; the attributed-entity highlight is its own (pink) tone.
    expect(channels.textAqua).not.toBe(channels.entityPink);
    // Sanctioned risk and the FIFO convention are both RED but must stay distinct values (they are
    // separated by shape — node halo+ring vs dashed edge — and a deliberate hex delta).
    expect(channels.riskRed).not.toBe(channels.traceRed);
  });

  it("the risk halo stays distinct from every edge color (output is magenta but never the risk fuchsia)", () => {
    // Output edge is intentionally magenta again; the guarantee is that it (and every other edge) is
    // never the same value as the sanctioned-risk halo, so a flow can't be mistaken for a risk glow.
    const risk = t("node.risk.sanctioned.halo");
    const edgeColors = [
      t("edge.default.line"), t("edge.transfer.line"), t("edge.tx_input.line"),
      t("edge.tx_output.line"), t("edge.trace.fifo.line"), t("edge.trace.investigator.line"),
    ];
    for (const c of edgeColors) expect(c).not.toBe(risk);
  });

  it("the attributed-entity highlight is the pink rim/outline — NOT a recolored label text", () => {
    // A1 fix: the entity signal is the ring + a matching label OUTLINE (node.entity.ring); the label
    // TEXT stays the default aqua (no `color` override), so 'pink = attributed entity' reads as the
    // ring, not the text. Rim, outline, and the legend swatch all resolve to the SAME entity token.
    const r = rule("node[?has_attribution]");
    expect(r!.style["border-color"]).toBe(t("node.entity.ring"));
    expect(r!.style["text-outline-color"]).toBe(t("node.entity.ring"));
    expect(r!.style["color"]).toBeUndefined();                 // text stays the default node.label.color
    const legend = legendItems({ nodes: [{ kind: "address", has_attribution: true }], edges: [] });
    expect(legend.some((i) => /attributed/i.test(i.label) && i.color === t("node.entity.ring"))).toBe(true);
  });
});

describe("investigator UX — annotation green outline", () => {
  it("an annotated node carries a green OUTLINE (distinct from the entity/risk border rings), from the catalog", () => {
    const r = rule("node[?has_annotation]");
    expect(r).toBeDefined();
    // an OUTLINE (outside the border) so it coexists with the entity-pink / risk-red BORDER rings.
    expect(r!.style["outline-color"]).toBe(t("node.annotation.ring"));
    expect(Number(r!.style["outline-width"])).toBeGreaterThan(0);
    // the annotation green is its own channel — not the aqua label text, the teal transfer edge, or a ring.
    const green = t("node.annotation.ring");
    for (const other of ["node.label.color", "edge.transfer.line", "node.entity.ring",
                         "node.seed.marker", "node.risk.sanctioned.halo"]) {
      expect(green).not.toBe(t(other));
    }
  });

  it("the legend surfaces an 'Annotated' entry when an annotated node is present", () => {
    const items = legendItems({ nodes: [{ kind: "address", has_annotation: true }], edges: [] });
    expect(items.some((i) => /annotat/i.test(i.label) && i.color === t("node.annotation.ring"))).toBe(true);
  });
});

describe("scale layer — dust aggregate, USD value, no-price gap", () => {
  it("the dust-aggregate node + edge resolve from dedicated catalog tokens (dashed = expandable bundle)", () => {
    const n = rule('node[kind="aggregate"]');
    expect(n!.style["background-color"]).toBe(t("node.aggregate.fill"));
    expect(n!.style["border-color"]).toBe(t("node.aggregate.rim"));
    expect(n!.style["border-style"]).toBe("dashed");
    expect(n!.style["color"]).toBe(t("node.aggregate.label"));
    const e = rule('edge[kind="aggregate"]');
    expect(e!.style["line-color"]).toBe(t("edge.aggregate.line"));
    expect(e!.style["line-style"]).toBe("dashed");
  });

  it("USD value-at-time wins the edge label when present (the cross-asset figure), via the catalog", () => {
    const r = rule("edge[?value_usd_label]");
    expect(r!.style["label"]).toBe("data(value_usd_label)");
    expect(r!.style["color"]).toBe(t("edge.value.label"));
  });

  it("a no-price value movement is an honest gap — kept visible but de-emphasised (never a $0 flow)", () => {
    const r = rule("edge[?no_price]");
    expect(r).toBeDefined();
    expect(Number(r!.style["opacity"])).toBeLessThan(1);
    expect(Number(r!.style["opacity"])).toBeGreaterThan(0);
  });

  it("the legend surfaces a dust-aggregate entry when an aggregate node is present", () => {
    const items = legendItems({ nodes: [{ kind: "aggregate" }], edges: [] });
    expect(items.some((i) => /aggregate/i.test(i.label) && i.color === t("node.aggregate.rim"))).toBe(true);
  });
});

describe("investigator UX — relabel & annotate FLOWS (feature A)", () => {
  it("a relabeled flow's custom label WINS the edge label, via the catalog", () => {
    const r = rule("edge[?custom_label]");
    expect(r).toBeDefined();
    expect(r!.style["label"]).toBe("data(custom_label)");          // investigator name overrides value
    expect(r!.style["color"]).toBe(t("edge.value.label"));         // catalog color, not a stray hex
  });

  it("an annotated flow gets a green GLOW (underlay) from a dedicated catalog token", () => {
    const r = rule("edge[?has_annotation]");
    expect(r).toBeDefined();
    expect(r!.style["underlay-color"]).toBe(t("edge.annotation.glow"));
    expect(Number(r!.style["underlay-opacity"])).toBeGreaterThan(0);
    // the glow must stay distinct from the fact-edge colors it overlays, so a flow can't be mistaken.
    for (const c of ["edge.transfer.line", "edge.tx_output.line", "edge.tx_input.line"]) {
      expect(t("edge.annotation.glow")).not.toBe(t(c));
    }
  });

  it("the legend surfaces 'Annotated' when only a FLOW (not a node) is annotated", () => {
    const items = legendItems({ nodes: [{ kind: "address" }], edges: [{ kind: "transfer", has_annotation: true }] });
    expect(items.some((i) => /annotat/i.test(i.label) && i.color === t("node.annotation.ring"))).toBe(true);
  });
});

describe("font controls — independent graph-label scaling (feature 5)", () => {
  it("scales every label font-size by the multiplier; the default (no arg) is unscaled", () => {
    const base = buildCytoscapeStyle();
    const big = buildCytoscapeStyle(2);
    // node label
    expect(ruleIn(base, "node")!.style["font-size"]).toBe(11);
    expect(ruleIn(big, "node")!.style["font-size"]).toBe(22);
    // an edge value label scales by the same factor
    const evBase = Number(ruleIn(base, "edge[?value_label]")!.style["font-size"]);
    const evBig = Number(ruleIn(big, "edge[?value_label]")!.style["font-size"]);
    expect(evBig).toBe(evBase * 2);
    // a FIFO trace label too
    const fifoBase = Number(ruleIn(base, 'edge[trace="fifo"]')!.style["font-size"]);
    expect(Number(ruleIn(big, 'edge[trace="fifo"]')!.style["font-size"])).toBe(fifoBase * 2);
  });

  it("scaling fonts never disturbs the color channels (still all catalog values)", () => {
    const catalogValues = new Set(CATALOG.map((tk) => tk.value));
    const colors = buildCytoscapeStyle(1.5).flatMap((r) =>
      Object.entries(r.style).filter(([k]) => /color/i.test(k)).map(([, v]) => String(v)));
    for (const v of colors) expect(catalogValues.has(v)).toBe(true);
  });
});

describe("P3.5 — value filter (user_dust), ordering tray, value-driven thickness", () => {
  it("the user_dust (value-filter) node is its OWN dashed bundle, distinct from the auto-dust aggregate", () => {
    const u = rule('node[kind="user_dust"]');
    const a = rule('node[kind="aggregate"]');
    expect(u).toBeDefined();
    expect(u!.style["background-color"]).toBe(t("node.dust.user.fill"));
    expect(u!.style["border-color"]).toBe(t("node.dust.user.rim"));
    expect(u!.style["border-style"]).toBe("dashed");                 // still an expandable bundle
    // the two buckets must NOT share a color — they never merge, and must not read alike.
    expect(t("node.dust.user.fill")).not.toBe(t("node.aggregate.fill"));
    expect(u!.style["background-color"]).not.toBe(a!.style["background-color"]);
    const ue = rule('edge[kind="user_dust"]');
    expect(ue!.style["line-color"]).toBe(t("edge.dust.user.line"));
    expect(t("edge.dust.user.line")).not.toBe(t("edge.aggregate.line"));
  });

  it("the legend surfaces a value-filtered entry when a user_dust node is present", () => {
    const items = legendItems({ nodes: [{ kind: "user_dust" }], edges: [] });
    expect(items.some((i) => /value-filtered|below/i.test(i.label) && i.color === t("node.dust.user.rim"))).toBe(true);
  });

  it("an unordered (missing-key) neighbor is trayed with a STRIKETHROUGH glyph from the catalog", () => {
    const r = rule("node[?ordering_trayed]");
    expect(r).toBeDefined();
    expect(r!.style["background-image"]).toBe(strikeBadgeImage());
    // the strike color is the ordering-tray token (encoded in the SVG data-URI) + the node is dimmed.
    expect(r!.style["background-image"]).toContain(t("node.ordering.tray").replace("#", "%23"));
    expect(r!.style["border-color"]).toBe(t("node.ordering.tray"));
    expect(Number(r!.style["opacity"])).toBeLessThan(1);
  });

  it("edge width is data-driven by the read-model's per-view ew (value-driven thickness, default on)", () => {
    // At the default size scale the width stays the literal data(ew) — the read-model's per-VIEW
    // normalization sets ew (priced -> log-scaled vs visible min/max; unpriced -> neutral baseline).
    expect(rule("edge[?value_label]")!.style["width"]).toBe("data(ew)");
    expect(rule('edge[kind="aggregate"]')!.style["width"]).toBe("data(ew)");
  });

  it("the ordering size multiplier scales icons + paths, orthogonally to the value→thickness mapping", () => {
    const base = buildCytoscapeStyle();             // sizeScale 1
    const big = buildCytoscapeStyle(1, undefined, 1.5); // ordering active -> auto-sized
    // node bodies scale by the multiplier...
    expect(ruleIn(base, 'node[kind="address"]')!.style["width"]).toBe(30);
    expect(ruleIn(big, 'node[kind="address"]')!.style["width"]).toBe(45);  // 30 * 1.5
    // ...and the value-driven edge width becomes a function that multiplies ew by the same scale,
    // KEEPING the value→thickness mapping intact (orthogonal — one scales the layout, the other the value).
    const w = ruleIn(big, "edge[?value_label]")!.style["width"];
    expect(typeof w).toBe("function");
    expect((w as (e: { data: (k: string) => number }) => number)({ data: () => 4 })).toBe(6);  // 4 * 1.5
    // colors are untouched by sizing
    const catalogValues = new Set(CATALOG.map((tk) => tk.value));
    for (const r of big) for (const [k, v] of Object.entries(r.style))
      if (/color/i.test(k)) expect(catalogValues.has(String(v))).toBe(true);
  });
});

describe("graph theme — seed marker (centered on the node, marker off the text label)", () => {
  it("the seed node carries a ★ marker CENTERED on the glyph (drawn over + unclipped, never cut off)", () => {
    const r = rule("node[?seed]");
    expect(r).toBeDefined();
    // Drawn OVER the node (containment 'over' + clip 'none') and CENTERED (50%/50%) so the ★ sits inside
    // the node and is NEVER clipped at the node edge (the reported bug: a top-right corner badge cut off).
    expect(r!.style["background-image"]).toBe(seedBadgeImage());
    expect(r!.style["background-image-containment"]).toBe("over");
    expect(r!.style["background-clip"]).toBe("none");
    expect(r!.style["background-position-x"]).toBe("50%");      // centered horizontally
    expect(r!.style["background-position-y"]).toBe("50%");      // centered vertically
    expect(Number(String(r!.style["background-width"]).replace("px", ""))).toBeGreaterThan(0);
    // The marker rides ONLY as the badge image — no text outline / color is baked onto the seed label.
    expect(r!.style["text-outline-width"]).toBeUndefined();
    expect(r!.style["color"]).toBeUndefined();
    // The badge color comes from the catalog (the encoded seed-marker hex appears in the data-URI).
    const seedHex = t("node.seed.marker").replace("#", "%23");
    expect(r!.style["background-image"]).toContain(seedHex);
  });
});

describe("edge disambiguation beyond color (P35/UX-02)", () => {
  it("the three fact-edge kinds carry DISTINCT target-arrow shapes (grayscale / color-blind safe)", () => {
    const shapes = ["transfer", "tx_input", "tx_output"].map(
      (k) => rule(`edge[kind="${k}"]`)!.style["target-arrow-shape"]);
    expect(shapes.every(Boolean)).toBe(true);       // each fact edge sets an explicit arrowhead
    expect(new Set(shapes).size).toBe(3);           // three DISTINCT shapes — readable without color
  });
  it("keeps the three fact-edge COLORS mutually distinct too (a reinforcing, not sole, channel)", () => {
    const colors = ["transfer", "tx_input", "tx_output"].map((k) => t(`edge.${k}.line`));
    expect(new Set(colors).size).toBe(3);
  });
});

describe("dashed-line semantics reconciled (P36/UX-04)", () => {
  it("provisional / trace-convention / poison use DISTINCT dash patterns (decode without guessing)", () => {
    const pat = (sel: string) => rule(sel)!.style["line-dash-pattern"] as number[] | undefined;
    const prov = pat('edge[finality_status="provisional"]');
    const fifo = pat('edge[trace="fifo"]');
    const inv = pat('edge[trace="investigator"]');
    const poison = pat("edge[?poison_suspect]");
    for (const p of [prov, fifo, inv, poison]) expect(Array.isArray(p) && p!.length > 0).toBe(true);
    const key = (a?: number[]) => (a ?? []).join(",");
    // the three MEANING families read apart: finality ≠ trace-convention ≠ poison
    expect(new Set([key(prov), key(fifo), key(poison)]).size).toBe(3);
    // within the trace family, fifo and investigator still differ
    expect(key(fifo)).not.toBe(key(inv));
  });
});
