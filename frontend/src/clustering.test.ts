import { afterEach, describe, expect, it, vi } from "vitest";
import * as C from "./clustering";

function mockFetch(payload: unknown, ok = true) {
  const calls: { url: string; opts: any }[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, opts?: any) => {
    calls.push({ url, opts });
    return Promise.resolve({ ok, status: ok ? 200 : 500, json: () => Promise.resolve(payload) } as Response);
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); });

describe("clustering client", () => {
  it("listHeuristics unwraps .heuristics", async () => {
    mockFetch({ heuristics: [{ name: "cospend", always_on: true }, { name: "btc-change", default_off: true }] });
    const h = await C.listHeuristics();
    expect(h.map((x) => x.name)).toEqual(["cospend", "btc-change"]);
  });

  it("applyClustering POSTs name+params to /api/clustering/apply", async () => {
    const calls = mockFetch({ ok: true, clusters: 2, memberships_created: 5 });
    const r = await C.applyClustering("btc-change", { require_agree: 2 });
    expect(calls[0].url).toBe("/api/clustering/apply");
    expect(calls[0].opts.method).toBe("POST");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ name: "btc-change", params: { require_agree: 2 } });
    expect((r as any).clusters).toBe(2);
  });

  it("undoClustering POSTs the source_query_id", async () => {
    const calls = mockFetch({ ok: true, retracted: 3 });
    await C.undoClustering("sq1");
    expect(calls[0].url).toBe("/api/clustering/undo");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ source_query_id: "sq1" });
  });

  it("clusteringSummary tolerates failure -> empty", async () => {
    mockFetch(null, false);
    const s = await C.clusteringSummary();
    expect(s).toEqual({ summary: {}, runs: [] });
  });
});
