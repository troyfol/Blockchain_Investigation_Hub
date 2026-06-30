// Report client + pure helpers (P8.5): generate an immutable report of the active case from the UI and
// open its PDF. The report's content_hash (over the canonical HTML) is the immutability proof; a missing
// OS browser engine is NOT an error — the HTML is always produced and pdf_skip_reason explains the skip.
// Pure helpers are unit-tested in node like ingest.ts / settings.ts.

export type ReportResult = {
  ok: boolean;
  report_id: string;
  content_hash: string;
  html_path: string;
  pdf_path: string | null;
  engine: string | null;
  pdf_skip_reason: string | null;
};

const JSON_HEADERS = { "Content-Type": "application/json" };

async function asJson(r: Response): Promise<any> {
  if (!r.ok) {
    const detail = await r.json().then((d) => d?.detail).catch(() => null);
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

// Generate the report. ``view`` (the active /api/view params) makes the report render the investigator's
// CURRENT bounded view (P8.7.1 #2) — what they're looking at — not the raw full-case hairball.
export function runReport(title?: string, view?: Record<string, unknown>): Promise<ReportResult> {
  const body: Record<string, unknown> = {};
  if (title) body.title = title;
  if (view) body.view = view;
  return fetch("/api/report", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body),
  }).then(asJson);
}

// Ask the backend to open a generated file with the OS default opener (windowed app). The backend only
// opens files under the active case dir (it rejects anything else).
export function openReportFile(path: string): Promise<{ ok: boolean; opened: string }> {
  return fetch("/api/report/open", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ path }),
  }).then(asJson);
}

// --- pure helpers (unit-tested) ------------------------------------------------------------

// The first 12 hex of the content hash — a short, human-quotable immutability fingerprint.
export function shortHash(hash: string): string {
  return (hash || "").slice(0, 12);
}

export type ReportSummary = {
  hashShort: string;
  hasPdf: boolean;
  pdfNote: string;     // "PDF rendered via edge" / a clean skip reason
  engine: string | null;
};

// A display summary: the short hash, whether a PDF exists, and an HONEST note about the PDF (rendered via
// which engine, or the clean skip reason — never surfaced as an error).
export function reportSummary(r: ReportResult): ReportSummary {
  const hasPdf = !!r.pdf_path;
  let pdfNote: string;
  if (hasPdf) pdfNote = `PDF rendered${r.engine ? ` via ${r.engine}` : ""}`;
  else pdfNote = r.pdf_skip_reason
    ? `HTML written, PDF skipped — ${humanizeSkip(r.pdf_skip_reason)}`
    : "HTML written, PDF skipped";
  return { hashShort: shortHash(r.content_hash), hasPdf, pdfNote, engine: r.engine };
}

// Make the engine's skip reason friendlier (the common "no engine" case gets an install hint).
export function humanizeSkip(reason: string): string {
  const r = reason || "";
  if (/no pdf engine|no engine|not found/i.test(r))
    return "install Microsoft Edge or Google Chrome to render the PDF (the HTML report is complete).";
  return r;
}
