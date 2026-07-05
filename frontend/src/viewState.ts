// The bounded-view state + its server-fetch signature (extracted from App.tsx so it is DOM-free and
// unit-testable in the node test env). One place defines what a /api/view fetch depends on.

import type { OrderState } from "./ordering";

// The value basis (P8.6): USD value-at-time, or raw NATIVE units (ETH/BTC, per-asset). Drives the edge
// labels, thickness, the dust + value-filter thresholds, and ordering.
export type ValueBasis = "usd" | "native";

export type ViewState = {
  focus: string | null; hops: number; nodeCap: number; groupDust: boolean;
  dustFloor: number; valueFloor: number; onlyFlagged: boolean; expand: string[];
  // P3.5 view-state — all part of the view-history (undo/redo) + Home reset, none mutate case.db.
  userDustOn: boolean; userDustUsd: number;   // the value filter (fold movements below the threshold)
  ordering: OrderState | null;                // the ordered layout (frontend-only; NO server refetch)
  // P8.6 — the value basis + denomination grouping (both SERVER params -> in the load signature).
  valueBasis: ValueBasis;                     // 'usd' | 'native' (units the view ranks/scales/thresholds in)
  groupDenominations: boolean;                // cluster equal-native-denomination pools (mixer structure)
  // P8.7 de-noise (SERVER params): reveal unverified tokens, fold poison, per-denomination min/fold.
  showUnverified: boolean;                    // reveal the collapsed unverified/unpriced-token bundle
  foldPoison: boolean;                        // fold likely address-poisoning (0-value look-alikes) away
  denomFilters: Record<string, { min?: number; fold?: number }>;  // per-asset native min/fold (#1)
  community: boolean;                         // P8.8 Leiden community overlay (VISUAL structure, not ownership)
};

// The seed-focused starting state (Home). EPHEMERAL view params only — never a case mutation. Also the
// state a case SWITCH resets to (a fresh case starts at its own seed/empty-state, not the old view).
export const DEFAULT_VIEW: ViewState = {
  focus: null, hops: 1, nodeCap: 150, groupDust: true,
  dustFloor: 1, valueFloor: 0, onlyFlagged: false, expand: [],
  userDustOn: false, userDustUsd: 10, ordering: null,
  valueBasis: "usd", groupDenominations: false,
  showUnverified: false, foldPoison: true, denomFilters: {}, community: false,
};

// The signature of the /api/view fetch for the CURRENT case + view. Two renders with the same signature
// need no refetch; a change re-fetches. It includes:
//   * the active CASE PATH — so switching cases re-fetches even when the view params are byte-identical
//     (the P4 bug: New/Open/Import/Recent changed the active case but the canvas kept the old graph
//     because nothing the loader keyed on had changed), and
//   * every SERVER-relevant view field,
// but NOT `ordering` (a pure frontend layout — P3.5: ordering re-lays-out without a refetch).
// `null` when no case is active (nothing to fetch — the entry screen shows).
export function viewLoadSignature(casePath: string | null, v: ViewState): string | null {
  if (!casePath) return null;
  return JSON.stringify([
    casePath, v.focus, v.hops, v.nodeCap, v.groupDust, v.dustFloor, v.valueFloor,
    v.onlyFlagged, v.userDustOn, v.userDustUsd, v.expand,
    v.valueBasis, v.groupDenominations,   // P8.6 server params — a change re-fetches
    v.showUnverified, v.foldPoison, v.denomFilters,   // P8.7 server params
    v.community,   // P8.8 community overlay
  ]);
}

// The active view as the report's view params (P8.7.1 #2) — so the Report renders the SAME bounded view
// the investigator is looking at (mirrors the /api/view query the canvas uses). Sent as JSON to /api/report.
export function viewToReportParams(v: ViewState): Record<string, unknown> {
  return {
    focus: v.focus,
    hops: v.hops,
    node_cap: v.nodeCap,
    group_dust: v.groupDust,
    dust_floor_usd: v.dustFloor,
    value_floor_usd: v.valueFloor,
    only_flagged: v.onlyFlagged,
    user_dust_usd: v.userDustOn && v.userDustUsd > 0 ? v.userDustUsd : null,
    expand: v.expand.join(","),
    value_basis: v.valueBasis,
    group_denominations: v.groupDenominations,
    show_unverified: v.showUnverified,
    fold_poison: v.foldPoison,
    denom_filters: Object.keys(v.denomFilters).length ? JSON.stringify(v.denomFilters) : null,
    community: v.community,
  };
}

// P34/UX-01 — how many display / de-noise FILTERS deviate from the seed default. Drives the "Filters (N)"
// badge on the collapsible Filters cluster (P34 header declutter) so the investigator sees at a glance that
// the view is filtered even while the filter controls are collapsed. Counts clear on/off engagements only
// (not the spectrum inputs — hops / font steppers): the two default-ON toggles (groupDust, foldPoison)
// count only when turned OFF (a non-default choice), so a fresh DEFAULT_VIEW reads 0.
export function activeFilterCount(v: ViewState): number {
  let n = 0;
  if (v.groupDust !== DEFAULT_VIEW.groupDust) n++;
  if (v.groupDenominations !== DEFAULT_VIEW.groupDenominations) n++;
  if (v.showUnverified !== DEFAULT_VIEW.showUnverified) n++;
  if (v.foldPoison !== DEFAULT_VIEW.foldPoison) n++;
  if (Object.keys(v.denomFilters).length > 0) n++;
  if (v.community !== DEFAULT_VIEW.community) n++;
  if (v.onlyFlagged !== DEFAULT_VIEW.onlyFlagged) n++;
  if (v.valueFloor !== DEFAULT_VIEW.valueFloor) n++;
  if (v.userDustOn !== DEFAULT_VIEW.userDustOn) n++;
  if (v.ordering !== null) n++;
  return n;
}

// Persist the per-case display CHOICES (value basis + active ordering) so they survive a session (#8).
// localStorage keyed by case path; corruption-tolerant. Ordering is a frontend layout but is remembered
// here too so a reopened case restores the investigator's last arrangement.
const LS_PREFS = "bih.viewPrefs";

export type CaseViewPrefs = { valueBasis: ValueBasis; ordering: OrderState | null };

function _readPrefs(): Record<string, CaseViewPrefs> {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(LS_PREFS) : null;
    const o = raw ? JSON.parse(raw) : {};
    return o && typeof o === "object" ? o : {};
  } catch { return {}; }
}

export function loadCasePrefs(casePath: string | null): CaseViewPrefs | null {
  if (!casePath) return null;
  const p = _readPrefs()[casePath];
  if (!p) return null;
  return { valueBasis: p.valueBasis === "native" ? "native" : "usd", ordering: p.ordering ?? null };
}

export function saveCasePrefs(casePath: string | null, prefs: CaseViewPrefs): void {
  if (!casePath) return;
  try {
    const all = _readPrefs();
    all[casePath] = { valueBasis: prefs.valueBasis, ordering: prefs.ordering };
    if (typeof localStorage !== "undefined") localStorage.setItem(LS_PREFS, JSON.stringify(all));
  } catch { /* ignore quota / disabled storage */ }
}
