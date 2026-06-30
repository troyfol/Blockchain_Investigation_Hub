// Settings client + pure helpers (P5): connectors · keys->keyring · cases folder · offline.
//
// CREDENTIAL BOUNDARY: a key VALUE never crosses this module in either direction except on the way OUT
// to the keyring write endpoint. setKey POSTs the key and the response carries only presence; nothing
// here ever reads a key back. The pure helpers (status badge, keyring/plaintext banner) are unit-tested
// in node, like cases.ts / ordering.ts.

export type FreeConnector = {
  name: string; label: string; kind: string; always_on: boolean;
  // Free pillars that need a key to FUNCTION (Etherscan for EVM) carry the same write-only key field as
  // paid connectors, but stay always-on (no enable toggle). `key_present` is presence only — never a value.
  requires_key?: boolean; key_present?: boolean;
};

export type PaidStatus = "available" | "needs-key" | "disabled";
export type PaidConnector = {
  name: string; kind: string; capabilities: string[];
  enabled: boolean; key_present: boolean; available: boolean; status: PaidStatus;
};

export type KeyringStatus = {
  backend: string; available: boolean; plaintext_active: boolean; message: string | null;
};

export type IntelSnapshot = { path: string; date: string | null; override: boolean; exists: boolean };

export type SettingsData = {
  connectors: { free: FreeConnector[]; paid: PaidConnector[] };
  cases_folder: string;
  offline: boolean;
  evm_chains?: string[];
  intel?: { ofac: IntelSnapshot; graphsense: IntelSnapshot };  // P8.7 intel-source snapshot dates
  keyring: KeyringStatus;
};

const JSON_HEADERS = { "Content-Type": "application/json" };

async function asJson(r: Response): Promise<any> {
  if (!r.ok) {
    const detail = await r.json().then((d) => d?.detail).catch(() => null);
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

// --- API client ----------------------------------------------------------------------------

export function getSettings(): Promise<SettingsData> {
  return fetch("/api/settings").then(asJson);
}

type SettingsPatch = {
  offline?: boolean; cases_folder?: string; connector?: { name: string; enabled: boolean };
};

export function patchSettings(patch: SettingsPatch): Promise<SettingsData> {
  return fetch("/api/settings", {
    method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(patch),
  }).then(asJson);
}

export const setConnectorEnabled = (name: string, enabled: boolean) =>
  patchSettings({ connector: { name, enabled } });
export const setOffline = (offline: boolean) => patchSettings({ offline });
export const setCasesFolder = (cases_folder: string) => patchSettings({ cases_folder });

// Write a key STRAIGHT to the keyring. The response carries only {ok, connector, key_present} — never
// the value. The caller (the field) clears its input on success; the value is never held in state.
export function setKey(connector: string, key: string): Promise<{ ok: boolean; connector: string; key_present: boolean }> {
  return fetch(`/api/settings/keys/${encodeURIComponent(connector)}`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ key }),
  }).then(asJson);
}

export function clearKey(connector: string): Promise<{ ok: boolean; connector: string; key_present: boolean }> {
  return fetch(`/api/settings/keys/${encodeURIComponent(connector)}`, { method: "DELETE" }).then(asJson);
}

// --- pure helpers (unit-tested) ------------------------------------------------------------

export type Badge = { label: string; tone: PaidStatus };

// A paid connector's status badge: available = enabled AND key present; needs-key = enabled, no key;
// disabled = not enabled (regardless of key).
export function statusBadge(p: PaidConnector): Badge {
  if (p.status === "available") return { label: "available", tone: "available" };
  if (p.status === "needs-key") return { label: "needs key", tone: "needs-key" };
  return { label: "disabled", tone: "disabled" };
}

// The loud credential banner, or null when everything is fine. Plaintext mode wins (most surprising):
// secrets are read from env vars this session, NOT the keyring. Else, if no keyring backend is present,
// warn that keys can't be stored. Otherwise no banner.
export function keyringBanner(k: KeyringStatus): { tone: "warning" | "error"; text: string } | null {
  if (k.plaintext_active) {
    return { tone: "warning",
      text: "Plaintext key mode is ON (BIH_ALLOW_PLAINTEXT_KEYS=1): API keys are read from environment "
        + "variables and are NOT stored in the OS keyring this session. Use the keyring for real keys." };
  }
  if (!k.available) {
    return { tone: "error",
      text: k.message || "No OS keyring backend is available — API keys cannot be stored securely here." };
  }
  return null;
}
