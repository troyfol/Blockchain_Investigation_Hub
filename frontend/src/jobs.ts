// Long-operation jobs client (P8.7.2): poll the active fetch/valuation job for live progress + cancel it.
// The backend runs at most ONE active job (single-user); the Add-address modal + the Value action poll it
// to show a real progress line ("fetching N pages" / "rate-limited — backing off" / "valuing M of N") and
// offer a Cancel that aborts cleanly (the worker stops at a page boundary, leaving a consistent case).

export type JobStatus = {
  id: string;
  kind: "ingest" | "valuation" | string;
  state: "running" | "done" | "canceled" | "error";
  phase: string;
  requests: number;
  valued: number;
  total: number;
  rate_limited: boolean;
  message: string;
  error: string | null;
};

export function getActiveJob(): Promise<JobStatus | null> {
  return fetch("/api/jobs/active").then((r) => r.json()).then((d) => (d?.job ?? null)).catch(() => null);
}

export function cancelJob(): Promise<boolean> {
  return fetch("/api/jobs/cancel", { method: "POST" })
    .then((r) => r.json()).then((d) => !!d?.canceled).catch(() => false);
}

// A human progress line from a job status (or "" when there's nothing to show).
export function jobProgressLine(j: JobStatus | null): string {
  if (!j) return "";
  if (j.state === "running" && j.rate_limited) return "Rate-limited — backing off…";
  if (j.kind === "valuation") {
    const n = j.total ? `${j.valued} of ${j.total}` : `${j.valued}`;
    return j.state === "running" ? `Valuing ${n}…` : `Valued ${j.valued}`;
  }
  // ingest
  if (j.state === "running") return `Fetching… ${j.requests} page${j.requests === 1 ? "" : "s"}`;
  if (j.state === "canceled") return "Canceled.";
  return `Fetched ${j.requests} page${j.requests === 1 ? "" : "s"}.`;
}

// The determinate completion fraction (0..1) when a job reports a total (valuation "M of N"), else null:
// ingest page-fetching has no up-front total, so the <Progress> bar renders indeterminate. (P29/UX-08.)
export function jobProgressFraction(j: JobStatus | null): number | null {
  if (!j || !j.total || j.total <= 0) return null;
  return Math.max(0, Math.min(1, j.valued / j.total));
}
