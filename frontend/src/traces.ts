// Trace-construction API client (LOG-04). A trace is an investigator construction (Family C): EVM edges
// reference real `transfer` facts; Bitcoin edges are `basis`-labeled apportionment CONVENTIONS
// (fifo | investigator), never ground-truth flow. Populate a trace by adding a selected EVM transfer, or
// by FIFO-apportioning a Bitcoin transaction. The writers are insert-once (re-running is a no-op).

const JSON_HEADERS = { "Content-Type": "application/json" };

function postJson(url: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  return fetch(url, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))));
}

function getJson<T>(url: string): Promise<T> {
  return fetch(url).then((r) => (r.ok ? (r.json() as Promise<T>) : Promise.reject(new Error(`HTTP ${r.status}`))));
}

// One legal endpoint for a manual within-tx BTC link: `sources` are the prev-outputs the tx spends,
// `dests` are the tx's own outputs. Picking one of each keeps the link within the transaction (Inv #5).
export type BtcLinkCandidate = {
  id: string; output_index: number; amount: string; address: string; label: string;
};

export type TraceAnnotation = { id: string; content: string; created_at: string };

export function createTrace(name: string, description?: string | null): Promise<{ trace_id: string }> {
  return postJson("/api/trace", { name, description: description ?? null }) as Promise<{ trace_id: string }>;
}

// v1.3.1 — soft-delete (retract) a WHOLE trace: append-only, the trace drops out of the list/graph/report.
export function retractTrace(traceId: string, reason: string): Promise<{ ok: boolean; retraction_id: string }> {
  return postJson(`/api/trace/${traceId}/retract`, { reason }) as Promise<{ ok: boolean; retraction_id: string }>;
}

export function addTraceTransfer(traceId: string, transferId: string): Promise<Record<string, unknown>> {
  return postJson(`/api/trace/${traceId}/transfer`, { transfer_id: transferId });
}

export function fifoTrace(traceId: string, transactionId: string): Promise<Record<string, unknown>> {
  return postJson(`/api/trace/${traceId}/fifo`, { transaction_id: transactionId });
}

export function addTraceLink(traceId: string, link: {
  transaction_id: string; source_output_id: string; dest_output_id: string;
  confidence?: number | null; note?: string | null;
}): Promise<Record<string, unknown>> {
  return postJson(`/api/trace/${traceId}/link`, link as Record<string, unknown>);
}

// The within-tx source/dest option sets for a manual BTC link, so the UI offers pickers (never free text).
export function btcLinkCandidates(txId: string): Promise<{ sources: BtcLinkCandidate[]; dests: BtcLinkCandidate[] }> {
  return getJson(`/api/transaction/${txId}/btc_link_candidates`);
}

// Guided expansion (FN-16): candidate next hops PROPOSED from a trace's frontier — outgoing facts already
// in the case. The tool proposes; the investigator picks. EVM = a transfer to add; BTC = the tx that spends
// a terminal output (the link is chosen + added within that tx).
export type EvmNextHop = {
  kind: "evm"; transfer_id: string; chain: string; from: string | null; to: string | null;
  asset: string | null; amount: string;
};
export type BtcNextHop = {
  kind: "btc"; source_output_id: string; source_label: string; spending_tx_id: string; tx_hash: string;
};

export function traceNextHops(traceId: string): Promise<{ evm: EvmNextHop[]; btc: BtcNextHop[] }> {
  return getJson(`/api/trace/${traceId}/next_hops`);
}

// A cross-chain bridge crossing (FN-17): the investigator asserts an outflow movement on chain A ↔ an
// inflow movement on chain B, as a `basis='investigator'` CLAIM inside a trace — never a fabricated fact.
// A pinned endpoint is one side of the crossing, captured from a selected flow (a `transfer` / `tx_output`).
export type BridgeEndpoint = {
  subject_type: "transfer" | "tx_output"; subject_id: string; chain: string; label: string;
};
export type TraceBridgeLink = {
  id: string; src_subject_type: string; src_subject_id: string; src_chain: string | null;
  dst_subject_type: string; dst_subject_id: string; dst_chain: string | null; basis: string; note: string | null;
};

export function addBridgeLink(traceId: string, link: {
  src_subject_type: string; src_subject_id: string; dst_subject_type: string; dst_subject_id: string;
  note?: string | null;
}): Promise<Record<string, unknown>> {
  return postJson(`/api/trace/${traceId}/bridge`, link as Record<string, unknown>);
}

export function listTraceBridgeLinks(traceId: string): Promise<{ bridge_links: TraceBridgeLink[] }> {
  return getJson(`/api/trace/${traceId}/bridge_links`);
}

// Trace annotations reuse the generic investigator-notes endpoint (`ANNOTATION_TARGET_TYPES` includes
// `trace`) — so a path can carry durable notes shown in the panel and the report's notes appendix.
export function listTraceAnnotations(traceId: string): Promise<{ annotations: TraceAnnotation[] }> {
  return getJson(`/api/target/trace/${traceId}/annotations`);
}

export function addTraceAnnotation(traceId: string, content: string): Promise<{ annotations: TraceAnnotation[] }> {
  return postJson(`/api/target/trace/${traceId}/annotations`, { content }) as Promise<{ annotations: TraceAnnotation[] }>;
}
