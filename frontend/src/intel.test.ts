import { afterEach, describe, expect, it, vi } from "vitest";
import * as I from "./intel";

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

describe("intel client (Check intel button)", () => {
  it("checkIntel POSTs /api/intel/check", async () => {
    const calls = mockFetch({ ok: true, sources: ["ofac-sdn", "graphsense"],
      ofac: { sanctioned: 1, attributions: 1, snapshot_date: "06/30/2026" },
      graphsense: { attributions: 1, memberships: 1, snapshot_date: "2023-05-10" } });
    const r = await I.checkIntel();
    expect(calls[0].url).toBe("/api/intel/check");
    expect(calls[0].opts.method).toBe("POST");
    expect(r.sources).toContain("ofac-sdn");
  });

  it("refreshIntel POSTs /api/intel/refresh", async () => {
    const calls = mockFetch({ ok: true, ofac: { date: "06/30/2026", bytes: 1234 } });
    await I.refreshIntel();
    expect(calls[0].url).toBe("/api/intel/refresh");
    expect(calls[0].opts.method).toBe("POST");
  });

  it("surfaces the backend detail on failure (e.g. offline refresh -> 409)", async () => {
    mockFetch({ detail: "offline mode is on — turn it off to refresh intel from source" }, false);
    await expect(I.refreshIntel()).rejects.toThrow(/offline mode is on/);
  });

  it("intelSummary describes what ran", () => {
    const s = I.intelSummary({ ok: true, sources: ["ofac-sdn", "graphsense"],
      ofac: { sanctioned: 2, attributions: 1, snapshot_date: "x" },
      graphsense: { attributions: 3, memberships: 1, snapshot_date: "y" } });
    expect(s).toContain("OFAC: 2 sanctioned");
    expect(s).toContain("GraphSense: 3");
    expect(I.intelSummary({ ok: true, sources: [] })).toMatch(/no intel/i);
  });
});
