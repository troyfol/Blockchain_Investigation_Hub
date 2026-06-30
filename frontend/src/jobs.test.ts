import { afterEach, describe, expect, it, vi } from "vitest";
import * as J from "./jobs";

function mockFetch(payload: unknown, ok = true) {
  const calls: { url: string; opts: any }[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, opts?: any) => {
    calls.push({ url, opts });
    return Promise.resolve({ ok, status: ok ? 200 : 500, json: () => Promise.resolve(payload) } as Response);
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); });

const JOB = (over: Partial<J.JobStatus> = {}): J.JobStatus => ({
  id: "j1", kind: "ingest", state: "running", phase: "fetching", requests: 0, valued: 0, total: 0,
  rate_limited: false, message: "", error: null, ...over,
});

describe("jobs client (progress + cancel)", () => {
  it("getActiveJob reads /api/jobs/active and unwraps .job", async () => {
    const calls = mockFetch({ job: JOB({ requests: 3 }) });
    const j = await J.getActiveJob();
    expect(calls[0].url).toBe("/api/jobs/active");
    expect(j?.requests).toBe(3);
  });

  it("getActiveJob returns null when no job + tolerates a failure", async () => {
    mockFetch({ job: null });
    expect(await J.getActiveJob()).toBeNull();
  });

  it("cancelJob POSTs /api/jobs/cancel", async () => {
    const calls = mockFetch({ ok: true, canceled: true });
    expect(await J.cancelJob()).toBe(true);
    expect(calls[0].url).toBe("/api/jobs/cancel");
    expect(calls[0].opts.method).toBe("POST");
  });
});

describe("jobProgressLine", () => {
  it("ingest shows pages fetched", () => {
    expect(J.jobProgressLine(JOB({ kind: "ingest", requests: 5 }))).toBe("Fetching… 5 pages");
    expect(J.jobProgressLine(JOB({ kind: "ingest", requests: 1 }))).toBe("Fetching… 1 page");
  });
  it("a rate-limited running job shows the backoff state (wins over the page count)", () => {
    expect(J.jobProgressLine(JOB({ rate_limited: true }))).toMatch(/rate-limited/i);
  });
  it("valuation shows M of N", () => {
    expect(J.jobProgressLine(JOB({ kind: "valuation", valued: 4, total: 10 }))).toBe("Valuing 4 of 10…");
  });
  it("null -> empty line", () => {
    expect(J.jobProgressLine(null)).toBe("");
  });
});
