// On-chain ingestion client + pure helpers (P8.5): the add-address control that seeds/extends a case by
// calling POST /api/graph/expand. The pure functions (chain auto-detect, depth->bounds, request build,
// error humanizing) are unit-tested in node like cases.ts / settings.ts; the fetch wrappers are thin.
//
// "Ingest a NEW address" (this module) is distinct from the header search box, which only CENTERS the
// view on a node already in the case. Ingest pulls facts from a connector into the active case.

import type { GraphData } from "./Graph";

export type ChainFamily = "evm" | "bitcoin" | "unknown";

export type DetectedChain = {
  family: ChainFamily;
  // For EVM, `chain` is unset (0x alone is ambiguous — the UI picks ethereum/polygon/…). For bitcoin it
  // is "bitcoin". For unknown it is null.
  chain: string | null;
};

// Auto-detect the chain family from an address's FORMAT. 0x + 40 hex -> EVM (ambiguous across EVM chains,
// so the caller supplies the specific chain). Bitcoin: bech32 (bc1…) or base58 P2PKH/P2SH (1…/3…). Else
// unknown. Deliberately conservative — never guess a chain we can't ingest.
export function detectChain(addressRaw: string): DetectedChain {
  const a = (addressRaw || "").trim();
  if (/^0x[0-9a-fA-F]{40}$/.test(a)) return { family: "evm", chain: null };
  if (/^bc1[0-9ac-hj-np-z]{6,87}$/.test(a)) return { family: "bitcoin", chain: "bitcoin" };
  if (/^[13][1-9A-HJ-NP-Za-km-z]{25,39}$/.test(a)) return { family: "bitcoin", chain: "bitcoin" };
  return { family: "unknown", chain: null };
}

export const EVM_DEFAULT_CHAIN = "ethereum";
// Fallback EVM list if /api/chains is unavailable; the live list is fetched from the backend (no drift).
export const EVM_CHAINS_FALLBACK = ["ethereum", "arbitrum", "optimism", "base", "polygon"];

// A simple "how much to pull" control. A first ingest of a busy address must not pull unbounded, so even
// "deep" caps pages; the endpoint marks the result `partial` when a bound truncates (surfaced to the UI).
export type Depth = "shallow" | "standard" | "deep";

export const DEPTH_LABELS: Record<Depth, string> = {
  shallow: "Shallow (quick peek)",
  standard: "Standard",
  deep: "Deep (more pages)",
};

// Bounds map to the connector Bounds contract, CHAIN-AWARE (P8.6): a Bitcoin ingest (Esplora) supports
// only ``max_pages`` — sending an EVM-only ``top_n_counterparties`` used to hard-error the BTC ingest — so
// BTC gets max_pages alone; EVM (Etherscan) also gets ``top_n_counterparties``. Conservative defaults so a
// first ingest is bounded; "deep" still caps pages (a truthful ``partial`` beats an unbounded pull).
export function depthBounds(depth: Depth, family: ChainFamily = "evm"): Record<string, number> {
  const pages = depth === "shallow" ? 1 : depth === "deep" ? 10 : 3;
  const bounds: Record<string, number> = { max_pages: pages };
  // top_n_counterparties is an EVM (Etherscan) bound; Esplora doesn't apply it, so omit it for bitcoin.
  if (family === "evm" && depth !== "deep") bounds.top_n_counterparties = depth === "shallow" ? 25 : 50;
  return bounds;
}

export type ExpandRequest = { chain: string; address: string; bounds: Record<string, number> };

// Build the POST body. EVM uses the caller-chosen chain (auto-detect can't disambiguate 0x); bitcoin uses
// the detected chain. Bounds are chain-aware so a BTC ingest never carries an EVM-only bound. Throws on an
// address whose chain can't be resolved (the UI guards this first).
export function buildExpandRequest(addressRaw: string, evmChain: string, depth: Depth): ExpandRequest {
  const address = (addressRaw || "").trim();
  const det = detectChain(address);
  let chain: string;
  if (det.family === "evm") chain = (evmChain || EVM_DEFAULT_CHAIN).trim() || EVM_DEFAULT_CHAIN;
  else if (det.family === "bitcoin") chain = "bitcoin";
  else throw new Error("unrecognized address format — expected an 0x… EVM address or a Bitcoin address");
  return { chain, address, bounds: depthBounds(depth, det.family) };
}

export type ExpandResponse = {
  graph: GraphData;
  partial: boolean;
  error?: string;
  offline?: boolean;
  needs_key?: string;
  canceled?: boolean;   // P8.7.2 — the fetch was canceled (case left consistent)
};

// Turn an expand RESPONSE into a human message, or null when the ingest succeeded. The backend already
// returns friendly text for offline + the missing-Etherscan-key case; this keeps the mapping honest and
// passes any upstream error through. Separate from a thrown/network failure (handled by the caller).
export function ingestErrorMessage(resp: ExpandResponse): string | null {
  if (!resp || !resp.error) return null;
  return resp.error;
}

// True when an ingest actually added graph elements (so the UI can say "nothing new" vs "ingested N").
export function ingestAddedData(before: GraphData | null, after: GraphData): boolean {
  const b = before?.nodes?.length ?? 0;
  return (after?.nodes?.length ?? 0) > b || (after?.edges?.length ?? 0) > (before?.edges?.length ?? 0);
}

// --- thin client ---------------------------------------------------------------------------

const JSON_HEADERS = { "Content-Type": "application/json" };

// The EVM chains the backend can actually ingest (sourced from the Etherscan map). Falls back to the
// static list if the endpoint is unavailable so the control still works.
export function getChains(): Promise<{ evm: string[]; btc: string[] }> {
  return fetch("/api/chains")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .catch(() => ({ evm: EVM_CHAINS_FALLBACK, btc: ["bitcoin"] }));
}

// POST the ingest. Returns the parsed body (which may carry {error}); a transport/HTTP failure rejects.
// An optional AbortSignal lets the Cancel button abort the in-flight fetch (P8.7.2).
export function postExpand(req: ExpandRequest, signal?: AbortSignal): Promise<ExpandResponse> {
  return fetch("/api/graph/expand", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(req), signal,
  }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))));
}
