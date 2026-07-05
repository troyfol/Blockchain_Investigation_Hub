import { forwardRef, useCallback, useImperativeHandle, useState } from "react";
import { isWindowed } from "./cases";
import { getActiveJob } from "./jobs";
import { openReportFile, type ReportResult, reportSummary, runReport } from "./report";
import { t } from "./theme/theme";
import Modal from "./Modal";

// The Report button (P8.5): generate an immutable report of the active case from the UI, show its
// content_hash (the immutability proof) + where the files landed, and open the PDF (OS opener, windowed
// app). A missing browser engine is a clean skip, never an error — the HTML report is always produced.

const field: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 4, padding: "3px 6px", fontSize: 12, cursor: "pointer", whiteSpace: "nowrap",
};
const card: React.CSSProperties = {
  background: t("ui.panel.bg"), border: `1px solid ${t("ui.border")}`, borderRadius: 10, padding: 18,
  display: "flex", flexDirection: "column", gap: 12, width: "100%", maxWidth: 560,
};
const btn: React.CSSProperties = { ...field, padding: "7px 13px", fontSize: 13 };
const mono: React.CSSProperties = {
  fontFamily: "ui-monospace, monospace", fontSize: 12, color: t("ui.text.secondary"),
  background: t("ui.panel.elevated"), border: `1px solid ${t("ui.border")}`, borderRadius: 6,
  padding: "6px 9px", overflowWrap: "anywhere",
};

export type ReportHandle = { generate: () => void };

const ReportButton = forwardRef<ReportHandle, { viewParams?: Record<string, unknown> }>(
  function ReportButton({ viewParams }, ref) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ReportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opened, setOpened] = useState(false);
  const [valNote, setValNote] = useState<string | null>(null);   // P8.7.3 #4 — valuation-in-progress hint

  const generate = useCallback(() => {
    setBusy(true); setError(null); setResult(null); setOpened(false); setValNote(null);
    // P8.7.3 #4 — if a background valuation is still running, the report will note partial USD coverage;
    // tell the investigator so a half-valued snapshot isn't a surprise (they can wait + re-generate).
    getActiveJob().then((j) => {
      if (j && j.kind === "valuation" && j.state === "running") {
        setValNote(`Valuation still running (${j.valued}${j.total ? ` of ${j.total}` : ""} priced) — this report will note partial USD coverage.`);
      }
    }).catch(() => {});
    // Pass the active view so the report renders the CURRENT bounded view (P8.7.1 #2), not the full case.
    runReport(undefined, viewParams)
      .then((r) => {
        setResult(r);
        // Auto-open the PDF in the windowed app (OS default opener); harmless to skip in browser/dev.
        if (r.pdf_path && isWindowed()) {
          openReportFile(r.pdf_path).then(() => setOpened(true)).catch(() => {});
        }
      })
      .catch((e) => setError(String(e instanceof Error ? e.message : e)))
      .finally(() => setBusy(false));
  }, [viewParams]);

  // P32/UX-07 — expose generate so a global "r" shortcut fires the same report (no-op while one is running).
  useImperativeHandle(ref, () => ({ generate: () => { if (!busy) generate(); } }), [generate, busy]);

  const openPdf = useCallback(() => {
    if (!result?.pdf_path) return;
    openReportFile(result.pdf_path).then(() => setOpened(true))
      .catch((e) => setError(String(e instanceof Error ? e.message : e)));
  }, [result]);

  const summary = result ? reportSummary(result) : null;
  const backdrop: React.CSSProperties = {
    position: "fixed", inset: 0, zIndex: 86, background: t("ui.app.bg"),
    display: "flex", alignItems: "flex-start", justifyContent: "center", overflow: "auto", padding: 32,
  };

  return (
    <>
      <button onClick={generate} disabled={busy} style={{ ...field, opacity: busy ? 0.6 : 1 }}
              title="Generate an immutable report of this case (HTML + PDF via the OS browser engine)">
        {busy ? "Reporting…" : "Report"}
      </button>

      {(result || error) && (
        <Modal onClose={() => { setResult(null); setError(null); }} backdropStyle={backdrop}
               containerStyle={card} labelledBy="report-title">
            <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
              <h2 id="report-title" style={{ margin: 0, fontSize: 17, color: t("ui.text") }}>Report</h2>
              <button style={{ ...btn, marginLeft: "auto" }} onClick={() => { setResult(null); setError(null); }}
                      aria-label="Close">✕</button>
            </div>

            {error && (
              <div style={{ ...card, padding: 10, maxWidth: "none", borderColor: t("ui.error") }}>
                <span style={{ color: t("ui.error"), fontSize: 13 }}>{error}</span>
              </div>
            )}

            {valNote && (
              <div style={{ ...card, padding: 10, maxWidth: "none", borderColor: t("ui.warning") }}>
                <span style={{ color: t("ui.warning"), fontSize: 12 }}>{valNote}</span>
              </div>
            )}

            {result && summary && (
              <>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <span style={{ fontSize: 12, color: t("ui.text.secondary") }}>
                    Content hash (immutability proof)
                  </span>
                  <code style={mono} title={result.content_hash}>{result.content_hash}</code>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <span style={{ fontSize: 12, color: t("ui.text.secondary") }}>Report HTML (the hashed source of truth)</span>
                  <code style={mono}>{result.html_path}</code>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 12, color: summary.hasPdf ? t("node.annotation.ring") : t("ui.muted") }}>
                    {summary.pdfNote}{opened ? " · opened" : ""}
                  </span>
                  {result.pdf_path && (
                    <button style={btn} onClick={openPdf}>Open PDF</button>
                  )}
                </div>
              </>
            )}
        </Modal>
      )}
    </>
  );
});

export default ReportButton;
