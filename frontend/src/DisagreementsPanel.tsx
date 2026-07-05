import { useEffect, useState } from "react";
import { t } from "./theme/theme";
import Modal from "./Modal";

// FN-09 — a case-wide roster of every subject where SOURCES DISAGREE (attribution label/category, risk
// category, or a movement's valuation). Every source's claim is shown side-by-side and NEVER merged
// (Invariant #4); each subject navigates to the canvas via onFocus. Adjudication stays an explicit
// investigator finding — the tool never picks a winner.
type DisagreementClaim = {
  source: string;
  label?: string; category?: string | null; confidence?: number | null;
  score?: number | null; score_scale?: string | null; rationale?: string | null;
  value?: string; unit_price?: string; currency?: string; price_timestamp?: string;
  retrieved_at?: string; source_query_id?: string | null;
};
type Disagreement = {
  subject_type: "address" | "movement";
  subject_id: string;
  node_id: string | null;
  edge_id?: string;
  movement_kind?: string;
  chain?: string; address?: string; address_display?: string;
  claim_type: "attribution" | "risk" | "valuation";
  fields: string[];
  sources: string[];
  claims: DisagreementClaim[];
};

const j = (r: Response) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)));
const short = (s?: string) => (!s ? "?" : s.length <= 17 ? s : `${s.slice(0, 8)}…${s.slice(-6)}`);

// The value(s) a single source asserts for the disagreeing fields — read straight from its claim, never
// combined with any other source's.
function claimText(d: Disagreement, c: DisagreementClaim): string {
  if (d.claim_type === "attribution")
    return [c.label, c.category ? `(${c.category})` : null].filter(Boolean).join(" ");
  if (d.claim_type === "risk")
    return [c.category, c.score != null ? `· ${c.score}${c.score_scale ? `/${c.score_scale}` : ""}` : null]
      .filter(Boolean).join(" ");
  return [c.value != null ? `${c.value} ${c.currency ?? "USD"}` : null,
          c.unit_price != null ? `@ ${c.unit_price}` : null].filter(Boolean).join(" ");
}

export default function DisagreementsPanel({ onClose, onFocus }: {
  onClose: () => void;
  onFocus: (nodeId: string) => void;
}) {
  const [items, setItems] = useState<Disagreement[]>([]);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    fetch("/api/disagreements").then(j).then((d) => setItems(d.disagreements ?? []))
      .catch((e) => setErr(String(e)));
  }, []);

  // styles (all via the token catalog) — mirrors FindingsPanel
  const surface: React.CSSProperties = { background: t("ui.panel.bg"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 8 };
  const card: React.CSSProperties = { background: t("ui.panel.elevated"), borderRadius: 4,
    padding: "8px 10px", marginBottom: 8 };
  const btn: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "3px 9px", fontSize: 12, cursor: "pointer" };
  const link: React.CSSProperties = { color: t("node.label.color"), cursor: "pointer" };

  const subjectLabel = (d: Disagreement) =>
    d.subject_type === "address" ? short(d.address) : `movement ${short(d.subject_id)}`;

  return (
    <Modal onClose={onClose} labelledBy="disagreements-title"
      backdropStyle={{ position: "fixed", inset: 0, zIndex: 50,
        background: `${t("canvas.background")}d9`,  /* tokenized scrim at ~85% alpha */
        display: "flex", justifyContent: "center", alignItems: "center" }}
      containerStyle={{ ...surface, width: "min(860px, 94vw)",
        height: "min(86vh, 900px)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
          padding: "10px 14px", borderBottom: `1px solid ${t("ui.border")}`, background: t("ui.panel.bg") }}>
          <strong id="disagreements-title">Source disagreements · {items.length}</strong>
          <button onClick={onClose} style={btn}>✕ Close</button>
        </div>
        {err && <div style={{ padding: "4px 14px", color: t("ui.error"), fontSize: 12 }}>{err}</div>}

        <div style={{ flex: 1, overflowY: "auto", padding: "10px 14px" }}>
          {items.length === 0 && !err && (
            <p style={{ color: t("ui.muted"), fontSize: 13 }}>
              No cross-source disagreements — every source agrees on all attribution, risk, and valuation
              claims in this case.
            </p>
          )}
          {items.map((d) => (
            <div key={`${d.claim_type}:${d.subject_id}`} style={card}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
                <div>
                  <span style={{ textTransform: "uppercase", fontSize: 10, letterSpacing: 0.4,
                    color: t("ui.text.secondary"), marginRight: 6 }}>{d.claim_type}</span>
                  {d.node_id
                    ? <span style={link} title="show on the graph" onClick={() => onFocus(d.node_id!)}>{subjectLabel(d)}</span>
                    : <span>{subjectLabel(d)}</span>}
                </div>
                <span style={{ color: t("ui.muted"), fontSize: 11 }}>differ on {d.fields.join(", ")}</span>
              </div>
              <table style={{ width: "100%", marginTop: 6, borderCollapse: "collapse", fontSize: 12 }}>
                <tbody>
                  {d.claims.map((c, i) => (
                    <tr key={`${c.source}:${i}`}>
                      <td style={{ padding: "2px 6px", fontWeight: 600, whiteSpace: "nowrap",
                        verticalAlign: "top", color: t("node.entity.label.color") }}>{c.source}</td>
                      <td style={{ padding: "2px 6px" }}>{claimText(d, c)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
        <div style={{ padding: "6px 14px", borderTop: `1px solid ${t("ui.border")}`, color: t("ui.muted"), fontSize: 11 }}>
          Every source's claim is shown side-by-side, never merged or averaged (Invariant #4). To adjudicate,
          record a finding — the tool never picks a winner for you.
        </div>
    </Modal>
  );
}
