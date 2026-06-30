// Intel client (P8.7 #4): run the free attribution/sanctions pillars against the active case + refresh
// the bundled snapshots from source. Running intel WRITES sourced claims (Inv #3/#4), never facts about
// the chain; the graph re-render then shows the sanctioned halo + GraphSense entity on matching addresses.

export type IntelResult = {
  ok: boolean;
  sources: string[];
  ofac?: { sanctioned: number; attributions: number; snapshot_date: string | null };
  graphsense?: { attributions: number; memberships: number; snapshot_date: string | null };
  chainalysis?: { checked?: number; error?: string };
};

async function asJson(r: Response): Promise<any> {
  if (!r.ok) {
    const detail = await r.json().then((d) => d?.detail).catch(() => null);
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

export function checkIntel(): Promise<IntelResult> {
  return fetch("/api/intel/check", { method: "POST" }).then(asJson);
}

export function refreshIntel(): Promise<{ ok: boolean; ofac: { date: string | null; bytes: number } }> {
  return fetch("/api/intel/refresh", { method: "POST" }).then(asJson);
}

// A short human summary of an intel run for a toast/banner.
export function intelSummary(r: IntelResult): string {
  if (!r.sources.length) return "No intel sources available.";
  const parts: string[] = [];
  if (r.ofac) parts.push(`OFAC: ${r.ofac.sanctioned} sanctioned`);
  if (r.graphsense) parts.push(`GraphSense: ${r.graphsense.attributions} attribution(s)`);
  if (r.chainalysis?.checked != null) parts.push(`Chainalysis: ${r.chainalysis.checked} checked`);
  return parts.join(" · ") || `ran ${r.sources.join(", ")}`;
}
