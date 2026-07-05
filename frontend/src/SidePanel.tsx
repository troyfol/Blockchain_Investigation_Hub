import { useEffect, useRef, useState } from "react";
import type { GraphEdge, GraphNode, NodeValue } from "./Graph";
import { valuationState } from "./nodeValue";
import { sourceColor, t } from "./theme/theme";
import { fetchSourceQuery, type SourceQueryProvenance } from "./provenance";
import {
  addTraceAnnotation,
  btcLinkCandidates,
  listTraceAnnotations,
  listTraceBridgeLinks,
  traceNextHops,
  type BridgeEndpoint,
  type BtcLinkCandidate,
  type BtcNextHop,
  type EvmNextHop,
  type TraceAnnotation,
  type TraceBridgeLink,
} from "./traces";

type BridgePins = { src: BridgeEndpoint | null; dst: BridgeEndpoint | null };

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
  source_query_id?: string | null;
};
type Risk = {
  id: string; category: string; score: number | null;
  score_scale: string | null; rationale: string | null; source: string;
  source_query_id?: string | null;
};
type EntityMembership = {
  name: string; entity_type: string | null; origin: string;
  source: string; method: string; confidence: number | null; flags: string | null;
  source_query_id?: string | null;
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
          // P38/UX-13 — edit/delete are ≥24px tap targets (inline-flex box + padding), not bare 10px glyphs.
          <span style={{ display: "inline-flex", alignItems: "center", whiteSpace: "nowrap", fontSize: em(10) }}>
            {onEdit && <span style={{ display: "inline-flex", alignItems: "center", minHeight: 24,
              padding: "0 6px", cursor: "pointer", color: t("node.label.color") }}
              onClick={() => { setText(a.content); setEditing(true); }}>edit</span>}
            {onEdit && onDelete && <span style={{ color: t("ui.muted") }}>·</span>}
            {onDelete && <span style={{ display: "inline-flex", alignItems: "center", minHeight: 24,
              padding: "0 6px", cursor: "pointer", color: t("ui.error") }}
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
  // P30/UX-09 — a node with native movement but NO USD on either side is ingested-but-UNVALUED (value-at-time
  // pending or unavailable), which is NOT $0 (unpriced ≠ zero). State that honestly instead of silently
  // omitting the USD; a valued node keeps the value-at-time note unchanged.
  const unvalued = valuationState(val) === "unvalued";
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
      {unvalued ? (
        <div style={{ color: t("ui.muted"), fontSize: em(10), marginTop: 3 }}>
          No USD valuation yet (value-at-time pending or unavailable).
        </div>
      ) : (
        <div style={{ color: t("ui.muted"), fontSize: em(10), marginTop: 3 }}>USD = value-at-time (DeFiLlama).</div>
      )}
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
function LabelEditor({ initial, placeholder, onSave, openToken }: {
  initial: string; placeholder: string; onSave: (value: string) => void; openToken?: number;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(initial);
  // P32 — a double-click on the node bumps `openToken`, opening this editor (reset to the current label) so
  // rename starts inline with no native prompt. We react only to a CHANGE in openToken (a ref seeded with the
  // mount value), so merely SELECTING a node — which mounts the editor while openToken is already non-zero
  // from an earlier rename — never auto-opens it; only a fresh double-click does.
  const seenToken = useRef(openToken);
  useEffect(() => {
    if (openToken === seenToken.current) return;
    seenToken.current = openToken;
    if (openToken) { setValue(initial); setEditing(true); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openToken]);
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
                         onEditAnnotation, onDeleteAnnotation, openRenameToken }: {
  target: Target; currentLabel: string; isCustom: boolean; annotations: Annotation[];
  onSaveLabel?: (ttype: string, tid: string, label: string) => void;
  onAddAnnotation?: (ttype: string, tid: string, content: string) => void;
  onEditAnnotation?: (id: string, content: string) => void | Promise<unknown>;
  onDeleteAnnotation?: (id: string) => void;
  openRenameToken?: number;
}) {
  return (
    <>
      {onSaveLabel && (
        <div style={{ marginBottom: 8 }}>
          <LabelEditor initial={currentLabel} placeholder="custom label" openToken={openRenameToken}
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

// The contextual action the current selection offers a trace: add an EVM transfer FACT, or FIFO-apportion
// a Bitcoin transaction into `basis='fifo'` links (a labeled convention, never ground-truth flow).
type TraceAction = { type: "transfer" | "fifo"; id: string; label: string };

type ManualLink = { transaction_id: string; source_output_id: string; dest_output_id: string; note: string | null };

const traceField = (): React.CSSProperties => ({
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 3, padding: "3px 6px", fontSize: em(12),
});

// Durable investigator NOTES on a trace/path (UX-06). A `trace` is a valid annotation target, so this
// reuses the generic notes endpoint. Self-contained: it fetches + posts the trace's OWN notes, so it never
// disturbs the selected node/flow's annotation state. These notes also appear in the report's notes appendix.
function TraceNotes({ traceId }: { traceId: string }) {
  const [notes, setNotes] = useState<TraceAnnotation[]>([]);
  const [text, setText] = useState("");
  useEffect(() => {
    if (!traceId) { setNotes([]); return; }
    let live = true;
    listTraceAnnotations(traceId).then((d) => { if (live) setNotes(d.annotations ?? []); }).catch(() => { /* leave empty */ });
    return () => { live = false; };
  }, [traceId]);
  const add = () => {
    const v = text.trim();
    if (!v) return;
    addTraceAnnotation(traceId, v).then((d) => { setNotes(d.annotations ?? []); setText(""); }).catch(() => { /* ignore */ });
  };
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ color: t("ui.muted"), fontSize: em(11), marginBottom: 3 }}>Notes on this trace</div>
      {notes.map((n) => (
        <div key={n.id} style={{ fontSize: em(11), color: t("ui.text"),
          borderLeft: `3px solid ${t("node.annotation.ring")}`, padding: "3px 6px", marginBottom: 3 }}>
          {n.content}
        </div>
      ))}
      <div style={{ display: "flex", gap: 4 }}>
        <input value={text} placeholder="note on this trace…" onChange={(e) => setText(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} style={{ ...traceField(), flex: 1, minWidth: 0 }} />
        <button onClick={add} style={{ ...traceField(), cursor: "pointer" }}>Note</button>
      </div>
    </div>
  );
}

// A manual `basis='investigator'` BTC link WITHIN one transaction (UX-06). The pickers are the tx's legal
// endpoints ONLY (sources = prev-outputs it spends, dests = its own outputs), so the UI cannot even express
// a cross-tx edge; the backend re-validates on write (Invariant #5 — the link is a within-tx claim).
function ManualLinkForm({ txId, traceId, onAddManualLink }: {
  txId: string; traceId: string; onAddManualLink: (traceId: string, link: ManualLink) => void;
}) {
  const [sources, setSources] = useState<BtcLinkCandidate[]>([]);
  const [dests, setDests] = useState<BtcLinkCandidate[]>([]);
  const [src, setSrc] = useState("");
  const [dst, setDst] = useState("");
  const [note, setNote] = useState("");
  useEffect(() => {
    let live = true;
    btcLinkCandidates(txId).then((d) => {
      if (!live) return;
      setSources(d.sources); setDests(d.dests);
      setSrc(d.sources[0]?.id ?? ""); setDst(d.dests[0]?.id ?? "");
    }).catch(() => { if (live) { setSources([]); setDests([]); } });
    return () => { live = false; };
  }, [txId]);
  const canLink = Boolean(traceId && src && dst);
  const link = () => {
    if (!canLink) return;
    onAddManualLink(traceId, { transaction_id: txId, source_output_id: src, dest_output_id: dst, note: note.trim() || null });
    setNote("");
  };
  if (sources.length === 0 || dests.length === 0) {
    return (
      <p style={{ color: t("ui.muted"), fontSize: em(11), marginTop: 8 }}>
        No in-DB spent-output → output pair to link within this tx.
      </p>
    );
  }
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ color: t("ui.muted"), fontSize: em(11), marginBottom: 3 }}>
        Manual link (within this tx — an investigator claim, not flow)
      </div>
      <select value={src} onChange={(e) => setSrc(e.target.value)} style={{ ...traceField(), width: "100%", cursor: "pointer" }}>
        {sources.map((s) => <option key={s.id} value={s.id}>from: {s.label}</option>)}
      </select>
      <select value={dst} onChange={(e) => setDst(e.target.value)} style={{ ...traceField(), width: "100%", marginTop: 3, cursor: "pointer" }}>
        {dests.map((d) => <option key={d.id} value={d.id}>to: {d.label}</option>)}
      </select>
      <div style={{ display: "flex", gap: 4, marginTop: 3 }}>
        <input value={note} placeholder="note (optional)…" onChange={(e) => setNote(e.target.value)}
               style={{ ...traceField(), flex: 1, minWidth: 0 }} />
        <button onClick={link} disabled={!canLink}
                style={{ ...traceField(), cursor: canLink ? "pointer" : "default", opacity: canLink ? 1 : 0.5 }}>
          Link
        </button>
      </div>
    </div>
  );
}

// Guided expansion (FN-16): the trace's PROPOSED next hops — outgoing facts already in the case that leave
// its frontier (terminal nodes). The tool PROPOSES; the investigator adds (nothing is auto-added). EVM = a
// one-click transfer add; BTC = focus the tx that spends the terminal output so the within-tx link form
// opens. `refreshKey` re-fetches after a hop is added (the frontier advances). Self-contained fetch.
function NextHops({ traceId, refreshKey, onAddTransferToTrace, onFocus }: {
  traceId: string; refreshKey: number;
  onAddTransferToTrace: (traceId: string, transferId: string) => void;
  onFocus?: (nodeId: string) => void;
}) {
  const [evm, setEvm] = useState<EvmNextHop[]>([]);
  const [btc, setBtc] = useState<BtcNextHop[]>([]);
  useEffect(() => {
    if (!traceId) { setEvm([]); setBtc([]); return; }
    let live = true;
    traceNextHops(traceId).then((d) => { if (live) { setEvm(d.evm ?? []); setBtc(d.btc ?? []); } })
      .catch(() => { if (live) { setEvm([]); setBtc([]); } });
    return () => { live = false; };
  }, [traceId, refreshKey]);
  if (evm.length === 0 && btc.length === 0) return null;
  const short = (a: string | null): string => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "?");
  const rowStyle: React.CSSProperties = { display: "flex", gap: 4, alignItems: "center", marginBottom: 3 };
  const textStyle: React.CSSProperties = { flex: 1, minWidth: 0, fontSize: em(11), color: t("ui.text"),
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ color: t("ui.muted"), fontSize: em(11), marginBottom: 3 }}>
        Next hops (proposed — you choose; nothing is auto-added)
      </div>
      {evm.map((h) => (
        <div key={h.transfer_id} style={rowStyle}>
          <span style={textStyle}>{short(h.from)} → {short(h.to)} · {h.amount} {h.asset ?? ""}</span>
          <button onClick={() => onAddTransferToTrace(traceId, h.transfer_id)} style={{ ...traceField(), cursor: "pointer" }}>Add</button>
        </div>
      ))}
      {btc.map((h) => (
        <div key={h.source_output_id} style={rowStyle}>
          <span style={textStyle}>{h.source_label} → spent by {short(h.tx_hash)}</span>
          <button onClick={() => onFocus?.(`tx:${h.spending_tx_id}`)} disabled={!onFocus}
                  style={{ ...traceField(), cursor: onFocus ? "pointer" : "default", opacity: onFocus ? 1 : 0.5 }}>Link…</button>
        </div>
      ))}
    </div>
  );
}

// FN-17: assert a CROSS-CHAIN bridge crossing — pin one selected flow as the source (chain A outflow) and
// another as the dest (chain B inflow), then create the link (a `basis='investigator'` CLAIM, never a
// fabricated fact). The backend requires the two movements exist and cross chains.
function BridgeControls({ traceId, currentMovement, pins, onPin, onClear, onCreate, onCreated }: {
  traceId: string; currentMovement: BridgeEndpoint | null; pins: BridgePins;
  onPin: (side: "src" | "dst", ep: BridgeEndpoint) => void;
  onClear: () => void;
  onCreate: (traceId: string) => Promise<unknown>;
  onCreated: () => void;
}) {
  const { src, dst } = pins;
  const crossChain = Boolean(src && dst && src.chain !== dst.chain);
  const canCreate = Boolean(traceId && crossChain);
  const btn = (extra: React.CSSProperties = {}): React.CSSProperties => ({ ...traceField(), cursor: "pointer", ...extra });
  const slot = (label: string, ep: BridgeEndpoint | null) => (
    <div style={{ fontSize: em(11), color: ep ? t("ui.text") : t("ui.muted"),
      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
      {label}: {ep ? `${ep.chain} · ${ep.label}` : "—"}
    </div>
  );
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ color: t("ui.muted"), fontSize: em(11), marginBottom: 3 }}>
        Cross-chain bridge link (investigator claim)
      </div>
      {currentMovement ? (
        <div style={{ display: "flex", gap: 4, marginBottom: 3 }}>
          <button onClick={() => onPin("src", currentMovement)} style={btn({ flex: 1 })}>Pin as source</button>
          <button onClick={() => onPin("dst", currentMovement)} style={btn({ flex: 1 })}>Pin as dest</button>
        </div>
      ) : (
        <p style={{ color: t("ui.muted"), fontSize: em(11) }}>Select a flow (EVM transfer / BTC output), then pin it.</p>
      )}
      {slot("from", src)}
      {slot("to", dst)}
      {src && dst && !crossChain && (
        <div style={{ fontSize: em(11), color: t("ui.muted") }}>both movements are on the same chain — a bridge must cross chains</div>
      )}
      <div style={{ display: "flex", gap: 4, marginTop: 3 }}>
        <button onClick={() => { void onCreate(traceId).then(onCreated); }} disabled={!canCreate}
                style={{ ...btn({ flex: 1 }), cursor: canCreate ? "pointer" : "default", opacity: canCreate ? 1 : 0.5 }}>
          Create bridge link
        </button>
        <button onClick={onClear} disabled={!src && !dst}
                style={{ ...btn(), cursor: (src || dst) ? "pointer" : "default", opacity: (src || dst) ? 1 : 0.5 }}>Clear</button>
      </div>
    </div>
  );
}

// The trace's existing cross-chain bridge links (labeled investigator claims). Self-contained fetch;
// `refreshKey` re-fetches after a new link is created.
function BridgeLinksList({ traceId, refreshKey }: { traceId: string; refreshKey: number }) {
  const [links, setLinks] = useState<TraceBridgeLink[]>([]);
  useEffect(() => {
    if (!traceId) { setLinks([]); return; }
    let live = true;
    listTraceBridgeLinks(traceId).then((d) => { if (live) setLinks(d.bridge_links ?? []); })
      .catch(() => { if (live) setLinks([]); });
    return () => { live = false; };
  }, [traceId, refreshKey]);
  if (links.length === 0) return null;
  return (
    <div style={{ marginTop: 6 }}>
      {links.map((l) => (
        <div key={l.id} style={{ fontSize: em(11), color: t("ui.text"),
          borderLeft: `3px solid ${t("edge.trace.fifo.line")}`, padding: "3px 6px", marginBottom: 3 }}>
          {l.src_chain ?? "?"} → {l.dst_chain ?? "?"} · <span style={{ color: t("ui.muted") }}>{l.basis}</span>
          {l.note ? ` · ${l.note}` : ""}
        </div>
      ))}
    </div>
  );
}

// Build + populate traces (LOG-04). Create a named trace; add the selected EVM transfer, FIFO-apportion the
// selected Bitcoin tx, or add a manual within-tx BTC link (UX-06). Guided expansion (FN-16) proposes next
// hops from the frontier; a cross-chain bridge link (FN-17) is a labeled investigator claim. Any trace can
// carry durable notes. The writers are insert-once (re-running is safe).
function TraceBuilder({ traces, action, onCreateTrace, onAddTransferToTrace, onFifoTx, onAddManualLink,
                        onFocus, currentMovement = null, bridgePins, onPinBridge, onClearBridge,
                        onCreateBridge }: {
  traces: TraceInfo[]; action: TraceAction | null;
  onCreateTrace: (name: string) => void;
  onAddTransferToTrace: (traceId: string, transferId: string) => void;
  onFifoTx: (traceId: string, txId: string) => void;
  onAddManualLink?: (traceId: string, link: ManualLink) => void;
  onFocus?: (nodeId: string) => void;
  currentMovement?: BridgeEndpoint | null;
  bridgePins?: BridgePins;
  onPinBridge?: (side: "src" | "dst", ep: BridgeEndpoint) => void;
  onClearBridge?: () => void;
  onCreateBridge?: (traceId: string) => Promise<unknown>;
}) {
  const [bridgeRefresh, setBridgeRefresh] = useState(0);
  const [name, setName] = useState("");
  const [traceId, setTraceId] = useState(traces[0]?.id ?? "");
  useEffect(() => {  // keep the picked trace valid as the list changes
    if (!traces.some((tr) => tr.id === traceId)) setTraceId(traces[0]?.id ?? "");
  }, [traces, traceId]);
  const selected = traces.find((tr) => tr.id === traceId);
  const hopsKey = selected ? selected.transfer_count + selected.btc_link_count : 0;  // advances on each add
  const create = () => { const v = name.trim(); if (v) { onCreateTrace(v); setName(""); } };
  const apply = () => {
    if (!traceId || !action) return;
    if (action.type === "transfer") onAddTransferToTrace(traceId, action.id);
    else onFifoTx(traceId, action.id);
  };
  return (
    <>
      <SectionHeader title="Build a trace" />
      <div style={{ display: "flex", gap: 4 }}>
        <input value={name} placeholder="new trace name…" onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") create(); }} style={{ ...traceField(), flex: 1, minWidth: 0 }} />
        <button onClick={create} style={{ ...traceField(), cursor: "pointer" }}>+ New</button>
      </div>
      {traces.length === 0 ? (
        <p style={{ color: t("ui.muted"), fontSize: em(11), marginTop: 6 }}>
          Create a trace above to add flows, FIFO/manual BTC links, and notes to it.
        </p>
      ) : (
        <div style={{ marginTop: 6 }}>
          <select value={traceId} onChange={(e) => setTraceId(e.target.value)}
                  style={{ ...traceField(), width: "100%", cursor: "pointer" }}>
            {traces.map((tr) => <option key={tr.id} value={tr.id}>{tr.name}</option>)}
          </select>
          {action && (
            <div style={{ marginTop: 6 }}>
              <div style={{ color: t("ui.muted"), fontSize: em(11), marginBottom: 3 }}>
                {action.type === "fifo" ? "FIFO-apportion " : "Add "}{action.label}
              </div>
              <button onClick={apply} style={{ ...traceField(), cursor: "pointer" }}>
                {action.type === "fifo" ? "Apportion" : "Add"}
              </button>
            </div>
          )}
          {action?.type === "fifo" && onAddManualLink && (
            <ManualLinkForm txId={action.id} traceId={traceId} onAddManualLink={onAddManualLink} />
          )}
          {!action && (
            <p style={{ color: t("ui.muted"), fontSize: em(11), marginTop: 6 }}>
              Select an EVM transfer flow, or a Bitcoin tx node, to add it — or FIFO/manual-link a BTC tx.
            </p>
          )}
          <NextHops traceId={traceId} refreshKey={hopsKey} onAddTransferToTrace={onAddTransferToTrace}
                    onFocus={onFocus} />
          {bridgePins && onPinBridge && onClearBridge && onCreateBridge && (
            <BridgeControls traceId={traceId} currentMovement={currentMovement} pins={bridgePins}
              onPin={onPinBridge} onClear={onClearBridge} onCreate={onCreateBridge}
              onCreated={() => setBridgeRefresh((x) => x + 1)} />
          )}
          <BridgeLinksList traceId={traceId} refreshKey={bridgeRefresh} />
          <TraceNotes traceId={traceId} />
        </div>
      )}
    </>
  );
}

// v1.3.1 — soft-delete (retract) a WHOLE trace: an inline confirm + optional reason (no native prompt, P32).
// The trace is WITHDRAWN (append-only) — its row + edges persist for the audit trail; it just leaves the case
// view (list, graph overlay, report, activity). A default reason keeps the click-through fast.
function TraceDeleteControl({ onDelete }: { onDelete: (reason: string) => void }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const field: React.CSSProperties = { background: t("ui.panel.bg"), color: t("ui.text"),
    border: `1px solid ${t("ui.border")}`, borderRadius: 3, padding: "2px 8px", fontSize: em(11) };
  const danger: React.CSSProperties = { ...field, color: t("ui.error"), borderColor: t("ui.error"), cursor: "pointer" };
  if (!open) {
    return <button style={danger} onClick={() => { setReason(""); setOpen(true); }}>🗑 Delete trace</button>;
  }
  const confirm = () => { onDelete(reason.trim() || "removed by investigator"); setOpen(false); };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ color: t("ui.muted"), fontSize: em(10), lineHeight: 1.4 }}>
        Delete this trace? It is withdrawn from the case — append-only, so the record is kept, not destroyed.
      </div>
      <input autoFocus value={reason} placeholder="reason (optional)…"
             onChange={(e) => setReason(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") confirm(); else if (e.key === "Escape") setOpen(false); }}
             style={{ ...field, width: "100%", boxSizing: "border-box" }} />
      <div style={{ display: "flex", gap: 4 }}>
        <button style={danger} onClick={confirm}>Confirm delete</button>
        <button style={{ ...field, color: t("ui.muted"), cursor: "pointer" }} onClick={() => setOpen(false)}>Cancel</button>
      </div>
    </div>
  );
}

function TraceList({ traces, onSaveTraceLabel, onRetractTrace }: {
  traces: TraceInfo[]; onSaveTraceLabel: (traceId: string, label: string) => void;
  onRetractTrace?: (traceId: string, reason: string) => void;
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
          {onRetractTrace && (
            <div style={{ marginTop: 4 }}>
              <TraceDeleteControl onDelete={(reason) => onRetractTrace(tr.id, reason)} />
            </div>
          )}
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

// A one-click "provenance" affordance on a claim card: lazily fetch + reveal the exact `source_query`
// behind the claim (FN-01). Today provenance stops at the source NAME; this exposes the query itself —
// endpoint, params/bounds, retrieval time, raw-response hash — without leaving the panel (Invariant #3).
function ProvenanceLink({ sqid }: { sqid?: string | null }) {
  const [open, setOpen] = useState(false);
  const [sq, setSq] = useState<SourceQueryProvenance | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!sqid) return null;
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !sq && !err) {
      fetchSourceQuery(sqid).then(setSq).catch((e) => setErr(String(e?.message ?? e)));
    }
  };
  const kv = (k: string, v?: string | null) =>
    v ? (
      <div style={{ display: "flex", gap: 6 }}>
        <span style={{ color: t("ui.muted"), minWidth: 62, flexShrink: 0 }}>{k}</span>
        <span style={{ wordBreak: "break-all" }}>{v}</span>
      </div>
    ) : null;
  return (
    <div style={{ marginTop: 3 }}>
      <span onClick={toggle} style={{ cursor: "pointer", color: t("node.label.color"), fontSize: em(10) }}>
        {open ? "▾ provenance" : "▸ provenance"}
      </span>
      {open && (
        <div style={{ marginTop: 3, padding: "4px 6px", background: t("ui.panel.bg"),
                      border: `1px solid ${t("ui.border")}`, borderRadius: 3, fontSize: em(10) }}>
          {err ? <div style={{ color: t("ui.error") }}>{err}</div>
            : !sq ? <div style={{ color: t("ui.muted") }}>loading…</div>
            : (
              <>
                {kv("source", `${sq.connector} · ${sq.capability}`)}
                {kv("endpoint", sq.endpoint)}
                {kv("params", sq.params ? JSON.stringify(sq.params) : null)}
                {kv("retrieved", sq.requested_at)}
                {kv("status", sq.status)}
                {kv("summary", sq.result_summary)}
                {kv("hash", sq.raw_response_hash)}
              </>
            )}
        </div>
      )}
    </div>
  );
}

function pct(c: number | null): string | null {
  return c == null ? null : `${Math.round(c * 100)}% confidence`;
}

// FN-03 — a movement's valuations, one card per source, side-by-side (never averaged — Invariant #4).
// Lazily fetched for a CONTESTED flow; each card carries the raw-response provenance drill-through (FN-01).
type ValuationClaim = {
  source: string; currency?: string; unit_price?: string; value?: string;
  price_timestamp?: string; confidence?: number | null; retrieved_at?: string;
  source_query_id?: string | null;
};
type MovementValuations = {
  subject_id: string; contested: boolean;
  valuations_by_source: Record<string, ValuationClaim[]>;
};

// A Decimal-TEXT value (e.g. "2000.000…") → a readable USD string, reusing the numeric fmtUsd above.
function usdFromText(v?: string): string {
  if (!v) return "—";
  const n = Number(v);
  return isFinite(n) ? (fmtUsd(n) ?? "—") : `$${v}`;
}

function ValuationBreakdown({ subjectId }: { subjectId: string }) {
  const [data, setData] = useState<MovementValuations | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setData(null); setErr(null);
    fetch(`/api/movement/${encodeURIComponent(subjectId)}/valuations`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setData).catch((e) => setErr(String(e?.message ?? e)));
  }, [subjectId]);
  const sources = data ? Object.keys(data.valuations_by_source) : [];
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ color: t("ui.muted"), fontSize: em(11), textTransform: "uppercase" }}>
        value-at-time{sources.length ? ` · ${sources.length} sources` : ""}
      </div>
      {err && <div style={{ color: t("ui.error"), fontSize: em(11) }}>{err}</div>}
      {!data && !err && <div style={{ color: t("ui.muted"), fontSize: em(11) }}>loading…</div>}
      {data && sources.map((src) =>
        data.valuations_by_source[src].map((v, i) => (
          <ClaimCard key={`${src}:${i}`} source={src}>
            <div style={{ fontSize: em(13) }}>
              {usdFromText(v.value)} <span style={{ color: t("ui.muted"), fontSize: em(10) }}>{v.currency ?? "USD"}</span>
            </div>
            <div style={{ color: t("ui.muted"), fontSize: em(11) }}>
              {v.unit_price ? `unit ${usdFromText(v.unit_price)}` : null}
              {v.confidence != null ? `${v.unit_price ? " · " : ""}${Math.round(v.confidence * 100)}% confidence` : null}
            </div>
            {v.retrieved_at && <div style={{ color: t("ui.muted"), fontSize: em(10) }}>retrieved {v.retrieved_at}</div>}
            <ProvenanceLink sqid={v.source_query_id} />
          </ClaimCard>
        )),
      )}
      <div style={{ color: t("ui.muted"), fontSize: em(10) }}>
        Each source's price is shown side-by-side and never averaged (Invariant #4).
      </div>
    </div>
  );
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
      {edge.value_contested && edge.ann_id ? (
        // FN-03 — a contested movement shows EVERY source's valuation side-by-side (never one collapsed
        // number); the flag still fires, but the detail is now rendered here rather than deferred.
        <ValuationBreakdown subjectId={edge.ann_id} />
      ) : edge.value_usd_label ? (
        <div style={{ marginBottom: 8 }}>
          <div style={{ color: t("ui.muted"), fontSize: em(11), textTransform: "uppercase" }}>value-at-time</div>
          <div>{edge.value_usd_label}</div>
          <div style={{ color: t("ui.muted"), fontSize: em(10) }}>USD = value-at-time (DeFiLlama).</div>
        </div>
      ) : null}
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
                                    onSaveLabel, onSaveTraceLabel, onRetractTrace, onCreateTrace, onAddTransferToTrace,
                                    onFifoTx, onAddManualLink, bridgePins, onPinBridge, onClearBridge,
                                    onCreateBridge, onFocus, fontScale = 1, renameToken }: {
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
  onRetractTrace?: (traceId: string, reason: string) => void;
  onCreateTrace?: (name: string) => void;
  onAddTransferToTrace?: (traceId: string, transferId: string) => void;
  onFifoTx?: (traceId: string, txId: string) => void;
  onAddManualLink?: (traceId: string, link: ManualLink) => void;
  bridgePins?: BridgePins;
  onPinBridge?: (side: "src" | "dst", ep: BridgeEndpoint) => void;
  onClearBridge?: () => void;
  onCreateBridge?: (traceId: string) => Promise<unknown>;
  onFocus?: (nodeId: string) => void;
  fontScale?: number;
  renameToken?: number;
}) {
  // One root font-size drives the whole panel; the UI pref scales it (children use em — feature 6).
  const panelStyle: React.CSSProperties = { ...PANEL, fontSize: BASE_PX * fontScale };

  // FN-17: the currently-selected FLOW as a pinnable bridge endpoint (a value movement + its chain). Only a
  // flow edge with a durable movement target (transfer / tx_output) can be one side of a bridge crossing.
  // The chain is derived from the edge's endpoint node (display + the same-chain hint only — the backend
  // re-validates the crossing from the actual movements).
  const currentMovement: BridgeEndpoint | null =
    edge && edge.ann_type && edge.ann_id
      ? { subject_type: edge.ann_type, subject_id: edge.ann_id,
          chain: nodesById[edge.source]?.chain ?? nodesById[edge.target]?.chain ?? "?",
          label: edge.value_label ? `${edge.ann_type} (${edge.value_label})` : edge.ann_type }
      : null;

  // What the current selection offers a trace: an EVM transfer FACT (a flow with a durable transfer
  // target), or a Bitcoin transaction to FIFO-apportion. Nothing else is addable.
  const traceAction: TraceAction | null =
    edge && edge.ann_type === "transfer" && edge.ann_id
      ? { type: "transfer", id: edge.ann_id, label: `this transfer${edge.value_label ? ` (${edge.value_label})` : ""}` }
      : node && node.kind === "transaction" && node.chain === "bitcoin"
        ? { type: "fifo", id: node.id.replace(/^tx:/, ""), label: `this BTC tx${node.label ? ` (${node.label})` : ""}` }
        : null;

  // The Traces area (rename existing paths + build/populate new ones), shown regardless of selection.
  const traceArea = (
    <>
      {traces.length > 0 && onSaveTraceLabel && <TraceList traces={traces} onSaveTraceLabel={onSaveTraceLabel} onRetractTrace={onRetractTrace} />}
      {onCreateTrace && onAddTransferToTrace && onFifoTx && (
        <TraceBuilder traces={traces} action={traceAction} onCreateTrace={onCreateTrace}
          onAddTransferToTrace={onAddTransferToTrace} onFifoTx={onFifoTx} onAddManualLink={onAddManualLink}
          currentMovement={currentMovement} bridgePins={bridgePins} onPinBridge={onPinBridge}
          onClearBridge={onClearBridge} onCreateBridge={onCreateBridge} onFocus={onFocus} />
      )}
    </>
  );

  // A tapped FLOW (edge) takes over the panel: its facts + the universal rename / annotate controls.
  if (edge && !node) {
    return (
      <aside style={panelStyle}>
        {traceArea}
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
        {traceArea}
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
      {traceArea}
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
          onEditAnnotation={onEditAnnotation} onDeleteAnnotation={onDeleteAnnotation}
          openRenameToken={renameToken} />
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
                    <ProvenanceLink sqid={e.source_query_id} />
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
                      <ProvenanceLink sqid={a.source_query_id} />
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
                      <ProvenanceLink sqid={r.source_query_id} />
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
