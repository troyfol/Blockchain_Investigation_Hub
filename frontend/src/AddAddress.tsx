import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  buildExpandRequest, type Depth, DEPTH_LABELS, detectChain, EVM_CHAINS_FALLBACK, EVM_DEFAULT_CHAIN,
  getChains, ingestAddedData, ingestErrorMessage, postExpand, refetchDiffSummary,
} from "./ingest";
import type { GraphData } from "./Graph";
import { cancelJob, getActiveJob, jobProgressLine } from "./jobs";
import { t } from "./theme/theme";
import Progress from "./Progress";
import Modal from "./Modal";

// The add-address (ingest) modal (P8.5): pull on-chain facts for a NEW address into the active case via
// POST /api/graph/expand — the way a brand-new empty case is seeded. Distinct from the header search box
// (which only centers the view on an in-case node). Chain is auto-detected by address format; an 0x…
// address gets an EVM chain selector (ambiguous). Errors are surfaced honestly (offline / missing key /
// upstream). All colors resolve through the token catalog (no hardcoded hex).

type Props = {
  onClose: () => void;
  // Called with the ingested address after a successful pull so the app can focus + reload the view and
  // refresh the case header counts. `partial` is true when a bound truncated the pull.
  onIngested: (address: string, partial: boolean) => void;
  // The current graph (to tell "ingested N new" from "nothing new for this address").
  currentGraph: GraphData | null;
  // Open Settings (so the "needs Etherscan key" guidance has a button).
  onOpenSettings?: () => void;
  // Reload the view once background valuation finishes (so USD fills in). P8.7.2.
  onValued?: () => void;
};

const card: React.CSSProperties = {
  background: t("ui.panel.bg"), border: `1px solid ${t("ui.border")}`, borderRadius: 10, padding: 18,
  display: "flex", flexDirection: "column", gap: 12, width: "100%", maxWidth: 540,
};
const field: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "7px 9px", fontSize: 13,
};
const btn: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "7px 13px", fontSize: 13, cursor: "pointer", whiteSpace: "nowrap",
};
const hint: React.CSSProperties = { fontSize: 11, color: t("ui.muted"), margin: 0 };
// P37/UX-05 — a lightweight "Advanced" disclosure toggle (borderless, muted) that collapses the depth knob.
const disclosureBtn: React.CSSProperties = {
  background: "transparent", border: 0, color: t("ui.muted"), fontSize: 12, cursor: "pointer",
  padding: 0, display: "inline-flex", alignItems: "center", gap: 5,
};

export default function AddAddress({ onClose, onIngested, currentGraph, onOpenSettings, onValued }: Props) {
  const [address, setAddress] = useState("");
  const [evmChain, setEvmChain] = useState(EVM_DEFAULT_CHAIN);
  const [depth, setDepth] = useState<Depth>("standard");
  const [evmChains, setEvmChains] = useState<string[]>(EVM_CHAINS_FALLBACK);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [needsKey, setNeedsKey] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [progress, setProgress] = useState<string>("");      // P8.7.2 live progress line (job-driven)
  const [jobProg, setJobProg] = useState<{ valued: number; total: number } | null>(null);  // P29 bar M-of-N
  const [showAdvanced, setShowAdvanced] = useState(false);   // P37/UX-05 — depth collapsed behind "Advanced"
  const [succeeded, setSucceeded] = useState(false);         // P37/UX-05 — a completed ingest -> "Done / Add another"
  const pollRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const addrRef = useRef<HTMLInputElement | null>(null);   // P31 — Modal initial-focus target

  useEffect(() => { getChains().then((c) => { if (c.evm?.length) setEvmChains(c.evm); }).catch(() => {}); }, []);
  // Stop polling on unmount (closing the modal mid-valuation leaves the bg job running server-side).
  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
    setJobProg(null);   // P29 — hide the determinate bar once polling ends
  }, []);
  const startPolling = useCallback((onDone?: () => void) => {
    stopPolling();
    pollRef.current = window.setInterval(async () => {
      const j = await getActiveJob();
      setProgress(jobProgressLine(j));
      setJobProg(j ? { valued: j.valued, total: j.total } : null);   // P29 — feed the bar (total 0 => indeterminate)
      if (!j || j.state !== "running") { stopPolling(); onDone?.(); }
    }, 500);
  }, [stopPolling]);

  const det = useMemo(() => detectChain(address), [address]);
  const canIngest = !busy && det.family !== "unknown" && address.trim().length > 0;

  // Cancel the in-flight fetch/valuation: tell the server to stop (consistent case) + abort the request.
  const cancel = useCallback(() => {
    cancelJob().catch(() => {});
    abortRef.current?.abort();
    setProgress("Canceling…");
  }, []);

  // After a successful ingest, auto-kick a BACKGROUND valuation pass (P8.7.2) and show its progress; on
  // completion reload the view so USD fills in. Never blocks; the user can close the modal anytime.
  const kickValuation = useCallback(() => {
    fetch("/api/valuation/run", { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d?.started) return;        // offline / no case -> skip quietly (facts already ingested)
        startPolling(() => { setProgress(""); onValued?.(); });
      })
      .catch(() => {});
  }, [startPolling, onValued]);

  // P37/UX-05 — after a successful ingest the footer offers "Done / Add another"; "Add another" clears the
  // form (keeping the chosen depth + EVM chain) and re-focuses the address input to seed the next one fast.
  const addAnother = useCallback(() => {
    setSucceeded(false); setAddress(""); setNote(null); setError(null); setNeedsKey(false); setProgress("");
    addrRef.current?.focus();
  }, []);

  const ingest = useCallback(() => {
    setError(null); setNeedsKey(false); setNote(null); setProgress("Starting…");
    let req;
    try { req = buildExpandRequest(address, evmChain, depth); }
    catch (e) { setError(String(e instanceof Error ? e.message : e)); setProgress(""); return; }
    setBusy(true);
    abortRef.current = new AbortController();
    startPolling();   // live "fetching N pages / rate-limited" while the request is in flight
    postExpand(req, abortRef.current.signal)
      .then((resp) => {
        stopPolling();
        if (resp.canceled) { setNote("Ingest canceled — the case is unchanged for this address."); return; }
        const msg = ingestErrorMessage(resp);
        if (msg) { setError(msg); setNeedsKey(resp.needs_key === "etherscan"); return; }
        const added = ingestAddedData(currentGraph, resp.graph);
        onIngested(req.address, !!resp.partial);
        setSucceeded(true);   // P37/UX-05 — a completed pull -> switch the footer to "Done / Add another"
        const base = added
          ? (resp.partial ? "Ingested (bounded — more data may exist; raise the depth or re-run)." : "Ingested.")
          : "No new on-chain data found for that address (it may be empty or already ingested).";
        const changes = refetchDiffSummary(resp);   // P23/FN-13: surface what a re-fetch matured/added
        setNote(changes ? `${base} Changes since last fetch: ${changes}.` : base);
        if (added) kickValuation();   // value the new movements in the background (USD fills in)
      })
      .catch((e) => {
        stopPolling();
        if ((e as Error)?.name === "AbortError") setNote("Ingest canceled.");
        else setError(`ingest failed: ${String(e instanceof Error ? e.message : e)}`);
      })
      .finally(() => { setBusy(false); abortRef.current = null; });
  }, [address, evmChain, depth, currentGraph, onIngested, startPolling, stopPolling, kickValuation]);

  const badge = det.family === "evm" ? `EVM · ${evmChain}`
    : det.family === "bitcoin" ? "Bitcoin (keyless)"
    : address.trim() ? "unrecognized address format" : "";
  const badgeColor = det.family === "unknown"
    ? (address.trim() ? t("ui.warning") : t("ui.muted")) : t("node.annotation.ring");

  const backdrop: React.CSSProperties = {
    position: "fixed", inset: 0, zIndex: 84, background: t("ui.app.bg"),
    display: "flex", alignItems: "flex-start", justifyContent: "center", overflow: "auto", padding: 32,
  };

  return (
    <Modal onClose={onClose} backdropStyle={backdrop} containerStyle={card}
           labelledBy="add-address-title" initialFocus={addrRef}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <h2 id="add-address-title" style={{ margin: 0, fontSize: 17, color: t("ui.text") }}>Add / ingest address</h2>
          <button style={{ ...btn, marginLeft: "auto" }} onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p style={hint}>Pull on-chain facts for an address into this case. Bitcoin needs no key; EVM
          (0x…) needs a free Etherscan key (Settings). This is how a brand-new case is first populated.</p>

        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ fontSize: 12, color: t("ui.text.secondary") }}>Address</span>
          <input ref={addrRef} value={address} placeholder="0x… or a Bitcoin address" spellCheck={false}
                 onChange={(e) => { setAddress(e.target.value); if (succeeded) setSucceeded(false); }}
                 onKeyDown={(e) => { if (e.key === "Enter" && canIngest) ingest(); }}
                 style={{ ...field, fontFamily: "ui-monospace, monospace" }} />
          <span style={{ fontSize: 11, color: badgeColor }}>{badge}</span>
        </label>

        {/* The EVM chain (when the 0x… address is chain-ambiguous) stays visible — it can't be inferred from
            the address, and the wrong chain silently ingests nothing. The chain FAMILY is auto-detected (badge). */}
        {det.family === "evm" && (
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 12, color: t("ui.text.secondary") }}>EVM chain</span>
              <select value={evmChain} onChange={(e) => setEvmChain(e.target.value)} style={field}>
                {evmChains.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
          </div>
        )}

        {/* P37/UX-05 — depth (an advanced hop-breadth knob) is collapsed behind a disclosure so the default
            path is just type-address → Enter. Collapsing never resets the chosen depth; a non-default depth is
            surfaced on the toggle so it is never silently hidden. */}
        <div>
          <button type="button" onClick={() => setShowAdvanced((v) => !v)} style={disclosureBtn}
                  aria-expanded={showAdvanced}>
            <span style={{ fontSize: 10 }}>{showAdvanced ? "▾" : "▸"}</span>
            Advanced{depth !== "standard" ? ` · depth: ${DEPTH_LABELS[depth]}` : ""}
          </button>
          {showAdvanced && (
            <label style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8, maxWidth: 260 }}>
              <span style={{ fontSize: 12, color: t("ui.text.secondary") }}>Depth (hops / breadth)</span>
              <select value={depth} onChange={(e) => setDepth(e.target.value as Depth)} style={field}>
                {(Object.keys(DEPTH_LABELS) as Depth[]).map((d) => (
                  <option key={d} value={d}>{DEPTH_LABELS[d]}</option>
                ))}
              </select>
            </label>
          )}
        </div>

        {error && (
          <div style={{ ...card, padding: 10, gap: 6, maxWidth: "none",
                        borderColor: needsKey ? t("ui.warning") : t("ui.error") }}>
            <span style={{ color: needsKey ? t("ui.warning") : t("ui.error"), fontSize: 12 }}>{error}</span>
            {needsKey && onOpenSettings && (
              <button style={{ ...btn, alignSelf: "flex-start" }}
                      onClick={() => { onClose(); onOpenSettings(); }}>Open Settings → add Etherscan key</button>
            )}
          </div>
        )}
        {note && <p style={{ ...hint, color: t("node.annotation.ring") }}>{note}</p>}

        {/* P8.7.2 — a LIVE progress line (pages fetched / rate-limited / valuing M of N) replaces the
            static spinner, with a Cancel that stops the in-flight fetch/valuation cleanly. */}
        {(busy || progress) && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span aria-live="polite" style={{ fontSize: 12, color: t("ui.text.secondary") }}>{progress || "Working…"}</span>
              {(busy || pollRef.current) && (
                <button style={{ ...btn, padding: "4px 10px", borderColor: t("ui.warning"), color: t("ui.warning") }}
                        onClick={cancel}>Cancel</button>
              )}
            </div>
            <Progress value={jobProg?.valued} max={jobProg?.total} label="ingest progress" />
          </div>
        )}

        {/* P37/UX-05 — after a successful ingest, offer a clear next step ("Done" / "+ Add another") instead
            of leaving the investigator on a spent form. */}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          {succeeded ? (
            <>
              <button style={btn} onClick={onClose}>Done</button>
              <button style={{ ...btn, borderColor: t("node.seed.marker") }} onClick={addAnother}>+ Add another</button>
            </>
          ) : (
            <>
              <button style={btn} onClick={onClose}>Close</button>
              <button style={{ ...btn, borderColor: t("node.seed.marker"),
                               opacity: canIngest ? 1 : 0.5, cursor: canIngest ? "pointer" : "default" }}
                      disabled={!canIngest} onClick={ingest}>
                {busy ? "Ingesting…" : "Ingest"}
              </button>
            </>
          )}
        </div>
    </Modal>
  );
}
