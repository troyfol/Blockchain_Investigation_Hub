import { afterEach, describe, expect, it, vi } from "vitest";
import * as R from "./report";

function mockFetch(payload: unknown, ok = true) {
  const calls: { url: string; opts: any }[] = [];
  const fn = vi.fn((url: string, opts?: any) => {
    calls.push({ url, opts });
    return Promise.resolve({ ok, status: ok ? 200 : 500, json: () => Promise.resolve(payload) } as Response);
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

afterEach(() => { vi.unstubAllGlobals(); });

const RESULT = (over: Partial<R.ReportResult> = {}): R.ReportResult => ({
  ok: true, report_id: "r1", content_hash: "a".repeat(64),
  html_path: "/cases/x/reports/r1.html", pdf_path: "/cases/x/reports/r1.pdf",
  engine: "edge", pdf_skip_reason: null, ...over,
});

describe("pure helpers", () => {
  it("shortHash takes the first 12 hex", () => {
    expect(R.shortHash("abcdef0123456789")).toBe("abcdef012345");
  });

  it("reportSummary reports a rendered PDF + the engine", () => {
    const s = R.reportSummary(RESULT());
    expect(s.hasPdf).toBe(true);
    expect(s.pdfNote).toContain("edge");
    expect(s.hashShort).toBe("a".repeat(12));
  });

  it("reportSummary surfaces a clean skip (never an error) when there is no PDF", () => {
    const s = R.reportSummary(RESULT({ pdf_path: null, engine: null,
      pdf_skip_reason: "no PDF engine found — install Microsoft Edge or Google Chrome" }));
    expect(s.hasPdf).toBe(false);
    expect(s.pdfNote.toLowerCase()).toContain("skipped");
    expect(s.pdfNote.toLowerCase()).toContain("install");
  });

  it("humanizeSkip adds an install hint for the no-engine case, passes others through", () => {
    expect(R.humanizeSkip("no PDF engine found").toLowerCase()).toContain("install");
    expect(R.humanizeSkip("disabled by env")).toBe("disabled by env");
  });
});

describe("client", () => {
  it("runReport POSTs /api/report with the title", async () => {
    const calls = mockFetch(RESULT());
    await R.runReport("My Case");
    expect(calls[0].url).toBe("/api/report");
    expect(calls[0].opts.method).toBe("POST");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ title: "My Case" });
  });

  it("runReport sends the active VIEW so the report renders the current bounded view (P8.7.1 #2)", async () => {
    const calls = mockFetch(RESULT());
    await R.runReport(undefined, { focus: "addr:x", hops: 2, fold_poison: true });
    expect(JSON.parse(calls[0].opts.body)).toEqual({ view: { focus: "addr:x", hops: 2, fold_poison: true } });
  });

  it("openReportFile POSTs /api/report/open with the path", async () => {
    const calls = mockFetch({ ok: true, opened: "/cases/x/reports/r1.pdf" });
    await R.openReportFile("/cases/x/reports/r1.pdf");
    expect(calls[0].url).toBe("/api/report/open");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ path: "/cases/x/reports/r1.pdf" });
  });

  it("runReport throws the backend detail on failure", async () => {
    mockFetch({ detail: "no active case" }, false);
    await expect(R.runReport()).rejects.toThrow("no active case");
  });
});
