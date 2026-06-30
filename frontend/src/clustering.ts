// Clustering API client (P8.8). Each heuristic is a SEPARATE, reversible, confidence-tagged producer;
// co-spend is always on; every opt-in heuristic + the Leiden community overlay default OFF.

const JSON_HEADERS = { "Content-Type": "application/json" };

export type HeuristicInfo = {
  name: string; chain: string; label: string;
  default_off: boolean; always_on: boolean; visual_only?: boolean;
};

export type ClusterRow = {
  entity_id: string; size: number; method: string;
  confidence_min: number | null; confidence_max: number | null; address_ids: string[];
};
export type ClusterSummary = Record<string, { clusters: ClusterRow[]; n_clusters: number; n_addresses: number }>;
export type ClusterRun = {
  source_query_id: string; connector: string; capability: string;
  requested_at: string; memberships: number; active: number;
};

export function listHeuristics(): Promise<HeuristicInfo[]> {
  return fetch("/api/clustering/heuristics").then((r) => r.json()).then((d) => d.heuristics ?? []);
}

export function clusteringSummary(): Promise<{ summary: ClusterSummary; runs: ClusterRun[] }> {
  return fetch("/api/clustering/summary").then((r) => r.json())
    .then((d) => ({ summary: d.summary ?? {}, runs: d.runs ?? [] }))
    .catch(() => ({ summary: {}, runs: [] }));
}

export function previewClustering(name: string, params?: Record<string, unknown>): Promise<{ preview: Record<string, unknown> }> {
  return fetch("/api/clustering/preview", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ name, params }),
  }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))));
}

export function applyClustering(name: string, params?: Record<string, unknown>): Promise<Record<string, unknown>> {
  return fetch("/api/clustering/apply", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ name, params }),
  }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))));
}

export function undoClustering(sourceQueryId: string): Promise<Record<string, unknown>> {
  return fetch("/api/clustering/undo", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ source_query_id: sourceQueryId }),
  }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))));
}
