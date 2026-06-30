import { useCallback, useEffect, useState } from "react";
import { isWindowed, pickNative } from "./cases";
import { refreshIntel } from "./intel";
import {
  type PaidConnector, type SettingsData, clearKey, getSettings, keyringBanner, setCasesFolder,
  setConnectorEnabled, setKey, setOffline, statusBadge,
} from "./settings";
import { t } from "./theme/theme";

// The Settings panel (P5, Neo-Tokyo): connectors (free always-on; paid toggle + write-only key →
// keyring), the cases folder, and offline mode. CREDENTIAL BOUNDARY: the key field is write-only — a
// password input that POSTs to the keyring and clears on submit; the UI only ever shows presence.

type Props = { onClose: () => void; onCasesFolderChanged?: (folder: string) => void };

const card: React.CSSProperties = {
  background: t("ui.panel.bg"), border: `1px solid ${t("ui.border")}`, borderRadius: 10,
  padding: 16, display: "flex", flexDirection: "column", gap: 10,
};
const field: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "6px 9px", fontSize: 13,
};
const btn: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "6px 11px", fontSize: 12, cursor: "pointer", whiteSpace: "nowrap",
};
const sectionTitle: React.CSSProperties = {
  fontSize: 12, fontWeight: 600, letterSpacing: 0.4, textTransform: "uppercase",
  color: t("node.seed.marker"), margin: 0,
};
const hint: React.CSSProperties = { fontSize: 11, color: t("ui.muted"), margin: 0 };

const badgeColor = (tone: PaidConnector["status"]): string =>
  tone === "available" ? t("node.annotation.ring") : tone === "needs-key" ? t("ui.warning") : t("ui.muted");

export default function SettingsPanel({ onClose, onCasesFolderChanged }: Props) {
  const windowed = isWindowed();
  const [data, setData] = useState<SettingsData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({});
  const [folderDraft, setFolderDraft] = useState("");

  const load = useCallback(() => { getSettings().then(setData).catch((e) => setErr(String(e))); }, []);
  useEffect(() => { load(); }, [load]);

  const run = useCallback(async (op: () => Promise<SettingsData>) => {
    setBusy(true); setErr(null);
    try { setData(await op()); } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }, []);

  const toggleConnector = (name: string, enabled: boolean) => run(() => setConnectorEnabled(name, enabled));
  const toggleOffline = () => { if (data) run(() => setOffline(!data.offline)); };

  const submitKey = (name: string) => {
    const key = (keyDraft[name] || "").trim();
    if (!key) return;
    setBusy(true); setErr(null);
    setKey(name, key)
      .then(() => { setKeyDraft((d) => ({ ...d, [name]: "" })); load(); })  // clear the field on success
      .catch((e) => setErr(String(e instanceof Error ? e.message : e)))
      .finally(() => setBusy(false));
  };

  const clearKeyFor = (name: string) => {
    setBusy(true); setErr(null);
    clearKey(name).then(() => load())
      .catch((e) => setErr(String(e instanceof Error ? e.message : e)))
      .finally(() => setBusy(false));
  };

  const applyFolder = (folder: string) => {
    const f = folder.trim();
    if (!f) return;
    run(() => setCasesFolder(f)).then(() => { setFolderDraft(""); onCasesFolderChanged?.(f); });
  };
  const chooseFolderNative = async () => {
    try {
      const paths = await pickNative("folder");
      if (paths && paths[0]) applyFolder(paths[0]);
    } catch (e) { setErr(String(e)); }
  };

  const refreshOfac = () => {
    setBusy(true); setErr(null);
    refreshIntel().then(() => load())
      .catch((e) => setErr(String(e instanceof Error ? e.message : e)))
      .finally(() => setBusy(false));
  };

  const banner = data ? keyringBanner(data.keyring) : null;

  const backdrop: React.CSSProperties = {
    position: "fixed", inset: 0, zIndex: 85, background: t("ui.app.bg"),
    display: "flex", alignItems: "flex-start", justifyContent: "center", overflow: "auto", padding: 28,
  };

  return (
    <div style={backdrop} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{ width: "100%", maxWidth: 720, display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <h1 style={{ margin: 0, fontSize: 20, color: t("ui.text") }}>Settings</h1>
          <button style={{ ...btn, marginLeft: "auto" }} onClick={onClose} aria-label="Close settings">✕</button>
        </div>

        {/* Offline banner — prominent: it changes what refresh/expand can do */}
        {data?.offline && (
          <div style={{ ...card, padding: 12, borderColor: t("ui.warning"), gap: 4 }}>
            <b style={{ color: t("ui.warning"), fontSize: 13 }}>⦿ Offline mode is ON</b>
            <span style={{ ...hint, color: t("ui.text.secondary") }}>
              Connectors will not make network calls — ingest / expand are disabled. Cached data, views,
              reports and export still work. Turn offline off (below) to fetch new data.
            </span>
          </div>
        )}

        {/* Credential / keyring banner (loud when plaintext mode or no backend) */}
        {banner && (
          <div style={{ ...card, padding: 12,
            borderColor: banner.tone === "error" ? t("ui.error") : t("ui.warning") }}>
            <b style={{ fontSize: 12, color: banner.tone === "error" ? t("ui.error") : t("ui.warning") }}>
              {banner.tone === "error" ? "⚠ " : "⦿ "}{banner.text}
            </b>
          </div>
        )}

        {err && (
          <div style={{ ...card, padding: 12, color: t("ui.error"), fontSize: 13, borderColor: t("ui.error") }}>
            {err}
          </div>
        )}

        {!data ? (
          <p style={hint}>Loading settings…</p>
        ) : (
          <>
            {/* Connectors */}
            <div style={card}>
              <p style={sectionTitle}>Connectors</p>
              <p style={hint}>Free pillars are always on (the baseline is never blocked). Paid sources are
                side-by-side options — each needs its enable toggle AND an API key in the OS keyring.</p>

              {/* Free pillars with NO key — simple always-on chips. */}
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {data.connectors.free.filter((c) => !c.requires_key).map((c) => (
                  <span key={c.name} title={c.kind} style={{ ...field, fontSize: 11,
                    display: "inline-flex", alignItems: "center", gap: 6 }}>
                    {c.label}
                    <span style={{ color: t("node.annotation.ring"), fontSize: 10 }}>● always on</span>
                  </span>
                ))}
              </div>

              {/* Free pillars that NEED a key to function (Etherscan for EVM) — always-on, no enable
                  toggle, but the same write-only keyring field as paid connectors. */}
              <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 4 }}>
                {data.connectors.free.filter((c) => c.requires_key).map((c) => (
                  <div key={c.name} style={{ ...field, padding: 10, display: "flex",
                    flexDirection: "column", gap: 8, background: t("ui.panel.elevated") }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <b style={{ color: t("ui.text"), fontSize: 13 }}>{c.label}</b>
                      <span style={{ color: t("node.annotation.ring"), fontSize: 10 }}>● always on</span>
                      <span style={{ marginLeft: "auto", fontSize: 11, fontWeight: 600,
                        color: c.key_present ? t("node.annotation.ring") : t("ui.warning") }}>
                        {c.key_present ? "key set ✓" : "no key"}
                      </span>
                    </div>
                    {!c.key_present && (
                      <span style={{ fontSize: 11, color: t("ui.muted") }}>
                        {c.name === "etherscan"
                          ? "EVM ingest needs a free Etherscan API key (etherscan.io/apis)."
                          : "This source needs a free API key to function."}
                      </span>
                    )}
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                      <input type="password" placeholder="paste API key (write-only)" autoComplete="off"
                             value={keyDraft[c.name] || ""} disabled={busy}
                             onChange={(e) => setKeyDraft((d) => ({ ...d, [c.name]: e.target.value }))}
                             onKeyDown={(e) => { if (e.key === "Enter") submitKey(c.name); }}
                             style={{ ...field, flex: 1, minWidth: 160 }} />
                      <button style={btn} disabled={busy || !(keyDraft[c.name] || "").trim()}
                              onClick={() => submitKey(c.name)}>Save key</button>
                      {c.key_present && (
                        <button style={{ ...btn, borderColor: t("ui.error"), color: t("ui.error") }}
                                disabled={busy} onClick={() => clearKeyFor(c.name)}>Clear key</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 4 }}>
                {data.connectors.paid.map((p) => {
                  const badge = statusBadge(p);
                  return (
                    <div key={p.name} style={{ ...field, padding: 10, display: "flex",
                      flexDirection: "column", gap: 8, background: t("ui.panel.elevated") }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                        <label style={{ display: "flex", alignItems: "center", gap: 6, color: t("ui.text"),
                          fontSize: 13 }}>
                          <input type="checkbox" checked={p.enabled} disabled={busy}
                                 onChange={(e) => toggleConnector(p.name, e.target.checked)} />
                          <b>{p.name}</b>
                        </label>
                        <span style={{ color: t("ui.muted"), fontSize: 11 }}>{p.capabilities.join(" · ")}</span>
                        <span style={{ marginLeft: "auto", fontSize: 11, fontWeight: 600,
                          color: badgeColor(p.status) }}>{badge.label}</span>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                        <span style={{ fontSize: 11, color: p.key_present ? t("node.annotation.ring") : t("ui.muted") }}>
                          {p.key_present ? "key set ✓" : "no key"}
                        </span>
                        <input type="password" placeholder="paste API key (write-only)" autoComplete="off"
                               value={keyDraft[p.name] || ""} disabled={busy}
                               onChange={(e) => setKeyDraft((d) => ({ ...d, [p.name]: e.target.value }))}
                               onKeyDown={(e) => { if (e.key === "Enter") submitKey(p.name); }}
                               style={{ ...field, flex: 1, minWidth: 160 }} />
                        <button style={btn} disabled={busy || !(keyDraft[p.name] || "").trim()}
                                onClick={() => submitKey(p.name)}>Save key</button>
                        {p.key_present && (
                          <button style={{ ...btn, borderColor: t("ui.error"), color: t("ui.error") }}
                                  disabled={busy} onClick={() => clearKeyFor(p.name)}>Clear key</button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Cases folder */}
            <div style={card}>
              <p style={sectionTitle}>Cases folder</p>
              <p style={hint}>Where NEW cases are created. Existing cases stay where they are.</p>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <code style={{ ...field, flex: 1, minWidth: 200, color: t("ui.text.secondary"),
                  fontSize: 12, overflow: "hidden", textOverflow: "ellipsis" }}
                      title={data.cases_folder}>{data.cases_folder}</code>
                {windowed ? (
                  <button style={btn} disabled={busy} onClick={chooseFolderNative}>Change…</button>
                ) : (
                  <>
                    <input style={{ ...field, flex: 1, minWidth: 160 }} placeholder="new folder path"
                           value={folderDraft} disabled={busy}
                           onChange={(e) => setFolderDraft(e.target.value)}
                           onKeyDown={(e) => { if (e.key === "Enter") applyFolder(folderDraft); }} />
                    <button style={btn} disabled={busy || !folderDraft.trim()}
                            onClick={() => applyFolder(folderDraft)}>Change</button>
                  </>
                )}
              </div>
            </div>

            {/* Intel sources (P8.7) — the bundled OFAC + GraphSense snapshot dates + refresh */}
            {data.intel && (
              <div style={card}>
                <p style={sectionTitle}>Intel sources</p>
                <p style={hint}>"Check intel" runs these free pillars against a case (writes sourced
                  claims, never facts). The bundled snapshots work offline; refresh OFAC from source when
                  online.</p>
                {([["OFAC SDN", data.intel.ofac], ["GraphSense TagPack", data.intel.graphsense]] as const).map(
                  ([label, snap]) => (
                    <div key={label} style={{ ...field, display: "flex", alignItems: "center", gap: 8,
                      flexWrap: "wrap", background: t("ui.panel.elevated") }}>
                      <b style={{ fontSize: 12, color: t("ui.text") }}>{label}</b>
                      <span style={{ fontSize: 11, color: t("ui.muted") }}>
                        snapshot {snap.date || "?"}{snap.override ? " (refreshed)" : " (bundled)"}
                      </span>
                      {label === "OFAC SDN" && (
                        <button style={{ ...btn, marginLeft: "auto" }} disabled={busy || data.offline}
                                title={data.offline ? "turn offline mode off to refresh" : "download the current OFAC SDN"}
                                onClick={refreshOfac}>Refresh from source</button>
                      )}
                    </div>
                  ))}
              </div>
            )}

            {/* Offline mode */}
            <div style={card}>
              <p style={sectionTitle}>Offline mode</p>
              <label style={{ display: "flex", alignItems: "center", gap: 8, color: t("ui.text"), fontSize: 13 }}>
                <input type="checkbox" checked={data.offline} disabled={busy} onChange={toggleOffline} />
                Work offline (no outbound network calls)
              </label>
              <p style={hint}>When on, connectors refuse to fetch — ingest and node-expand are disabled and
                operate only on already-ingested data. Everything else (graph, claims, reports, export)
                keeps working.</p>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
