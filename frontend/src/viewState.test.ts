import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_VIEW, loadCasePrefs, saveCasePrefs, type ViewState, viewLoadSignature, viewToReportParams } from "./viewState";

// viewLoadSignature is the key the App's view-load effect depends on. It encodes two invariants:
//   * P4 fix: a case SWITCH re-fetches the graph even when the view params are identical (so the new
//     case's seed / empty-state renders without a page reload), and
//   * P3.5: an ordering-only change re-lays-out WITHOUT a server refetch.
describe("viewLoadSignature — the /api/view fetch key", () => {
  const CASE_A = "/cases/a/case.db";
  const CASE_B = "/cases/b/case.db";

  it("is null when no case is active (nothing to fetch — the entry screen shows)", () => {
    expect(viewLoadSignature(null, DEFAULT_VIEW)).toBeNull();
  });

  it("CHANGES when the active case changes even if the view is byte-identical (re-fetch on switch)", () => {
    const a = viewLoadSignature(CASE_A, DEFAULT_VIEW);
    const b = viewLoadSignature(CASE_B, DEFAULT_VIEW);
    expect(a).not.toBeNull();
    expect(a).not.toEqual(b);
  });

  it("does NOT change when only the ordering changes (ordering re-lays-out without a refetch)", () => {
    const base = viewLoadSignature(CASE_A, DEFAULT_VIEW);
    const ordered: ViewState = { ...DEFAULT_VIEW, ordering: { anchor: "addr:x", mode: "value" } };
    expect(viewLoadSignature(CASE_A, ordered)).toEqual(base);
  });

  it("CHANGES when a server-relevant view field changes (focus / value filter / value floor)", () => {
    const base = viewLoadSignature(CASE_A, DEFAULT_VIEW);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, focus: "addr:x" })).not.toEqual(base);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, userDustOn: true })).not.toEqual(base);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, valueFloor: 5 })).not.toEqual(base);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, expand: ["agg:x"] })).not.toEqual(base);
  });

  // P8.6 — value basis + denomination grouping are SERVER params, so they must re-fetch.
  it("CHANGES when the value basis or denomination grouping changes (P8.6 server params)", () => {
    const base = viewLoadSignature(CASE_A, DEFAULT_VIEW);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, valueBasis: "native" })).not.toEqual(base);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, groupDenominations: true })).not.toEqual(base);
  });

  // P8.7 — the de-noise toggles + per-denomination filters are SERVER params, so they must re-fetch.
  it("CHANGES when the spam/poison toggles or per-denomination filters change (P8.7 server params)", () => {
    const base = viewLoadSignature(CASE_A, DEFAULT_VIEW);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, showUnverified: true })).not.toEqual(base);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, foldPoison: false })).not.toEqual(base);
    expect(viewLoadSignature(CASE_A, { ...DEFAULT_VIEW, denomFilters: { CDAI: { fold: 1000 } } })).not.toEqual(base);
  });
});

// P8.6 #8 — the per-case display choice (value basis + ordering) persists across sessions.
describe("per-case view prefs (localStorage)", () => {
  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => { store.set(k, v); },
      removeItem: (k: string) => { store.delete(k); },
      clear: () => { store.clear(); },
    });
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("round-trips the value basis + ordering per case path", () => {
    const ordering = { anchor: "addr:x", mode: "native" as const };
    saveCasePrefs("/cases/p/case.db", { valueBasis: "native", ordering });
    expect(loadCasePrefs("/cases/p/case.db")).toEqual({ valueBasis: "native", ordering });
    // a different case has its own (absent) prefs
    expect(loadCasePrefs("/cases/other/case.db")).toBeNull();
  });

  it("returns null for no case + tolerates a cleared store", () => {
    expect(loadCasePrefs(null)).toBeNull();
    expect(loadCasePrefs("/cases/never-saved/case.db")).toBeNull();
  });
});

// P8.7.1 #2 — the Report button sends the active view so the report renders the CURRENT bounded view.
describe("viewToReportParams (report renders the current view)", () => {
  it("maps the active ViewState to the /api/report view params", () => {
    const v: ViewState = { ...DEFAULT_VIEW, focus: "addr:x", hops: 2, valueBasis: "native",
      groupDenominations: true, showUnverified: true, foldPoison: false,
      denomFilters: { CDAI: { fold: 1000 } }, userDustOn: true, userDustUsd: 5 };
    const p = viewToReportParams(v);
    expect(p.focus).toBe("addr:x");
    expect(p.hops).toBe(2);
    expect(p.value_basis).toBe("native");
    expect(p.group_denominations).toBe(true);
    expect(p.show_unverified).toBe(true);
    expect(p.fold_poison).toBe(false);
    expect(p.user_dust_usd).toBe(5);
    expect(p.denom_filters).toBe(JSON.stringify({ CDAI: { fold: 1000 } }));
  });
  it("omits the value filter + denom filters when off", () => {
    const p = viewToReportParams(DEFAULT_VIEW);
    expect(p.user_dust_usd).toBeNull();
    expect(p.denom_filters).toBeNull();
  });
});
