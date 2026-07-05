import { useEffect, useState } from "react";
import { t } from "./theme/theme";
import Modal from "./Modal";

// FN-14 (P24) — the case ACTIVITY TIMELINE: one read-only, time-ordered log of everything that happened to
// the case (data fetches + the investigator's traces, findings, annotations, tags, trace edits, bridge
// links, exhibits, reports). Backend-aggregated + deterministically ordered (`/api/activity`); this panel
// only renders it. Mirrors DisagreementsPanel's modal shell + token styling.
type ActivityEvent = {
  ts: string | null;
  kind: string;
  summary: string;
  ref_type: string;
  ref_id: string;
  detail?: string | null;
};

const j = (r: Response) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)));

// Human label per event kind (the backend `kind` is a stable machine token).
const KIND_LABELS: Record<string, string> = {
  fetch: "Fetch", trace: "Trace", finding: "Finding", annotation: "Annotation", tag: "Tag",
  trace_edit: "Trace edit", bridge_link: "Bridge link", exhibit: "Exhibit", report: "Report",
};

// "2026-01-01T00:00:00Z" -> "2026-01-01 00:00:00" (a readable timestamp; unknown -> "—").
function whenText(ts: string | null): string {
  if (!ts) return "—";
  return ts.replace("T", " ").replace(/Z$/, "");
}

export default function ActivityPanel({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<ActivityEvent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    fetch("/api/activity").then(j).then((d) => setItems(d.activity ?? [])).catch((e) => setErr(String(e)));
  }, []);

  const surface: React.CSSProperties = { background: t("ui.panel.bg"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 8 };
  const btn: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "3px 9px", fontSize: 12, cursor: "pointer" };
  const badge: React.CSSProperties = { textTransform: "uppercase", fontSize: 10, letterSpacing: 0.4,
    color: t("ui.text.secondary"), border: `1px solid ${t("ui.border")}`, borderRadius: 3,
    padding: "1px 5px", whiteSpace: "nowrap" };

  return (
    <Modal onClose={onClose} labelledBy="activity-title"
      backdropStyle={{ position: "fixed", inset: 0, zIndex: 50,
        background: `${t("canvas.background")}d9`,
        display: "flex", justifyContent: "center", alignItems: "center" }}
      containerStyle={{ ...surface, width: "min(860px, 94vw)",
        height: "min(86vh, 900px)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
          padding: "10px 14px", borderBottom: `1px solid ${t("ui.border")}`, background: t("ui.panel.bg") }}>
          <strong id="activity-title">Case activity · {items.length}</strong>
          <button onClick={onClose} style={btn}>✕ Close</button>
        </div>
        {err && <div style={{ padding: "4px 14px", color: t("ui.error"), fontSize: 12 }}>{err}</div>}

        <div style={{ flex: 1, overflowY: "auto", padding: "6px 14px" }}>
          {items.length === 0 && !err && (
            <p style={{ color: t("ui.muted"), fontSize: 13 }}>
              No activity yet — fetch an address, or record a finding/annotation/trace, and it appears here.
            </p>
          )}
          {items.map((e) => (
            <div key={`${e.kind}:${e.ref_id}`} style={{ display: "flex", gap: 10, alignItems: "baseline",
              padding: "6px 0", borderBottom: `1px solid ${t("ui.border")}` }}>
              <span style={{ color: t("ui.muted"), fontSize: 11, fontFamily: "monospace",
                whiteSpace: "nowrap" }}>{whenText(e.ts)}</span>
              <span style={badge}>{KIND_LABELS[e.kind] ?? e.kind}</span>
              <span style={{ fontSize: 13 }}>
                {e.summary}
                {e.detail && <span style={{ color: t("ui.muted") }}> — {e.detail}</span>}
              </span>
            </div>
          ))}
        </div>
        <div style={{ padding: "6px 14px", borderTop: `1px solid ${t("ui.border")}`, color: t("ui.muted"), fontSize: 11 }}>
          A read-only, time-ordered log of every case event — data fetches and your own constructions. Feeds
          the chain-of-custody narrative in the report.
        </div>
    </Modal>
  );
}
