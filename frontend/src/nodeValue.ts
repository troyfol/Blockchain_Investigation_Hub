import type { NodeValue } from "./Graph";

// The USD-valuation state of a node's value summary (P30/UX-09), as a PURE, DOM-free helper so it can be
// unit tested. A node that has on-chain MOVEMENT (a native received/sent amount) but NO USD figure on either
// side is ingested-but-**unvalued** — its value-at-time is pending or unavailable, which is NOT the same as
// $0 (unpriced ≠ zero; the backend only attaches `val` when a movement exists, and leaves `in_usd`/`out_usd`
// null until a valuation pass prices it). "valued" = at least one USD side is present; "none" = nothing to
// value (null `val`, or an all-empty summary) → the value header shows no valuation line.

export type ValuationState = "valued" | "unvalued" | "none";

export function valuationState(val: NodeValue | null | undefined): ValuationState {
  if (!val) return "none";
  if (val.in_usd != null || val.out_usd != null) return "valued";
  if (val.in_native != null || val.out_native != null) return "unvalued";
  return "none";
}
