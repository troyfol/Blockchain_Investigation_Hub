import { afterEach, describe, expect, it, vi } from "vitest";
import * as C from "./cases";

// Drive the case-management client + the pure verdict/display helpers the entry screen renders from.
// Node env (no DOM): we stub global fetch to assert the client hits the right endpoints, and exercise
// the pure functions directly — same approach as ordering.test.ts / theme.test.ts.

type Init = { ok?: boolean; status?: number };

function mockFetch(payload: unknown, init: Init = {}) {
  const calls: { url: string; opts: any }[] = [];
  const fn = vi.fn((url: string, opts?: any) => {
    calls.push({ url, opts });
    return Promise.resolve({
      ok: init.ok ?? true,
      status: init.status ?? 200,
      json: () => Promise.resolve(payload),
    } as Response);
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("cases API client drives the active-case endpoints", () => {
  it("getActiveCase unwraps {active}", async () => {
    const calls = mockFetch({ active: { title: "X" } });
    expect(await C.getActiveCase()).toEqual({ title: "X" });
    expect(calls[0].url).toBe("/api/cases/active");
  });

  it("listCases unwraps {cases}", async () => {
    mockFetch({ cases: [{ path: "/a" }] });
    expect(await C.listCases()).toEqual([{ path: "/a" }]);
  });

  it("newCase POSTs title + location", async () => {
    const calls = mockFetch({ active: { title: "N" }, path: "/p" });
    await C.newCase("My Case", "/loc");
    expect(calls[0].url).toBe("/api/cases/new");
    expect(calls[0].opts.method).toBe("POST");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ title: "My Case", location: "/loc" });
  });

  it("openCase POSTs the path", async () => {
    const calls = mockFetch({ active: { title: "O" }, migrated: false, path: "/q" });
    await C.openCase("/q");
    expect(calls[0].url).toBe("/api/cases/open");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ path: "/q" });
  });

  it("importCaseByPath carries allow_untrusted", async () => {
    const calls = mockFetch({ opened: true });
    await C.importCaseByPath("/b.casefile", true);
    expect(calls[0].url).toBe("/api/cases/import");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ path: "/b.casefile", allow_untrusted: true });
  });

  it("importCaseUpload puts filename + flag in the query and sends the blob body", async () => {
    const calls = mockFetch({ opened: true });
    const blob = new Blob([new Uint8Array([1, 2, 3])]);
    await C.importCaseUpload(blob, "x.casefile", true);
    expect(calls[0].url).toContain("/api/cases/import-upload?");
    expect(calls[0].url).toContain("filename=x.casefile");
    expect(calls[0].url).toContain("allow_untrusted=true");
    expect(calls[0].opts.body).toBe(blob);
  });

  it("forgetCase POSTs the path", async () => {
    const calls = mockFetch({ cases: [] });
    await C.forgetCase("/z");
    expect(calls[0].url).toBe("/api/cases/forget");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ path: "/z" });
  });

  it("pickNative returns null in browser mode (501)", async () => {
    mockFetch({}, { ok: false, status: 501 });
    expect(await C.pickNative("casefile")).toBeNull();
  });

  it("pickNative returns the picked paths on success", async () => {
    mockFetch({ paths: ["/a"] });
    expect(await C.pickNative("folder")).toEqual(["/a"]);
  });

  it("surfaces the backend detail on an error response", async () => {
    mockFetch({ detail: "not a BIH case" }, { ok: false, status: 400 });
    await expect(C.openCase("/x")).rejects.toThrow("not a BIH case");
  });
});

describe("importVerdict splits TAMPER from AUDIT-warning (never conflated)", () => {
  const clean = {
    manifest: { ok: true, missing: [], mismatched: [], extra: [], file_count: 3 },
    self_contained: {
      ok: true, attached_databases: [], fk_violations: 0, missing_referenced_files: [],
      unsafe_referenced_paths: [], audits_passed: true,
    },
  };

  it("is a green 'verified' for a fully clean bundle", () => {
    const v = C.importVerdict({ ok: true, ...clean });
    expect(v.ok).toBe(true);
    expect(v.tone).toBe("verified");
    expect(v.reasons).toEqual([]);
  });

  it("a HASH MISMATCH is tamper (altered after sealing), naming the file", () => {
    const v = C.importVerdict({
      ok: false,
      manifest: { ok: false, missing: [], mismatched: ["case.db"], extra: [], file_count: 3 },
    });
    expect(v.tone).toBe("tamper");
    expect(v.headline).toMatch(/altered/i);
    expect(v.reasons.join(" ")).toContain("case.db");
  });

  it("a PATH-ESCAPE (structural self-containment failure) is tamper", () => {
    const v = C.importVerdict({
      ok: false,
      manifest: { ok: true, missing: [], mismatched: [], extra: [], file_count: 3 },
      self_contained: {
        ok: false, attached_databases: [], fk_violations: 0, missing_referenced_files: [],
        unsafe_referenced_paths: ["../../escape.txt"], audits_passed: true,
      },
    });
    expect(v.tone).toBe("tamper");
    expect(v.reasons.some((r) => r.includes("../../escape.txt"))).toBe(true);
  });

  it("HASH-INTACT but audits fail is an AUDIT warning, NOT tamper, and names the failing audit", () => {
    const v = C.importVerdict({
      ok: false,
      manifest: { ok: true, missing: [], mismatched: [], extra: [], file_count: 3 },
      self_contained: {
        ok: false, attached_databases: [], fk_violations: 0, missing_referenced_files: [],
        unsafe_referenced_paths: [], audits_passed: false, failed_audits: ["final-immutability"],
      },
    });
    expect(v.tone).toBe("audit");
    expect(v.tone).not.toBe("tamper");
    expect(v.headline).toMatch(/authentic/i);
    expect(v.reasons.join(" ")).toContain("final-immutability");
  });

  it("an audit warning falls back to a generic reason when no audit names are given", () => {
    const v = C.importVerdict({
      ok: false,
      manifest: { ok: true, missing: [], mismatched: [], extra: [], file_count: 1 },
      self_contained: {
        ok: false, attached_databases: [], fk_violations: 0, missing_referenced_files: [],
        unsafe_referenced_paths: [], audits_passed: false,
      },
    });
    expect(v.tone).toBe("audit");
    expect(v.reasons.length).toBeGreaterThan(0);
  });

  it("always reports a failing state with at least one reason", () => {
    const v = C.importVerdict({ ok: false });
    expect(v.ok).toBe(false);
    expect(v.reasons.length).toBeGreaterThan(0);
  });
});

describe("display helpers", () => {
  it("shortenPath keeps the last three segments", () => {
    expect(C.shortenPath("/a/b/c/d/e")).toBe("…/c/d/e");
    expect(C.shortenPath("a/b")).toBe("a/b");
    expect(C.shortenPath("C:\\cases\\acme\\case.db")).toBe("…/cases/acme/case.db");
  });

  it("caseLabel prefers the title, else a short path", () => {
    expect(C.caseLabel({ title: "Acme", path: "/x/y/z/w" })).toBe("Acme");
    expect(C.caseLabel({ path: "/x/y/z/w" })).toBe("…/y/z/w");
  });

  it("chainSummary joins chains or notes the empty case", () => {
    expect(C.chainSummary(["ethereum", "bitcoin"])).toBe("ethereum · bitcoin");
    expect(C.chainSummary([])).toMatch(/no on-chain/);
  });

  it("lastOpenedLabel is lenient about bad input", () => {
    expect(C.lastOpenedLabel("")).toBe("");
    expect(C.lastOpenedLabel("not-a-date")).toBe("");
  });

  it("isWindowed is false with no pywebview bridge present", () => {
    expect(C.isWindowed()).toBe(false);
  });
});
