import { useCallback, useEffect, useState } from "react";
import type { GraphNode } from "./Graph";
import { t } from "./theme/theme";

// Aggregated investigator input (from /api/investigator/notes) — every annotation, label override, and
// tag grouped by its target, with a jump-to node id where the target is a graph node.
type NoteGroup = {
  target_type: string; target_id: string; node_id: string | null; label: string;
  annotations: { id: string; content: string; created_at: string }[];
  label_override: string | null; tags: string[];
};
type FindingRef = { id: string; ref_type: string; ref_id: string; note: string | null;
  label: string; node_id: string | null };
type Finding = { id: string; statement: string; assessment: string | null; created_at: string;
  refs: FindingRef[] };

// A ref being assembled in the composer (before the finding is saved).
type DraftRef = { ref_type: string; ref_id: string; label: string; note?: string };

const j = (r: Response) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)));
const post = (url: string, body: unknown) =>
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(j);
const patch = (url: string, body: unknown) =>
  fetch(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(j);
const del = (url: string) => fetch(url, { method: "DELETE" }).then(j);

// A graph node -> a finding ref descriptor (only address/tx nodes are durable ref targets).
function nodeToRef(n: GraphNode | null): DraftRef | null {
  if (!n) return null;
  if (n.kind === "address") return { ref_type: "address", ref_id: n.id.replace(/^addr:/, ""), label: n.label };
  if (n.kind === "transaction") return { ref_type: "transaction", ref_id: n.id.replace(/^tx:/, ""), label: n.label };
  return null;
}

export default function FindingsPanel({ onClose, refreshKey, selected, onFocus, onChanged }: {
  onClose: () => void;
  refreshKey: number;
  selected: GraphNode | null;
  onFocus: (nodeId: string) => void;
  onChanged: () => void;
}) {
  const [notes, setNotes] = useState<NoteGroup[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [err, setErr] = useState<string | null>(null);

  // composer state
  const [stmt, setStmt] = useState("");
  const [assessment, setAssessment] = useState("");
  const [refs, setRefs] = useState<DraftRef[]>([]);
  // inline edit of an existing finding
  const [editing, setEditing] = useState<{ id: string; statement: string; assessment: string } | null>(null);
  // inline edit of an existing annotation (note)
  const [editingNote, setEditingNote] = useState<{ id: string; content: string } | null>(null);

  const refresh = useCallback(() => {
    fetch("/api/investigator/notes").then(j).then((d) => setNotes(d.notes ?? [])).catch(() => setNotes([]));
    fetch("/api/findings").then(j).then((d) => setFindings(d.findings ?? [])).catch(() => setFindings([]));
  }, []);
  useEffect(() => { refresh(); }, [refresh, refreshKey]);

  const did = (p: Promise<unknown>) => p.then(() => { refresh(); onChanged(); }).catch((e) => setErr(String(e)));
  const addRef = (r: DraftRef) =>
    setRefs((rs) => (rs.some((x) => x.ref_type === r.ref_type && x.ref_id === r.ref_id) ? rs : [...rs, r]));

  const saveFinding = () => {
    const statement = stmt.trim();
    if (!statement) return;
    did(post("/api/findings", { statement, assessment: assessment.trim() || null,
      refs: refs.map((r) => ({ ref_type: r.ref_type, ref_id: r.ref_id, note: r.note || null })) }))
      .then(() => { setStmt(""); setAssessment(""); setRefs([]); });
  };

  // --- styles (all via the token catalog) ---
  const surface: React.CSSProperties = { background: t("ui.panel.bg"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 8 };
  const card: React.CSSProperties = { background: t("ui.panel.elevated"), borderRadius: 4,
    padding: "8px 10px", marginBottom: 8 };
  const field: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "4px 7px", fontSize: 13, width: "100%" };
  const btn: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "3px 9px", fontSize: 12, cursor: "pointer" };
  const head: React.CSSProperties = { fontSize: 12, letterSpacing: 0.4, textTransform: "uppercase",
    color: t("ui.text.secondary"), borderBottom: `1px solid ${t("ui.border")}`, paddingBottom: 4, margin: "4px 0 8px" };
  const link: React.CSSProperties = { color: t("node.label.color"), cursor: "pointer" };

  const selRef = nodeToRef(selected);

  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 50,
      background: `${t("canvas.background")}d9`,  /* the catalog bg at ~85% alpha — a tokenized scrim */
      display: "flex", justifyContent: "center", alignItems: "center" }}>
      <div onClick={(e) => e.stopPropagation()} style={{ ...surface, width: "min(1000px, 94vw)",
        height: "min(86vh, 900px)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
          padding: "10px 14px", borderBottom: `1px solid ${t("ui.border")}`, background: t("ui.panel.bg") }}>
          <strong>Findings &amp; Notes</strong>
          <button onClick={onClose} style={btn}>✕ Close</button>
        </div>
        {err && <div style={{ padding: "4px 14px", color: t("ui.error"), fontSize: 12 }}>{err}</div>}

        <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
          {/* LEFT — every annotation / label / tag grouped by target */}
          <div style={{ flex: 1, overflowY: "auto", padding: "10px 14px",
            borderRight: `1px solid ${t("ui.border")}` }}>
            <div style={head}>Notes &amp; labels · {notes.length} target(s)</div>
            {notes.length === 0 && <p style={{ color: t("ui.muted"), fontSize: 13 }}>No annotations, labels, or tags yet.</p>}
            {notes.map((g) => (
              <div key={`${g.target_type}:${g.target_id}`} style={card}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
                  <div>
                    {g.node_id
                      ? <span style={link} onClick={() => onFocus(g.node_id!)}>{g.label}</span>
                      : <span>{g.label}</span>}
                    <span style={{ color: t("ui.muted"), fontSize: 11 }}> · {g.target_type}</span>
                  </div>
                  <button style={{ ...btn, fontSize: 11 }}
                          onClick={() => addRef({ ref_type: g.target_type, ref_id: g.target_id, label: g.label })}>
                    ↪ ref
                  </button>
                </div>
                {g.label_override && <div style={{ fontSize: 12, color: t("node.entity.label.color") }}>label: {g.label_override}</div>}
                {g.tags.length > 0 && <div style={{ fontSize: 11, color: t("ui.muted") }}>tags: {g.tags.join(", ")}</div>}
                {g.annotations.map((a) => (
                  editingNote?.id === a.id ? (
                    <div key={a.id} style={{ marginTop: 4, paddingLeft: 8,
                      borderLeft: `2px solid ${t("node.annotation.ring")}` }}>
                      <textarea value={editingNote.content} rows={2}
                        onChange={(e) => setEditingNote({ id: a.id, content: e.target.value })}
                        style={{ ...field, resize: "vertical", marginBottom: 4 }} />
                      <button style={{ ...btn, fontSize: 11 }} onClick={() => {
                        const c = editingNote.content.trim();
                        if (!c) return;
                        // close only on SUCCESS (a failed save keeps the editor open with the text)
                        patch(`/api/annotations/${a.id}`, { content: c })
                          .then(() => { setEditingNote(null); refresh(); onChanged(); })
                          .catch((e) => setErr(String(e)));
                      }}>Save</button>{" "}
                      <button style={{ ...btn, fontSize: 11, color: t("ui.muted") }}
                        onClick={() => setEditingNote(null)}>Cancel</button>
                    </div>
                  ) : (
                    <div key={a.id} style={{ fontSize: 12, marginTop: 3, paddingLeft: 8,
                      borderLeft: `2px solid ${t("node.annotation.ring")}` }}>
                      <div style={{ whiteSpace: "pre-wrap" }}>{a.content}</div>
                      <div style={{ fontSize: 11, marginTop: 1 }}>
                        <span style={link} onClick={() => setEditingNote({ id: a.id, content: a.content })}>edit</span>
                        {" · "}
                        <span style={{ color: t("ui.error"), cursor: "pointer" }}
                          onClick={() => did(del(`/api/annotations/${a.id}`))}>delete</span>
                      </div>
                    </div>
                  )
                ))}
              </div>
            ))}
          </div>

          {/* RIGHT — compose + list/edit findings */}
          <div style={{ flex: 1.1, overflowY: "auto", padding: "10px 14px" }}>
            <div style={head}>Compose a finding</div>
            <div style={card}>
              <textarea value={stmt} placeholder="statement (what you conclude)…" rows={2}
                        onChange={(e) => setStmt(e.target.value)} style={{ ...field, resize: "vertical", marginBottom: 6 }} />
              <input value={assessment} placeholder="assessment (optional, e.g. high / medium)"
                     onChange={(e) => setAssessment(e.target.value)} style={{ ...field, marginBottom: 6 }} />
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 6 }}>
                {refs.map((r) => (
                  <span key={`${r.ref_type}:${r.ref_id}`} style={{ ...btn, cursor: "default",
                    borderColor: t("node.annotation.ring") }}>
                    {r.ref_type}: {r.label}
                    <span style={{ marginLeft: 6, cursor: "pointer", color: t("ui.muted") }}
                          onClick={() => setRefs((rs) => rs.filter((x) => !(x.ref_type === r.ref_type && x.ref_id === r.ref_id)))}>✕</span>
                  </span>
                ))}
                {selRef && (
                  <button style={{ ...btn, fontSize: 11 }} onClick={() => addRef(selRef)}>
                    + add selected ({selRef.label})
                  </button>
                )}
              </div>
              <button onClick={saveFinding} disabled={!stmt.trim()}
                      style={{ ...btn, opacity: stmt.trim() ? 1 : 0.5 }}>Save finding</button>
            </div>

            <div style={head}>Findings · {findings.length}</div>
            {findings.length === 0 && <p style={{ color: t("ui.muted"), fontSize: 13 }}>No findings composed yet.</p>}
            {findings.map((f) => (
              <div key={f.id} style={card}>
                {editing?.id === f.id ? (
                  <>
                    <textarea value={editing.statement} rows={2}
                              onChange={(e) => setEditing({ ...editing, statement: e.target.value })}
                              style={{ ...field, resize: "vertical", marginBottom: 6 }} />
                    <input value={editing.assessment} placeholder="assessment"
                           onChange={(e) => setEditing({ ...editing, assessment: e.target.value })}
                           style={{ ...field, marginBottom: 6 }} />
                    <button style={btn} onClick={() => did(patch(`/api/findings/${f.id}`,
                      { statement: editing.statement, assessment: editing.assessment.trim() || null }))
                      .then(() => setEditing(null))}>Save</button>{" "}
                    <button style={{ ...btn, color: t("ui.muted") }} onClick={() => setEditing(null)}>Cancel</button>
                  </>
                ) : (
                  <>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                      <div style={{ fontWeight: 600 }}>{f.statement}</div>
                      <span style={{ whiteSpace: "nowrap" }}>
                        <span style={link} onClick={() => setEditing({ id: f.id, statement: f.statement, assessment: f.assessment ?? "" })}>edit</span>
                        {" · "}
                        <span style={{ color: t("ui.error"), cursor: "pointer" }} onClick={() => did(del(`/api/findings/${f.id}`))}>delete</span>
                      </span>
                    </div>
                    {f.assessment && <div style={{ color: t("ui.text.secondary"), fontSize: 12 }}>assessment: {f.assessment}</div>}
                    {f.refs.map((r) => (
                      <div key={r.id} style={{ fontSize: 12, marginTop: 3, display: "flex", justifyContent: "space-between", gap: 6 }}>
                        <span>
                          <span style={{ color: t("ui.muted") }}>{r.ref_type}: </span>
                          {r.node_id ? <span style={link} onClick={() => onFocus(r.node_id!)}>{r.label}</span> : <span>{r.label}</span>}
                          {r.note ? <span style={{ color: t("ui.muted") }}> — {r.note}</span> : null}
                        </span>
                        <span style={{ color: t("ui.muted"), cursor: "pointer" }} onClick={() => did(del(`/api/findings/refs/${r.id}`))}>✕</span>
                      </div>
                    ))}
                    {selRef && (
                      <button style={{ ...btn, fontSize: 11, marginTop: 5 }}
                              onClick={() => did(post(`/api/findings/${f.id}/refs`,
                                { ref_type: selRef.ref_type, ref_id: selRef.ref_id, note: null }))}>
                        + ref selected ({selRef.label})
                      </button>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        </div>
        <div style={{ padding: "6px 14px", borderTop: `1px solid ${t("ui.border")}`, color: t("ui.muted"), fontSize: 11 }}>
          Findings + notes are durable claims (never facts) and flow into the report's Findings section + notes appendix.
        </div>
      </div>
    </div>
  );
}
