import { useEffect, useState } from "react";
import type { GraphEdge, GraphNode, NodeValue } from "./Graph";
import { sourceColor, t } from "./theme/theme";

const fmtUsd = (v?: number | null): string | null =>
  v == null ? null : `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

// Panel font sizes are expressed in `em` RELATIVE to the panel root, whose px size is scaled by the
// side-panel font-size UI pref (feature 6). So one root size drives the whole panel and a pref change
// scales every line uniformly — independent of the graph zoom and of the graph-label font pref.
const BASE_PX = 13;
const em = (px: number): string => `${(px / BASE_PX).toFixed(3)}em`;

// The panel chrome resolves from the same catalog as the graph (no hardcoded hex) — it goes dark with
// the rest of the app under the neo-tokyo-night theme, light under print-light. (fontSize is applied at
// render from the UI pref, so it is intentionally omitted here.)
const PANEL: React.CSSProperties = {
  width: 300,
  borderLeft: `1px solid ${t("ui.border")}`,
  background: t("ui.panel.bg"),
  color: t("ui.text"),
  padding: "12px 14px",
  overflowY: "auto",
};

// --- claim shapes (from GET /api/address/:id/claims — claims_display.address_claims) ---------
type Attribution = {
  id: string; label: string; category: string | null;
  source: string; confidence: number | null; note: string | null;
};
type Risk = {
  id: string; category: string; score: number | null;
  score_scale: string | null; rationale: string | null; source: string;
};
type EntityMembership = {
  name: string; entity_type: string | null; origin: string;
  source: string; method: string; confidence: number | null; flags: string | null;
};
export type AddressClaims = {
  address_id: string;
  attributions_by_source: Record<string, Attribution[]>;
  risks_by_source: Record<string, Risk[]>;
  entities: EntityMembership[];
};

// One trace/path (from GET /api/traces). `name` is the DISPLAY name (a custom investigator label
// overrides the trace's `original_name`).
export type TraceInfo = {
  id: string; name: string; original_name: string; description: string | null;
  btc_link_count: number; transfer_count: number; custom_label: boolean;
};

// A ranked counterparty + the node's value summary (from GET /api/node/:id/summary).
export type Counterparty = {
  id: string; label?: string; kind?: string; address?: string;
  risk_level?: string | null; has_attribution?: boolean | null; entity_label?: string | null;
  in_usd?: number | null; out_usd?: number | null; usd?: number | null;
  in_count: number; out_count: number; count: number;
};
export type NodeSummary = {
  node_id: string; label?: string; val?: NodeValue | null;
  counterparties: Counterparty[]; counterparty_total: number; flagged: Counterparty[];
};

// A durable investigator note (free text) on a target (GET/POST /api/target/:type/:id/annotations).
export type Annotation = { id: string; content: string; created_at: string };

// A durable target the investigator can rename + annotate — an address/transaction NODE or a flow EDGE.
// (External / group / aggregate nodes, and tx_input / trace / aggregate edges, are view artifacts with
// no durable object behind them, so they return null and the rename/annotate controls are hidden.)
type Target = { ttype: string; tid: string };
function targetOf(node: GraphNode | null, edge: GraphEdge | null): Target | null {
  if (node) {
    if (node.kind === "address") return { ttype: "address", tid: node.id.replace(/^addr:/, "") };
    if (node.kind === "transaction") return { ttype: "transaction", tid: node.id.replace(/^tx:/, "") };
    return null;
  }
  if (edge && edge.ann_type && edge.ann_id) return { ttype: edge.ann_type, tid: edge.ann_id };
  return null;
}

// One durable note — view mode (content + timestamp + edit/delete links) or an inline edit field.
function AnnotationItem({ a, onEdit, onDelete }: {
  a: Annotation; onEdit?: (id: string, content: string) => void | Promise<unknown>;
  onDelete?: (id: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(a.content);
  // Re-sync the draft to the source content whenever it changes externally (e.g. the same note is edited
  // in the Findings modal) — but only while NOT actively editing, so an open editor isn't clobbered.
  useEffect(() => { if (!editing) setText(a.content); }, [a.content, editing]);
  const field: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 3, padding: "3px 6px", fontSize: em(12) };
  const card: React.CSSProperties = { background: t("ui.panel.elevated"), borderRadius: 3,
    borderLeft: `3px solid ${t("node.annotation.ring")}`, padding: "5px 8px", marginBottom: 5 };
  if (editing) {
    // Close the editor only once the save SUCCEEDS — a failed save keeps the editor open with the text.
    const save = () => {
      const v = text.trim();
      if (!v || !onEdit) { setEditing(false); return; }
      Promise.resolve(onEdit(a.id, v)).then(() => setEditing(false)).catch(() => { /* keep editor open */ });
    };
    return (
      <div style={card}>
        <textarea autoFocus value={text} rows={2} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
            else if (e.key === "Escape") { setText(a.content); setEditing(false); }
          }}
          style={{ ...field, width: "100%", boxSizing: "border-box", resize: "vertical" }} />
        <div style={{ display: "flex", gap: 6, marginTop: 3 }}>
          <button style={{ ...field, cursor: "pointer" }} onClick={save}>Save</button>
          <button style={{ ...field, cursor: "pointer", color: t("ui.muted") }}
                  onClick={() => { setText(a.content); setEditing(false); }}>Cancel</button>
        </div>
      </div>
    );
  }
  return (
    <div style={card}>
      <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{a.content}</div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 2, gap: 6 }}>
        <span style={{ color: t("ui.muted"), fontSize: em(10) }}>{a.created_at}</span>
        {(onEdit || onDelete) && (
          <span style={{ whiteSpace: "nowrap", fontSize: em(10) }}>
            {onEdit && <span style={{ cursor: "pointer", color: t("node.label.color") }}
              onClick={() => { setText(a.content); setEditing(true); }}>edit</span>}
            {onEdit && onDelete && <span style={{ color: t("ui.muted") }}> · </span>}
            {onDelete && <span style={{ cursor: "pointer", color: t("ui.error") }}
              onClick={() => onDelete(a.id)}>delete</span>}
          </span>
        )}
      </div>
    </div>
  );
}

// Free-text notes on the selected target — durable claims (the green-accent companion to Rename).
function AnnotationsSection({ target, annotations, onAdd, onEdit, onDelete }: {
  target: Target; annotations: Annotation[];
  onAdd: (ttype: string, tid: string, content: string) => void;
  onEdit?: (id: string, content: string) => void | Promise<unknown>;
  onDelete?: (id: string) => void;
}) {
  const [text, setText] = useState("");
  const add = () => { const v = text.trim(); if (v) { onAdd(target.ttype, target.tid, v); setText(""); } };
  const field: React.CSSProperties = { background: t("ui.panel.elevated"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 3, padding: "3px 6px", fontSize: em(12) };
  return (
    <>
      <SectionHeader title={`Annotations · ${annotations.length}`} />
      {annotations.map((a) => (
        <AnnotationItem key={a.id} a={a} onEdit={onEdit} onDelete={onDelete} />
      ))}
      <div style={{ display: "flex", gap: 4, marginTop: 4 }}>
        <input value={text} placeholder="add a note…" onChange={(e) => setText(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} style={{ ...field, flex: 1, minWidth: 0 }} />
        <button onClick={add} style={{ ...field, cursor: "pointer" }}>Add</button>
      </div>
    </>
  );
}

// The node's received / sent value (native + USD value-at-time). Honest: no current-price source means
// no "now" figure (never fabricated) — only value-at-time is shown.
function ValueHeader({ val }: { val: NodeValue }) {
  const sym = val.native_symbol ?? "";
  const row = (lbl: string, nat?: number | null, usd?: number | null) =>
    (nat != null || usd != null) ? (
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <span style={{ color: t("ui.muted") }}>{lbl}</span>
        <span style={{ textAlign: "right" }}>
          {nat != null ? `${nat} ${sym}` : ""}{usd != null ? <span style={{ color: t("ui.text.secondary") }}>{nat != null ? "  " : ""}~{fmtUsd(usd)}</span> : null}
        </span>
      </div>
    ) : null;
  return (
    <div style={{ background: t("ui.panel.elevated"), borderRadius: 3, padding: "6px 8px", marginBottom: 8 }}>
      {row("received", val.in_native, val.in_usd)}
      {row("sent", val.out_native, val.out_usd)}
      <div style={{ color: t("ui.muted"), fontSize: em(10), marginTop: 3 }}>USD = value-at-time (DeFiLlama).</div>
    </div>
  );
}

function CounterpartyRow({ cp, onFocus }: { cp: Counterparty; onFocus?: (id: string) => void }) {
  const marker = cp.risk_level === "sanctioned" ? "⛔" : cp.risk_level ? "⚠" : cp.has_attribution ? "◆" : "";
  const dir = cp.in_count && cp.out_count ? "↔" : cp.in_count ? "→" : "←";  // → = received from, ← = sent to
  return (
    <div onClick={() => onFocus?.(cp.id)}
         style={{ display: "flex", justifyContent: "space-between", gap: 6, padding: "3px 4px",
                  borderRadius: 3, cursor: onFocus ? "pointer" : "default", borderLeft: cp.risk_level || cp.has_attribution ? `2px solid ${t("node.entity.ring")}` : "2px solid transparent" }}>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {marker && <span>{marker} </span>}{dir} {cp.entity_label || cp.label || cp.id}
      </span>
      <span style={{ color: t("ui.text.secondary"), whiteSpace: "nowrap" }}>
        {cp.usd != null ? fmtUsd(cp.usd) : <span style={{ color: t("ui.muted") }}>—</span>}{cp.count > 1 ? ` ·${cp.count}` : ""}
      </span>
    </div>
  );
}

function RankedList({ summary, onFocus }: { summary: NodeSummary; onFocus?: (id: string) => void }) {
  const cps = summary.counterparties ?? [];
  if (!cps.length) return null;
  return (
    <>
      <SectionHeader title={`Counterparties · top ${cps.length} of ${summary.counterparty_total}`} />
      {summary.flagged?.length > 0 && (
        <div style={{ color: t("ui.muted"), fontSize: em(11), marginBottom: 2 }}>
          {summary.flagged.length} flagged (risk / attribution)
        </div>
      )}
      <div style={{ fontSize: em(12) }}>
        {cps.map((cp) => <CounterpartyRow key={cp.id} cp={cp} onFocus={onFocus} />)}
      </div>
      <p style={{ color: t("ui.muted"), fontSize: em(10), marginTop: 4 }}>
        Ranked by USD value-at-time. Click a row to center the graph on it.
      </p>
    </>
  );
}

// An inline "✎ Rename" control: an investigator label edit, saved on Enter / Save. Used for a node's
// custom label, a flow/edge's name, and a trace/path's name (the universal rename — feature A3).
function LabelEditor({ initial, placeholder, onSave }: {
  initial: string; placeholder: string; onSave: (value: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(initial);
  const btn: React.CSSProperties = {
    background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
    borderRadius: 3, padding: "2px 8px", fontSize: em(11), cursor: "pointer",
  };
  if (!editing) {
    return (
      <button style={btn} onClick={() => { setValue(initial); setEditing(true); }}>✎ Rename</button>
    );
  }
  const save = () => { const v = value.trim(); if (v) onSave(v); setEditing(false); };
  return (
    <span style={{ display: "flex", gap: 4, marginTop: 4 }}>
      <input
        autoFocus value={value} placeholder={placeholder}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") save(); else if (e.key === "Escape") setEditing(false); }}
        style={{ flex: 1, minWidth: 0, background: t("ui.panel.elevated"), color: t("ui.text"),
          border: `1px solid ${t("ui.border")}`, borderRadius: 3, padding: "2px 6px", fontSize: em(12) }}
      />
      <button style={btn} onClick={save}>Save</button>
      <button style={{ ...btn, color: t("ui.muted") }} onClick={() => setEditing(false)}>✕</button>
    </span>
  );
}

// The universal Rename + Annotations block — rendered for EVERY durable target (address, transaction,
// flow), so renaming + annotating works the same way for a node and a flow (feature A3).
function RenameAndNotes({ target, currentLabel, isCustom, annotations, onSaveLabel, onAddAnnotation,
                         onEditAnnotation, onDeleteAnnotation }: {
  target: Target; currentLabel: string; isCustom: boolean; annotations: Annotation[];
  onSaveLabel?: (ttype: string, tid: string, label: string) => void;
  onAddAnnotation?: (ttype: string, tid: string, content: string) => void;
  onEditAnnotation?: (id: string, content: string) => void | Promise<unknown>;
  onDeleteAnnotation?: (id: string) => void;
}) {
  return (
    <>
      {onSaveLabel && (
        <div style={{ marginBottom: 8 }}>
          <LabelEditor initial={currentLabel} placeholder="custom label"
            onSave={(v) => onSaveLabel(target.ttype, target.tid, v)} />
          {isCustom && (
            <div style={{ color: t("ui.muted"), fontSize: em(11), marginTop: 2 }}>
              investigator label (overrides the auto label)
            </div>
          )}
        </div>
      )}
      {onAddAnnotation && (
        <AnnotationsSection target={target} annotations={annotations} onAdd={onAddAnnotation}
          onEdit={onEditAnnotation} onDelete={onDeleteAnnotation} />
      )}
    </>
  );
}

function TraceList({ traces, onSaveTraceLabel }: {
  traces: TraceInfo[]; onSaveTraceLabel: (traceId: string, label: string) => void;
}) {
  return (
    <>
      <SectionHeader title={`Traces · ${traces.length}`} />
      {traces.map((tr) => (
        <div key={tr.id} style={{ borderLeft: `3px solid ${t("edge.trace.fifo.line")}`,
          background: t("ui.panel.elevated"), borderRadius: 3, padding: "6px 8px", marginBottom: 6 }}>
          <div style={{ fontWeight: 600, wordBreak: "break-word" }}>{tr.name}</div>
          <div style={{ color: t("ui.muted"), fontSize: em(11), marginBottom: 2 }}>
            {tr.btc_link_count} link(s)
            {tr.transfer_count ? ` · ${tr.transfer_count} transfer(s)` : ""}
            {tr.custom_label ? ` · renamed from "${tr.original_name}"` : ""}
          </div>
          <LabelEditor initial={tr.name} placeholder="trace label"
            onSave={(v) => onSaveTraceLabel(tr.id, v)} />
        </div>
      ))}
    </>
  );
}

// One color per source (resolved from the shared token catalog — `source.<name>`), so two sources are
// visually distinct side-by-side, never merged (Invariant #4). Same colors as the graph's source cues.

function Row({ k, v }: { k: string; v?: string }) {
  if (!v) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ color: t("ui.muted"), fontSize: em(11), textTransform: "uppercase" }}>{k}</div>
      <div style={{ wordBreak: "break-all" }}>{v}</div>
    </div>
  );
}

function SectionHeader({ title }: { title: string }) {
  return (
    <div style={{ margin: "16px 0 6px", paddingBottom: 4, borderBottom: `1px solid ${t("ui.border")}`,
                  fontSize: em(11), letterSpacing: 0.4, textTransform: "uppercase", color: t("ui.text.secondary") }}>
      {title}
    </div>
  );
}

function SourceTag({ source }: { source: string }) {
  return (
    <span style={{ display: "inline-block", fontSize: em(10), fontWeight: 600, color: t("ui.onAccent"),
                   background: sourceColor(source), borderRadius: 3, padding: "1px 6px",
                   textTransform: "uppercase", letterSpacing: 0.3 }}>
      {source}
    </span>
  );
}

// A claim card whose left border is the source color — disagreement stays visible, never collapsed.
function ClaimCard({ source, children }: { source: string; children: React.ReactNode }) {
  return (
    <div style={{ borderLeft: `3px solid ${sourceColor(source)}`, background: t("ui.panel.elevated"),
                  borderRadius: 3, padding: "6px 8px", marginBottom: 6 }}>
      <div style={{ marginBottom: 4 }}><SourceTag source={source} /></div>
      {children}
    </div>
  );
}

function pct(c: number | null): string | null {
  return c == null ? null : `${Math.round(c * 100)}% confidence`;
}

// --- the FLOW / edge inspector: a tapped edge's facts + universal rename / annotate -----------
const EDGE_KIND_LABEL: Record<string, string> = {
  transfer: "Flow · transfer (EVM)", tx_output: "Flow · output (BTC)", tx_input: "Flow · input (BTC)",
  trace: "Trace overlay (convention)", aggregate: "Dust bundle (display-only)",
};

function EdgeView({ edge, nodesById, target, annotations, onSaveLabel, onAddAnnotation,
                   onEditAnnotation, onDeleteAnnotation, onFocus }: {
  edge: GraphEdge; nodesById: Record<string, GraphNode>; target: Target | null;
  annotations: Annotation[];
  onSaveLabel?: (ttype: string, tid: string, label: string) => void;
  onAddAnnotation?: (ttype: string, tid: string, content: string) => void;
  onEditAnnotation?: (id: string, content: string) => void | Promise<unknown>;
  onDeleteAnnotation?: (id: string) => void;
  onFocus?: (nodeId: string) => void;
}) {
  const src = nodesById[edge.source];
  const tgt = nodesById[edge.target];
  const chain = src?.chain || tgt?.chain;
  const endpoint = (n: GraphNode | undefined, id: string) => (
    <span onClick={() => onFocus?.(id)} style={{ cursor: onFocus ? "pointer" : "default",
      color: onFocus ? t("node.label.color") : t("ui.text"), wordBreak: "break-all" }}>
      {n?.label || id}
    </span>
  );
  return (
    <>
      <h3 style={{ margin: "0 0 6px", fontSize: em(15) }}>{EDGE_KIND_LABEL[edge.kind] || "Flow"}</h3>
      <div style={{ background: t("ui.panel.elevated"), borderRadius: 3, padding: "6px 8px", marginBottom: 8 }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {endpoint(src, edge.source)}<span style={{ color: t("ui.muted") }}>→</span>{endpoint(tgt, edge.target)}
        </div>
      </div>
      {edge.parallel_aggregate && (
        <div style={{ marginBottom: 8, background: t("ui.panel.elevated"), borderRadius: 3, padding: "6px 8px" }}>
          <div style={{ color: t("ui.muted"), fontSize: em(11), textTransform: "uppercase" }}>collapsed flow</div>
          <div>{(edge.count ?? 0).toLocaleString()} movements between these endpoints, summed.</div>
          {edge.no_price_count ? (
            <div style={{ color: t("ui.muted"), fontSize: em(11) }}>{edge.no_price_count.toLocaleString()} with no USD price (excluded from the USD sum).</div>
          ) : null}
          <div style={{ color: t("ui.muted"), fontSize: em(10) }}>
            A display rollup of real same-endpoint facts — each movement keeps its own provenance.
          </div>
        </div>
      )}
      {edge.custom_label && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ color: t("ui.muted"), fontSize: em(11), textTransform: "uppercase" }}>label</div>
          <div style={{ color: t("node.entity.label.color"), wordBreak: "break-word" }}>{edge.custom_label}</div>
        </div>
      )}
      <Row k="value" v={edge.value_label} />
      {edge.value_usd_label && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ color: t("ui.muted"), fontSize: em(11), textTransform: "uppercase" }}>value-at-time</div>
          <div>{edge.value_usd_label}
            {edge.value_contested && <span style={{ color: t("ui.muted"), fontSize: em(11) }}> · multiple sources priced this (see node detail)</span>}
          </div>
          <div style={{ color: t("ui.muted"), fontSize: em(10) }}>USD = value-at-time (DeFiLlama).</div>
        </div>
      )}
      {edge.no_price && (
        <div style={{ marginBottom: 8, color: t("ui.muted"), fontSize: em(11) }}>
          No USD price for this movement — shown as a gap, never a fabricated $0.
        </div>
      )}
      <Row k="chain" v={chain} />
      <Row k="finality" v={edge.finality_status} />
      {target && (onSaveLabel || onAddAnnotation) ? (
        <RenameAndNotes target={target} currentLabel={edge.custom_label ?? ""}
          isCustom={!!edge.custom_label} annotations={annotations}
          onSaveLabel={onSaveLabel} onAddAnnotation={onAddAnnotation}
          onEditAnnotation={onEditAnnotation} onDeleteAnnotation={onDeleteAnnotation} />
      ) : (
        <p style={{ color: t("ui.muted"), fontSize: em(11), marginTop: 12 }}>
          This overlay/bundle is a display-only view artifact — it has no underlying object to rename or
          annotate. Tap a transfer or output flow to label it.
        </p>
      )}
    </>
  );
}

export default function SidePanel({ node, edge = null, claims, summary, traces = [], annotations = [],
                                    nodesById = {}, onAddAnnotation, onEditAnnotation, onDeleteAnnotation,
                                    onSaveLabel, onSaveTraceLabel, onFocus, fontScale = 1 }: {
  node: GraphNode | null;
  edge?: GraphEdge | null;
  claims: AddressClaims | null;
  summary?: NodeSummary | null;
  traces?: TraceInfo[];
  annotations?: Annotation[];
  nodesById?: Record<string, GraphNode>;
  onAddAnnotation?: (ttype: string, tid: string, content: string) => void;
  onEditAnnotation?: (id: string, content: string) => void | Promise<unknown>;
  onDeleteAnnotation?: (id: string) => void;
  onSaveLabel?: (ttype: string, tid: string, label: string) => void;
  onSaveTraceLabel?: (traceId: string, label: string) => void;
  onFocus?: (nodeId: string) => void;
  fontScale?: number;
}) {
  // One root font-size drives the whole panel; the UI pref scales it (children use em — feature 6).
  const panelStyle: React.CSSProperties = { ...PANEL, fontSize: BASE_PX * fontScale };

  // The Traces list (rename paths) is shown whether or not a node is selected.
  const traceSection = traces.length > 0 && onSaveTraceLabel
    ? <TraceList traces={traces} onSaveTraceLabel={onSaveTraceLabel} />
    : null;

  // A tapped FLOW (edge) takes over the panel: its facts + the universal rename / annotate controls.
  if (edge && !node) {
    return (
      <aside style={panelStyle}>
        {traceSection}
        <EdgeView edge={edge} nodesById={nodesById} target={targetOf(null, edge)}
          annotations={annotations} onSaveLabel={onSaveLabel} onAddAnnotation={onAddAnnotation}
          onEditAnnotation={onEditAnnotation} onDeleteAnnotation={onDeleteAnnotation}
          onFocus={onFocus} />
      </aside>
    );
  }

  if (!node) {
    return (
      <aside style={panelStyle}>
        {traceSection}
        <p style={{ color: t("ui.muted") }}>Select a node or flow to see its facts and sourced claims.</p>
      </aside>
    );
  }

  const attrSources = claims ? Object.entries(claims.attributions_by_source) : [];
  const riskSources = claims ? Object.entries(claims.risks_by_source) : [];
  const entities = claims?.entities ?? [];
  const hasClaims = attrSources.length > 0 || riskSources.length > 0 || entities.length > 0;

  const val = summary?.val ?? node.val ?? null;
  const canFocus = onFocus && (node.kind === "address" || node.kind === "transaction");
  const target = targetOf(node, null);  // address / transaction nodes are durable targets

  return (
    <aside style={panelStyle}>
      {traceSection}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0, textTransform: "capitalize" }}>{node.kind}</h3>
        {canFocus && (
          <button onClick={() => onFocus!(node.id)}
                  style={{ background: t("ui.panel.elevated"), color: t("ui.text"),
                    border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "3px 8px",
                    fontSize: em(11), cursor: "pointer" }}>
            ⌖ Focus / expand here
          </button>
        )}
      </div>
      <div style={{ height: 6 }} />
      {val && <ValueHeader val={val} />}
      <Row k="label" v={node.label} />
      <Row k="address" v={node.address} />
      <Row k="transaction" v={node.tx_hash} />
      <Row k="chain" v={node.chain} />
      <Row k="finality" v={node.finality_status} />

      {/* Universal rename + annotate (+ edit/delete notes) — for any address / transaction node. */}
      {target && (
        <RenameAndNotes target={target} currentLabel={node.label ?? ""} isCustom={!!node.custom_label}
          annotations={annotations} onSaveLabel={onSaveLabel} onAddAnnotation={onAddAnnotation}
          onEditAnnotation={onEditAnnotation} onDeleteAnnotation={onDeleteAnnotation} />
      )}

      {node.kind === "address" && (
        hasClaims ? (
          <>
            {entities.length > 0 && (
              <>
                <SectionHeader title="Entity" />
                {entities.map((e, i) => (
                  <ClaimCard key={i} source={e.source}>
                    {/* entity name in the graph's entity-teal so the panel matches the canvas encoding */}
                    <div style={{ fontWeight: 600, color: t("node.entity.label.color") }}>{e.name}</div>
                    <div style={{ color: t("ui.text.secondary"), fontSize: em(12) }}>
                      {[e.entity_type, e.method, pct(e.confidence)].filter(Boolean).join(" · ")}
                    </div>
                    {e.flags && (
                      <div style={{ color: t("node.flag.coinjoin.ring"), fontSize: em(11), marginTop: 2 }}>⚑ {e.flags}</div>
                    )}
                  </ClaimCard>
                ))}
              </>
            )}

            {attrSources.length > 0 && (
              <>
                <SectionHeader title={`Attribution · ${attrSources.length} source(s)`} />
                {attrSources.map(([source, list]) =>
                  list.map((a) => (
                    <ClaimCard key={a.id} source={source}>
                      <div style={{ fontWeight: 600 }}>{a.label}</div>
                      <div style={{ color: t("ui.text.secondary"), fontSize: em(12) }}>
                        {[a.category, pct(a.confidence)].filter(Boolean).join(" · ")}
                      </div>
                      {a.note && (
                        <div style={{ color: t("ui.muted"), fontSize: em(11), marginTop: 2, wordBreak: "break-all" }}>
                          {a.note}
                        </div>
                      )}
                    </ClaimCard>
                  ))
                )}
              </>
            )}

            {riskSources.length > 0 && (
              <>
                <SectionHeader title={`Risk · ${riskSources.length} source(s)`} />
                {riskSources.map(([source, list]) =>
                  list.map((r) => (
                    <ClaimCard key={r.id} source={source}>
                      <div style={{ fontWeight: 600, textTransform: "capitalize" }}>
                        {r.category}
                        {r.score != null && (
                          <span style={{ fontWeight: 400, color: t("ui.text.secondary") }}>
                            {" "}— {r.score}{r.score_scale ? ` (${r.score_scale})` : ""}
                          </span>
                        )}
                      </div>
                      {r.rationale && (
                        <div style={{ color: t("ui.text.secondary"), fontSize: em(12), marginTop: 2 }}>{r.rationale}</div>
                      )}
                    </ClaimCard>
                  ))
                )}
              </>
            )}

            <p style={{ color: t("ui.muted"), fontSize: em(11), marginTop: 14, lineHeight: 1.4 }}>
              Sources shown side-by-side, each with its own provenance — never merged into one
              score or label (Invariant&nbsp;#4).
            </p>
          </>
        ) : (
          <p style={{ color: t("ui.muted"), fontSize: em(12), marginTop: 14 }}>
            No sourced claims for this address.
          </p>
        )
      )}

      {summary && <RankedList summary={summary} onFocus={onFocus} />}
    </aside>
  );
}
