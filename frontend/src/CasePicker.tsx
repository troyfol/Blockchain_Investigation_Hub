import { useCallback, useEffect, useRef, useState } from "react";
import {
  type CaseEntry, type CaseMeta, type CaseTemplate, type ImportResult, type Verdict,
  caseLabel, chainSummary, fetchCaseTemplates, forgetCase, importCaseByPath, importCaseUpload,
  importSampleCase, importVerdict, isWindowed, lastOpenedLabel, listCases, newCase, openCase, pickNative,
  sampleAvailable, shortenPath,
} from "./cases";
import { t } from "./theme/theme";
import Modal from "./Modal";

// The Neo-Tokyo entry screen: New / Open / Import (.casefile, verified) / Recent. Shown full-screen
// when no case is active, and as an overlay "Cases" switcher (onClose set) to change the active case.
// Opening or importing a case READS it — it never executes anything from the bundle. Import verifies
// FIRST; a bundle that fails verification is reported loudly and only opened behind an explicit
// "untrusted" confirmation.

type Props = {
  active: CaseMeta | null;          // the current active case (overlay/switcher header); null = empty state
  onOpened: (active: CaseMeta) => void;
  onClose?: () => void;             // present only when used as the switcher overlay
};

// A pending import source kept so an "open anyway (untrusted)" retry can resend the SAME bundle.
type PendingImport = { path: string } | { file: File };

const card: React.CSSProperties = {
  background: t("ui.panel.bg"), border: `1px solid ${t("ui.border")}`, borderRadius: 10,
  padding: 18, display: "flex", flexDirection: "column", gap: 10,
};
const field: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "7px 9px", fontSize: 13, width: "100%", boxSizing: "border-box",
};
const btn: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "7px 12px", fontSize: 13, cursor: "pointer", whiteSpace: "nowrap",
};
const primaryBtn: React.CSSProperties = {
  ...btn, background: t("node.seed.marker"), color: t("ui.onAccent"), border: 0, fontWeight: 600,
};
const sectionTitle: React.CSSProperties = {
  fontSize: 12, fontWeight: 600, letterSpacing: 0.4, textTransform: "uppercase",
  color: t("node.seed.marker"), margin: 0,
};
const hint: React.CSSProperties = { fontSize: 11, color: t("ui.muted"), margin: 0 };

// Verdict tone -> catalog color + icon. "verified" = green, "audit" (authentic but invariant warnings)
// = amber/caution, "tamper" (bundle altered) = red. Kept distinct so an audit warning is never dressed
// up as tampering and vice-versa.
function verdictTokens(tone: Verdict["tone"]): { color: string; icon: string } {
  if (tone === "verified") return { color: t("node.annotation.ring"), icon: "✓ " };
  if (tone === "audit") return { color: t("ui.warning"), icon: "⚠ " };
  return { color: t("ui.error"), icon: "⚠ " };
}

export default function CasePicker({ active, onOpened, onClose }: Props) {
  const windowed = isWindowed();
  const [recent, setRecent] = useState<CaseEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [newLocation, setNewLocation] = useState<string | null>(null);
  const [templates, setTemplates] = useState<CaseTemplate[]>([]);   // P26: declarative case presets
  const [template, setTemplate] = useState("");                     // "" = from scratch (no preset)
  const [openPath, setOpenPath] = useState("");
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [sampleOk, setSampleOk] = useState(false);   // P39: this build ships a bundled sample case
  const pendingImport = useRef<PendingImport | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const refreshRecent = useCallback(() => { listCases().then(setRecent).catch(() => setRecent([])); }, []);
  useEffect(() => { refreshRecent(); }, [refreshRecent]);
  useEffect(() => { fetchCaseTemplates().then(setTemplates).catch(() => setTemplates([])); }, []);
  useEffect(() => { sampleAvailable().then(setSampleOk).catch(() => setSampleOk(false)); }, []);

  // Run an async case operation with one busy/error envelope. The op either opens a case (-> onOpened)
  // or returns null (e.g. a cancelled native dialog) for a no-op.
  const run = useCallback(async (op: () => Promise<CaseMeta | null>) => {
    setBusy(true); setErr(null);
    try {
      const opened = await op();
      if (opened) onOpened(opened);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  }, [onOpened]);

  const handleNew = () =>
    run(async () => {
      if (!title.trim()) { setErr("give the case a title"); return null; }
      return (await newCase(title.trim(), newLocation, template || null)).active;
    });

  const handleOpenPath = (path: string) => run(async () => (await openCase(path)).active);

  const handleOpenNative = () =>
    run(async () => {
      const paths = await pickNative("casedb");
      return paths && paths[0] ? (await openCase(paths[0])).active : null;
    });

  const handleNewFolderNative = () =>
    run(async () => {
      const paths = await pickNative("folder");
      if (paths && paths[0]) setNewLocation(paths[0]);
      return null;
    });

  // Apply an import result: open on success, else surface the tamper verdict (keeping the pending
  // bundle so the user can explicitly choose "open anyway").
  const applyImport = useCallback((res: ImportResult) => {
    if (res.opened && res.active) { setVerdict(null); pendingImport.current = null; onOpened(res.active); }
    else setVerdict(importVerdict(res.verification));
  }, [onOpened]);

  const doImport = useCallback((src: PendingImport, untrusted: boolean) =>
    run(async () => {
      pendingImport.current = src;
      const res = "path" in src
        ? await importCaseByPath(src.path, untrusted)
        : await importCaseUpload(src.file, src.file.name, untrusted);
      applyImport(res);
      return null; // applyImport calls onOpened itself on success (so we can show a verdict on failure)
    }), [run, applyImport]);

  const handleImportNative = () =>
    run(async () => {
      const paths = await pickNative("casefile");
      if (paths && paths[0]) doImport({ path: paths[0] }, false);
      return null;
    });

  const handleImportFile = (file: File | undefined) => { if (file) doImport({ file }, false); };

  // P39 — one-click first-run: import + open the app's bundled sample case (reuses the import verdict path,
  // so a — very unlikely — verification issue on our own bundle still surfaces rather than silently opening).
  const handleExploreSample = () =>
    run(async () => { applyImport(await importSampleCase()); return null; });

  const handleOpenAnyway = () => {
    const src = pendingImport.current;
    if (!src) return;
    if (!window.confirm(
      "This .casefile FAILED verification — its contents may have been altered and its provenance can no "
      + "longer be trusted. Open it anyway as an UNTRUSTED case?")) return;
    doImport(src, true);
  };

  const handleForget = (path: string) =>
    forgetCase(path).then((d) => setRecent(d.cases ?? [])).catch((e) => setErr(String(e)));

  const backdrop: React.CSSProperties = {
    position: "fixed", inset: 0, zIndex: 80, background: t("ui.app.bg"),
    display: "flex", alignItems: "flex-start", justifyContent: "center", overflow: "auto", padding: 28,
  };
  const shellStyle: React.CSSProperties = {
    width: "100%", maxWidth: 720, display: "flex", flexDirection: "column", gap: 16,
  };

  // Dialog semantics (role="dialog" + focus-trap + Esc) apply ONLY to the overlay "Cases" SWITCHER
  // (onClose set). The full-screen empty state is the app's landing surface — not a modal — so it renders
  // WITHOUT the dialog wrapper (P31).
  const body = (
    <>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <h1 id="cases-title" style={{ margin: 0, fontSize: 22, color: t("ui.text") }}>
            Blockchain <span style={{ color: t("node.seed.marker") }}>Investigation Hub</span>
          </h1>
          <span style={{ ...hint, marginLeft: "auto" }}>
            {windowed ? "native file dialogs" : "browser mode — upload / path entry"}
          </span>
          {onClose && (
            <button style={btn} onClick={onClose} aria-label="Close case picker">✕</button>
          )}
        </div>
        {active && (
          <p style={{ ...hint, margin: 0 }}>
            Active case: <b style={{ color: t("ui.text") }}>{caseLabel(active)}</b> · {chainSummary(active.chains)}
          </p>
        )}

        {err && (
          <div style={{ ...card, borderColor: t("ui.error"), padding: 12, color: t("ui.error"), fontSize: 13 }}>
            {err}
          </div>
        )}

        {/* First-run: explore the bundled sample + a zero-setup sources nudge + the local-data reassurance.
            Shown only on a fresh install (no cases yet) and only when a sample is actually bundled (P39). */}
        {sampleOk && recent.length === 0 && (
          <div style={{ ...card, borderColor: t("node.seed.marker") }}>
            <p style={sectionTitle}>New here?</p>
            <p style={{ ...hint, color: t("ui.text.secondary"), lineHeight: 1.5 }}>
              Explore a ready-made investigation — the public <b style={{ color: t("ui.text") }}>Tornado Cash</b>{" "}
              sample case. No keys, no setup: the free intelligence pillars (OFAC SDN, GraphSense) are already
              on — add a free Etherscan key in Settings later to ingest EVM chains. Your case data stays on this
              machine; nothing is ever uploaded.
            </p>
            <button style={{ ...primaryBtn, alignSelf: "flex-start" }} disabled={busy}
                    onClick={handleExploreSample}>Explore the sample case</button>
          </div>
        )}

        {/* New */}
        <div style={card}>
          <p style={sectionTitle}>New case</p>
          <input style={field} placeholder="Case title (e.g. Acme Exchange theft 2026)" value={title}
                 disabled={busy} onChange={(e) => setTitle(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") handleNew(); }} />
          {templates.length > 0 && (
            <>
              <select style={field} value={template} disabled={busy}
                      onChange={(e) => setTemplate(e.target.value)} aria-label="Case template">
                <option value="">Start from scratch (no template)</option>
                {templates.map((tpl) => <option key={tpl.id} value={tpl.id}>{tpl.name}</option>)}
              </select>
              {template && (
                <p style={hint}>{templates.find((tpl) => tpl.id === template)?.description}</p>
              )}
            </>
          )}
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            {windowed ? (
              <button style={btn} disabled={busy} onClick={handleNewFolderNative}>Choose folder…</button>
            ) : (
              <input style={{ ...field, flex: 1, minWidth: 200 }} placeholder="Location (optional; default cases/)"
                     value={newLocation ?? ""} disabled={busy}
                     onChange={(e) => setNewLocation(e.target.value || null)} />
            )}
            {newLocation && <span style={hint} title={newLocation}>{shortenPath(newLocation)}</span>}
            <button style={primaryBtn} disabled={busy || !title.trim()} onClick={handleNew}>Create case</button>
          </div>
        </div>

        {/* Open */}
        <div style={card}>
          <p style={sectionTitle}>Open an existing case</p>
          {windowed ? (
            <button style={btn} disabled={busy} onClick={handleOpenNative}>Choose a case.db…</button>
          ) : (
            <div style={{ display: "flex", gap: 8 }}>
              <input style={{ ...field, flex: 1 }} placeholder="path to case.db (or its folder)"
                     value={openPath} disabled={busy}
                     onChange={(e) => setOpenPath(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter" && openPath.trim()) handleOpenPath(openPath.trim()); }} />
              <button style={btn} disabled={busy || !openPath.trim()} onClick={() => handleOpenPath(openPath.trim())}>Open</button>
            </div>
          )}
        </div>

        {/* Import .casefile */}
        <div style={card}>
          <p style={sectionTitle}>Import a .casefile</p>
          <p style={hint}>The bundle is verified (hashes + self-containment) before it opens. Importing reads
            data only — nothing in the bundle is ever executed.</p>
          {windowed ? (
            <button style={btn} disabled={busy} onClick={handleImportNative}>Choose a .casefile…</button>
          ) : (
            <>
              <input ref={fileInput} type="file" accept=".casefile,application/zip" style={{ display: "none" }}
                     onChange={(e) => { handleImportFile(e.target.files?.[0]); e.target.value = ""; }} />
              <button style={btn} disabled={busy} onClick={() => fileInput.current?.click()}>Choose a .casefile…</button>
            </>
          )}
          {verdict && (() => {
            const vt = verdictTokens(verdict.tone);
            return (
              <div style={{ ...card, padding: 12, gap: 6, borderColor: vt.color }}>
                <b style={{ color: vt.color, fontSize: 13 }}>{vt.icon}{verdict.headline}</b>
                {verdict.reasons.length > 0 && (
                  <ul style={{ margin: 0, paddingLeft: 18, color: t("ui.text.secondary"), fontSize: 12 }}>
                    {verdict.reasons.map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                )}
                {/* "Open anyway" gates BOTH non-clean states (tamper AND audit warning). */}
                {verdict.tone !== "verified" && pendingImport.current && (
                  <button style={{ ...btn, borderColor: vt.color, color: vt.color, alignSelf: "flex-start" }}
                          disabled={busy} onClick={handleOpenAnyway}>Open anyway (untrusted)</button>
                )}
              </div>
            );
          })()}
        </div>

        {/* Recent */}
        <div style={card}>
          <p style={sectionTitle}>Recent</p>
          {recent.length === 0 ? (
            <p style={hint}>No cases yet — create or open one above.</p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {recent.map((c) => (
                <div key={c.path} style={{ display: "flex", alignItems: "center", gap: 8,
                  padding: "6px 8px", borderRadius: 6, background: t("ui.panel.elevated") }}>
                  <button style={{ ...btn, background: "transparent", border: 0, padding: 0, flex: 1,
                    textAlign: "left", display: "flex", flexDirection: "column", gap: 2 }}
                          disabled={busy} onClick={() => handleOpenPath(c.path)} title={c.path}>
                    <span style={{ color: t("ui.text"), fontSize: 13 }}>
                      {caseLabel(c)}
                      {c.trusted === false && (
                        <span style={{ color: t("ui.error"), fontSize: 11, marginLeft: 6 }}>· untrusted</span>
                      )}
                    </span>
                    <span style={{ color: t("ui.muted"), fontSize: 11 }}>
                      {chainSummary(c.chains)}{c.last_opened ? ` · opened ${lastOpenedLabel(c.last_opened)}` : ""}
                    </span>
                  </button>
                  <button style={{ ...btn, padding: "3px 8px", fontSize: 11 }} disabled={busy}
                          title="Remove from this list (does not delete the case on disk)"
                          onClick={() => handleForget(c.path)}>remove</button>
                </div>
              ))}
            </div>
          )}
        </div>
    </>
  );

  return onClose ? (
    <Modal onClose={onClose} backdropStyle={backdrop} containerStyle={shellStyle} labelledBy="cases-title">
      {body}
    </Modal>
  ) : (
    <div style={backdrop}>
      <div style={shellStyle}>{body}</div>
    </div>
  );
}
