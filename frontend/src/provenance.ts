// Provenance drill-through (FN-01): fetch the full `source_query` behind a displayed claim/fact, so an
// investigator can see WHERE it came from — connector, endpoint, params/bounds, retrieval time, and the
// raw-response hash — in one interaction. Read-only; the never-collapse model keeps each source separate
// (Invariant #4), and this just exposes the query each claim already references.

export type SourceQueryProvenance = {
  id: string;
  connector: string;
  capability: string;
  endpoint: string;
  params: Record<string, unknown> | string | null;
  requested_at: string;
  completed_at: string | null;
  status: string;
  raw_response_ref: string | null;
  raw_response_hash: string | null;
  result_summary: string | null;
};

export function fetchSourceQuery(id: string): Promise<SourceQueryProvenance> {
  return fetch(`/api/source_query/${encodeURIComponent(id)}`).then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  });
}
