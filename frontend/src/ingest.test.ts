import { afterEach, describe, expect, it, vi } from "vitest";
import * as I from "./ingest";

type Init = { ok?: boolean; status?: number; reject?: boolean };

function mockFetch(payload: unknown, init: Init = {}) {
  const calls: { url: string; opts: any }[] = [];
  const fn = vi.fn((url: string, opts?: any) => {
    calls.push({ url, opts });
    if (init.reject) return Promise.reject(new Error("network down"));
    return Promise.resolve({
      ok: init.ok ?? true, status: init.status ?? 200, json: () => Promise.resolve(payload),
    } as Response);
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

afterEach(() => { vi.unstubAllGlobals(); });

const G = (nodes: number, edges = 0) => ({
  nodes: Array.from({ length: nodes }, (_, i) => ({ id: `n${i}` })),
  edges: Array.from({ length: edges }, (_, i) => ({ id: `e${i}` })),
}) as any;

describe("detectChain", () => {
  it("recognizes an 0x… EVM address (chain ambiguous)", () => {
    const d = I.detectChain("0x" + "a".repeat(40));
    expect(d.family).toBe("evm");
    expect(d.chain).toBeNull();
  });
  it("recognizes bech32 + base58 Bitcoin addresses", () => {
    expect(I.detectChain("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq").family).toBe("bitcoin");
    expect(I.detectChain("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa").family).toBe("bitcoin");
    expect(I.detectChain("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy").family).toBe("bitcoin");
  });
  it("rejects junk + an 0x with the wrong length", () => {
    expect(I.detectChain("hello").family).toBe("unknown");
    expect(I.detectChain("0x123").family).toBe("unknown");
    expect(I.detectChain("").family).toBe("unknown");
  });
});

describe("depthBounds", () => {
  it("always caps pages so a first ingest is bounded", () => {
    expect(I.depthBounds("shallow").max_pages).toBe(1);
    expect(I.depthBounds("standard").max_pages).toBe(3);
    expect(I.depthBounds("deep").max_pages).toBe(10);
  });
  it("is chain-aware: BTC sends ONLY max_pages (no EVM-only top_n_counterparties)", () => {
    // P8.6 bug fix — sending top_n_counterparties to Esplora used to hard-error the BTC ingest.
    expect(I.depthBounds("standard", "bitcoin")).toEqual({ max_pages: 3 });
    expect(I.depthBounds("shallow", "bitcoin").top_n_counterparties).toBeUndefined();
    expect(I.depthBounds("standard", "evm").top_n_counterparties).toBe(50);
  });
});

describe("buildExpandRequest", () => {
  it("uses the chosen EVM chain for an 0x address", () => {
    const req = I.buildExpandRequest("0x" + "b".repeat(40), "polygon", "standard");
    expect(req).toMatchObject({ chain: "polygon", address: "0x" + "b".repeat(40) });
    expect(req.bounds.max_pages).toBe(3);
  });
  it("uses bitcoin + BTC-safe bounds (no EVM-only bound) for a BTC address", () => {
    const req = I.buildExpandRequest("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", "polygon", "standard");
    expect(req.chain).toBe("bitcoin");
    expect(req.bounds).toEqual({ max_pages: 3 });             // no top_n_counterparties for Esplora
  });
  it("defaults the EVM chain to ethereum when none is given", () => {
    expect(I.buildExpandRequest("0x" + "c".repeat(40), "", "standard").chain).toBe("ethereum");
  });
  it("throws on an unrecognized address", () => {
    expect(() => I.buildExpandRequest("not-an-address", "ethereum", "standard")).toThrow();
  });
});

describe("ingestErrorMessage + ingestAddedData", () => {
  it("returns the backend error when present, null on success", () => {
    expect(I.ingestErrorMessage({ graph: G(0), partial: false, error: "offline mode is on" })).toBe("offline mode is on");
    expect(I.ingestErrorMessage({ graph: G(3), partial: false })).toBeNull();
  });
  it("detects whether ingest added graph elements", () => {
    expect(I.ingestAddedData(G(0), G(3))).toBe(true);          // empty -> populated
    expect(I.ingestAddedData(G(2, 1), G(2, 2))).toBe(true);    // a new edge
    expect(I.ingestAddedData(G(2, 1), G(2, 1))).toBe(false);   // nothing new
    expect(I.ingestAddedData(null, G(1))).toBe(true);
  });
});

describe("client", () => {
  it("getChains hits /api/chains", async () => {
    const calls = mockFetch({ evm: ["ethereum", "polygon"], btc: ["bitcoin"] });
    const c = await I.getChains();
    expect(calls[0].url).toBe("/api/chains");
    expect(c.evm).toContain("ethereum");
  });
  it("getChains falls back to the static list when the endpoint fails", async () => {
    mockFetch(null, { reject: true });
    const c = await I.getChains();
    expect(c.evm).toEqual(I.EVM_CHAINS_FALLBACK);
  });
  it("postExpand POSTs the request to /api/graph/expand", async () => {
    const calls = mockFetch({ graph: G(1), partial: false });
    await I.postExpand({ chain: "bitcoin", address: "bc1xyz", bounds: { max_pages: 3 } });
    expect(calls[0].url).toBe("/api/graph/expand");
    expect(calls[0].opts.method).toBe("POST");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ chain: "bitcoin", address: "bc1xyz", bounds: { max_pages: 3 } });
  });
});
