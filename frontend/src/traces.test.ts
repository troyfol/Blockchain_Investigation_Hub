import { afterEach, describe, expect, it, vi } from "vitest";
import * as T from "./traces";

function mockFetch(payload: unknown, ok = true) {
  const calls: { url: string; opts: any }[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, opts?: any) => {
    calls.push({ url, opts });
    return Promise.resolve({ ok, status: ok ? 200 : 400, json: () => Promise.resolve(payload) } as Response);
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); });

describe("trace-construction client", () => {
  it("createTrace POSTs name + description to /api/trace", async () => {
    const calls = mockFetch({ ok: true, trace_id: "tr1" });
    const r = await T.createTrace("Stolen BTC path");
    expect(calls[0].url).toBe("/api/trace");
    expect(calls[0].opts.method).toBe("POST");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ name: "Stolen BTC path", description: null });
    expect(r.trace_id).toBe("tr1");
  });

  it("addTraceTransfer POSTs transfer_id to /api/trace/:id/transfer", async () => {
    const calls = mockFetch({ ok: true, trace_transfer_id: "tt1" });
    await T.addTraceTransfer("tr1", "xfer9");
    expect(calls[0].url).toBe("/api/trace/tr1/transfer");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ transfer_id: "xfer9" });
  });

  it("fifoTrace POSTs transaction_id to /api/trace/:id/fifo", async () => {
    const calls = mockFetch({ ok: true, links_written: 2 });
    const r = await T.fifoTrace("tr1", "txabc");
    expect(calls[0].url).toBe("/api/trace/tr1/fifo");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ transaction_id: "txabc" });
    expect((r as any).links_written).toBe(2);
  });

  it("addTraceLink POSTs the full link body to /api/trace/:id/link", async () => {
    const calls = mockFetch({ ok: true, trace_btc_link_id: "l1" });
    await T.addTraceLink("tr1", { transaction_id: "t", source_output_id: "o1", dest_output_id: "o2" });
    expect(calls[0].url).toBe("/api/trace/tr1/link");
    expect(JSON.parse(calls[0].opts.body)).toEqual({
      transaction_id: "t", source_output_id: "o1", dest_output_id: "o2" });
  });

  it("rejects on a non-ok response", async () => {
    mockFetch({ detail: "trace not found" }, false);
    await expect(T.fifoTrace("ghost", "tx")).rejects.toThrow("HTTP 400");
  });
});
