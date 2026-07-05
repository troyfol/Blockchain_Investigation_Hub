// Case-management client + pure helpers (P4 entry screen).
//
// One module owns talking to the active-case / registry / import API and the small pure functions the
// entry screen renders from (verdict classification, display formatting, windowed-mode detection). The
// pure helpers are unit-tested (vitest, node env — no DOM), mirroring how ordering.ts / theme.ts are
// covered. Security surface: import ALWAYS verifies a .casefile before opening; the UI must show the
// verdict and only offer "open anyway" behind a loud, explicit untrusted confirmation.

// --- types (mirror the backend shapes) -----------------------------------------------------

export type CaseMeta = {
  path: string;
  title: string;
  description: string | null;
  status: string;
  schema_version: number;
  chains: string[];
  address_count: number;
  tx_count: number;
  created_at: string;
  updated_at: string;
};

export type CaseEntry = {
  path: string;
  title?: string;
  chains?: string[];
  schema_version?: number;
  trusted?: boolean;
  last_opened?: string;
};

export type ManifestCheck = {
  ok: boolean; missing: string[]; mismatched: string[]; extra: string[]; file_count: number;
};
export type SelfContainedCheck = {
  ok: boolean; attached_databases: string[]; fk_violations: number;
  missing_referenced_files: string[]; unsafe_referenced_paths: string[]; audits_passed: boolean;
  failed_audits?: string[];
};
export type VerifyResult = {
  ok: boolean; manifest?: ManifestCheck; self_contained?: SelfContainedCheck; extracted_to?: string | null;
};

export type ImportResult = {
  ok: boolean; opened: boolean; trusted: boolean;
  manifest_ok?: boolean; self_contained_ok?: boolean; audits_passed?: boolean;
  verification: VerifyResult; active: CaseMeta | null;
};

// --- API client ----------------------------------------------------------------------------

const JSON_HEADERS = { "Content-Type": "application/json" };

async function asJson(r: Response): Promise<any> {
  if (!r.ok) {
    const detail = await r.json().then((d) => d?.detail).catch(() => null);
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

export function getActiveCase(): Promise<CaseMeta | null> {
  return fetch("/api/cases/active").then(asJson).then((d) => (d.active ?? null) as CaseMeta | null);
}

export function listCases(): Promise<CaseEntry[]> {
  return fetch("/api/cases").then(asJson).then((d) => (d.cases ?? []) as CaseEntry[]);
}

// P26/FN-22: a declarative case template — a scenario preset that pre-seeds a new case's methodology +
// connectors (settings only, never facts). An absent/null template = today's from-scratch case.
export type CaseTemplate = {
  id: string;
  name: string;
  description: string;
  connectors: string[];
  default_bounds: Record<string, number>;
};

export function fetchCaseTemplates(): Promise<CaseTemplate[]> {
  return fetch("/api/case_templates")
    .then(asJson)
    .then((d) => (d.templates ?? []) as CaseTemplate[])
    .catch(() => [] as CaseTemplate[]);   // templates are optional — a failure just offers no presets
}

export function newCase(title: string, location?: string | null, template?: string | null):
    Promise<{ active: CaseMeta | null; path: string;
              template?: { id: string; name: string; default_bounds: Record<string, number> } }> {
  return fetch("/api/cases/new", {
    method: "POST", headers: JSON_HEADERS,
    body: JSON.stringify({ title, location: location ?? null, template: template ?? null }),
  }).then(asJson);
}

export function openCase(path: string): Promise<{ active: CaseMeta | null; migrated: boolean; path: string }> {
  return fetch("/api/cases/open", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ path }),
  }).then(asJson);
}

export function importCaseByPath(path: string, allowUntrusted = false): Promise<ImportResult> {
  return fetch("/api/cases/import", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ path, allow_untrusted: allowUntrusted }),
  }).then(asJson);
}

export function importCaseUpload(file: Blob, filename: string, allowUntrusted = false): Promise<ImportResult> {
  const q = new URLSearchParams({ filename, allow_untrusted: String(allowUntrusted) });
  return fetch(`/api/cases/import-upload?${q.toString()}`, { method: "POST", body: file }).then(asJson);
}

// P39 — whether this build ships a bundled first-run sample case (drives the CasePicker's "Explore the
// sample case" affordance). Never throws: a failure just means "no sample offered".
export function sampleAvailable(): Promise<boolean> {
  return fetch("/api/cases/sample").then(asJson).then((d) => Boolean(d.available)).catch(() => false);
}

// P39 — import + open the app's bundled sample case (one-click "Explore the sample case"). Same verify
// gate + ImportResult shape as a user import.
export function importSampleCase(): Promise<ImportResult> {
  return fetch("/api/cases/import-sample", { method: "POST" }).then(asJson);
}

export function forgetCase(path: string): Promise<{ cases: CaseEntry[] }> {
  return fetch("/api/cases/forget", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ path }),
  }).then(asJson);
}

export type DialogKind = "casefile" | "casedb" | "folder";

// Open the native OS dialog (windowed app). Returns the picked path, [] on cancel, or null when no
// native dialog is available (dev/browser mode -> the caller uses the HTML upload + path-field fallback).
export function pickNative(kind: DialogKind): Promise<string[] | null> {
  return fetch("/api/dialog/pick", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ kind }),
  }).then(async (r) => {
    if (r.status === 501) return null; // browser/dev mode
    return (await asJson(r)).paths as string[];
  });
}

// --- pure helpers (unit-tested) ------------------------------------------------------------

export type Verdict = { ok: boolean; tone: "verified" | "tamper" | "audit"; headline: string; reasons: string[] };

// Classify a .casefile verification into a UI verdict — THREE distinct claims, never conflated:
//   * "verified" (green): every file's hash matches, the case is self-contained, and all invariant
//     audits pass. Imports clean, no warning.
//   * "tamper"   (red):   the bundle's FILES are wrong — a hash mismatch / injected / missing file, or a
//     structural self-containment violation (path-escape, external-DB dependency, broken provenance).
//     This means the bundle was ALTERED after it was sealed. Reserve "tamper" for exactly this.
//   * "audit"    (warning): every file is AUTHENTIC (hashes match, self-contained) but an invariant
//     audit fails (e.g. a post-finality 'final-immutability' drift). The bundle was NOT tampered with —
//     it just carries an integrity warning. Headlined distinctly and lists the failing audit(s).
// Both non-clean states gate the explicit "open anyway (untrusted)" affordance.
export function importVerdict(v: VerifyResult): Verdict {
  const m = v.manifest;
  const s = v.self_contained;
  const manifestOk = m ? m.ok : true;
  const structuralOk = s
    ? (!s.attached_databases?.length && !s.fk_violations
       && !s.missing_referenced_files?.length && !s.unsafe_referenced_paths?.length)
    : true;
  const auditsOk = s ? s.audits_passed : true;

  if (manifestOk && structuralOk && auditsOk && v.ok !== false) {
    return { ok: true, tone: "verified",
      headline: "Verified — every file's hash matches and the case is self-contained", reasons: [] };
  }

  // TAMPER: the bundle's bytes or structure are wrong -> it was altered after sealing.
  if (!manifestOk || !structuralOk) {
    const reasons: string[] = [];
    if (m?.mismatched?.length) reasons.push(`${m.mismatched.length} file(s) changed since the bundle was sealed: ${m.mismatched.join(", ")}`);
    if (m?.missing?.length) reasons.push(`${m.missing.length} listed file(s) missing from the bundle: ${m.missing.join(", ")}`);
    if (m?.extra?.length) reasons.push(`${m.extra.length} unlisted file(s) were added: ${m.extra.join(", ")}`);
    if (s?.unsafe_referenced_paths?.length) reasons.push(`unsafe (path-escaping) file reference(s): ${s.unsafe_referenced_paths.join(", ")}`);
    if (s?.attached_databases?.length) reasons.push(`depends on an external database (not self-contained): ${s.attached_databases.join(", ")}`);
    if (s && s.fk_violations > 0) reasons.push(`${s.fk_violations} broken provenance link(s)`);
    if (s?.missing_referenced_files?.length) reasons.push(`${s.missing_referenced_files.length} referenced file(s) missing from the bundle`);
    if (!reasons.length) reasons.push("the bundle failed its integrity check");
    return { ok: false, tone: "tamper",
      headline: "Tamper warning — the bundle was altered after it was sealed", reasons };
  }

  // AUTHENTIC but an invariant audit fails -> an integrity warning, NOT tampering.
  const reasons: string[] = [];
  if (s?.failed_audits?.length) reasons.push(`failed invariant audit(s): ${s.failed_audits.join(", ")}`);
  else reasons.push("one or more invariant audits did not pass on the imported case");
  return { ok: false, tone: "audit",
    headline: "Verified authentic, but this case has invariant warnings", reasons };
}

export function shortenPath(p: string): string {
  const parts = (p || "").replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length <= 3 ? p : `…/${parts.slice(-3).join("/")}`;
}

export function caseLabel(e: { title?: string | null; path: string }): string {
  return (e.title && e.title.trim()) || shortenPath(e.path);
}

export function chainSummary(chains?: string[] | null): string {
  return chains && chains.length ? chains.join(" · ") : "no on-chain data yet";
}

// A compact "last opened" hint. Kept lenient (bad/empty input -> "") so it never throws in render.
export function lastOpenedLabel(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString();
}

// pywebview injects window.pywebview when the app runs inside the native window. We use it to decide
// whether to offer native OS dialogs vs the HTML upload + path-field fallback.
export function isWindowed(): boolean {
  return typeof window !== "undefined" && !!(window as unknown as { pywebview?: unknown }).pywebview;
}
