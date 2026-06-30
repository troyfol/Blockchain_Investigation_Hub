import { afterEach, describe, expect, it, vi } from "vitest";
import * as S from "./settings";

type Init = { ok?: boolean; status?: number };

function mockFetch(payload: unknown, init: Init = {}) {
  const calls: { url: string; opts: any }[] = [];
  const fn = vi.fn((url: string, opts?: any) => {
    calls.push({ url, opts });
    return Promise.resolve({
      ok: init.ok ?? true, status: init.status ?? 200, json: () => Promise.resolve(payload),
    } as Response);
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

afterEach(() => { vi.unstubAllGlobals(); });

const PAID = (over: Partial<S.PaidConnector> = {}): S.PaidConnector => ({
  name: "bitquery", kind: "fact", capabilities: ["get_transactions"],
  enabled: false, key_present: false, available: false, status: "disabled", ...over,
});

describe("settings API client", () => {
  it("getSettings hits /api/settings", async () => {
    const calls = mockFetch({ offline: false });
    await S.getSettings();
    expect(calls[0].url).toBe("/api/settings");
  });

  it("setConnectorEnabled PATCHes the connector toggle", async () => {
    const calls = mockFetch({});
    await S.setConnectorEnabled("bitquery", true);
    expect(calls[0].url).toBe("/api/settings");
    expect(calls[0].opts.method).toBe("PATCH");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ connector: { name: "bitquery", enabled: true } });
  });

  it("setOffline + setCasesFolder PATCH their fields", async () => {
    let calls = mockFetch({});
    await S.setOffline(true);
    expect(JSON.parse(calls[0].opts.body)).toEqual({ offline: true });
    calls = mockFetch({});
    await S.setCasesFolder("/cases/here");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ cases_folder: "/cases/here" });
  });

  it("setKey POSTs to the keyring endpoint and the response carries NO key value (write-only)", async () => {
    const calls = mockFetch({ ok: true, connector: "bitquery", key_present: true });
    const res = await S.setKey("bitquery", "super-secret-123");
    expect(calls[0].url).toBe("/api/settings/keys/bitquery");
    expect(calls[0].opts.method).toBe("POST");
    expect(JSON.parse(calls[0].opts.body)).toEqual({ key: "super-secret-123" });
    // the contract: only presence comes back, never the value
    expect(res).toEqual({ ok: true, connector: "bitquery", key_present: true });
    expect(JSON.stringify(res)).not.toContain("super-secret-123");
  });

  it("clearKey DELETEs the keyring endpoint", async () => {
    const calls = mockFetch({ ok: true, connector: "oklink", key_present: false });
    await S.clearKey("oklink");
    expect(calls[0].url).toBe("/api/settings/keys/oklink");
    expect(calls[0].opts.method).toBe("DELETE");
  });

  it("surfaces the backend detail on error", async () => {
    mockFetch({ detail: "no OS keyring backend available" }, { ok: false, status: 503 });
    await expect(S.setKey("bitquery", "k")).rejects.toThrow("no OS keyring backend available");
  });
});

describe("statusBadge", () => {
  it("available when enabled + key present", () => {
    expect(S.statusBadge(PAID({ status: "available" })).tone).toBe("available");
  });
  it("needs-key when enabled without a key", () => {
    const b = S.statusBadge(PAID({ enabled: true, status: "needs-key" }));
    expect(b.tone).toBe("needs-key");
    expect(b.label).toMatch(/needs key/i);
  });
  it("disabled when not enabled (even with a key)", () => {
    expect(S.statusBadge(PAID({ key_present: true, status: "disabled" })).tone).toBe("disabled");
  });
});

describe("keyringBanner", () => {
  const KR = (over: Partial<S.KeyringStatus> = {}): S.KeyringStatus => ({
    backend: "x.Win", available: true, plaintext_active: false, message: null, ...over,
  });

  it("no banner when keyring is available and plaintext is off", () => {
    expect(S.keyringBanner(KR())).toBeNull();
  });

  it("loud WARNING when plaintext key mode is active (secrets not in the keyring)", () => {
    const b = S.keyringBanner(KR({ plaintext_active: true }));
    expect(b?.tone).toBe("warning");
    expect(b?.text).toMatch(/plaintext/i);
    expect(b?.text).toMatch(/not.*keyring/i);
  });

  it("ERROR when no keyring backend is available, using the backend message", () => {
    const b = S.keyringBanner(KR({ available: false, message: "No Secret Service keyring backend." }));
    expect(b?.tone).toBe("error");
    expect(b?.text).toContain("Secret Service");
  });

  it("plaintext warning takes precedence over an unavailable backend", () => {
    const b = S.keyringBanner(KR({ available: false, plaintext_active: true, message: "no backend" }));
    expect(b?.tone).toBe("warning");
  });
});
