// Trace-construction API client (LOG-04). A trace is an investigator construction (Family C): EVM edges
// reference real `transfer` facts; Bitcoin edges are `basis`-labeled apportionment CONVENTIONS
// (fifo | investigator), never ground-truth flow. Populate a trace by adding a selected EVM transfer, or
// by FIFO-apportioning a Bitcoin transaction. The writers are insert-once (re-running is a no-op).

const JSON_HEADERS = { "Content-Type": "application/json" };

function postJson(url: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  return fetch(url, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))));
}

export function createTrace(name: string, description?: string | null): Promise<{ trace_id: string }> {
  return postJson("/api/trace", { name, description: description ?? null }) as Promise<{ trace_id: string }>;
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
