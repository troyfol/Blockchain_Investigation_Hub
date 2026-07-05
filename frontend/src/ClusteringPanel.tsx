import { useCallback, useEffect, useState } from "react";
import {
  type ClusterRun, type ClusterSummary, type HeuristicInfo,
  applyClustering, clusteringSummary, listHeuristics, previewClustering, undoClustering,
} from "./clustering";
import { t } from "./theme/theme";
import Modal from "./Modal";

// Clustering panel (P8.8). Toggle/parameterise each heuristic, PREVIEW what it would merge, APPLY it (a
// reversible run), and UNDO any run — plus the Leiden community VISUAL overlay (structure, not ownership).
// Co-spend (Meiklejohn) is always on; every opt-in heuristic defaults off. Per cluster the summary shows
// which heuristic formed it + confidence, side-by-side (Inv #4). No hardcoded hex (catalog tokens).

type Props = {
  onChanged: () => void;                 // re-fetch the graph + summary after apply/undo
  onClose: () => void;
  community: boolean;                    // the Leiden community overlay (view-state, never persisted)
  onToggleCommunity: (on: boolean) => void;
  communityNote?: string | null;
};

const card: React.CSSProperties = {
  background: t("ui.panel.bg"), border: `1px solid ${t("ui.border")}`, borderRadius: 10, padding: 16,
  display: "flex", flexDirection: "column", gap: 12, width: "100%", maxWidth: 620,
};
const btn: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "4px 10px", fontSize: 12, cursor: "pointer", whiteSpace: "nowrap",
};
const num: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "3px 6px", fontSize: 12, width: 64,
};
const box: React.CSSProperties = {
  background: t("ui.panel.elevated"), border: `1px solid ${t("ui.border")}`, borderRadius: 8, padding: 10,
  display: "flex", flexDirection: "column", gap: 6,
};

// Default parameters per heuristic (faithful library defaults; the panel exposes the key knob).
function defaultParams(name: string, requireAgree: number): Record<string, unknown> {
  if (name === "btc-change") return { require_agree: requireAgree };
  return {};
}

export default function ClusteringPanel({ onChanged, onClose, community, onToggleCommunity, communityNote }: Props) {
  const [heuristics, setHeuristics] = useState<HeuristicInfo[]>([]);
  const [summary, setSummary] = useState<ClusterSummary>({});
  const [runs, setRuns] = useState<ClusterRun[]>([]);
  const [requireAgree, setRequireAgree] = useState(2);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    clusteringSummary().then(({ summary, runs }) => { setSummary(summary); setRuns(runs); });
  }, []);
  useEffect(() => { listHeuristics().then(setHeuristics); refresh(); }, [refresh]);

  const preview = useCallback((name: string) => {
    setBusy(name); setNote(null);
    previewClustering(name, defaultParams(name, requireAgree))
      .then((r) => {
        const p = r.preview as { n_clusters?: number; note?: string };
        setNote(`${name}: would form ${p.n_clusters ?? 0} cluster(s)${p.note ? ` — ${p.note}` : ""}`);
      })
      .catch((e) => setNote(`preview failed: ${String(e)}`))
      .finally(() => setBusy(null));
  }, [requireAgree]);

  const apply = useCallback((name: string) => {
    setBusy(name); setNote(null);
    applyClustering(name, defaultParams(name, requireAgree))
      .then((r) => {
        const n = (r.clusters as number) ?? 0;
        setNote(`${name}: applied — ${n} cluster(s), ${(r.memberships_created as number) ?? 0} memberships`
          + (r.note ? ` (${r.note})` : ""));
        refresh(); onChanged();
      })
      .catch((e) => setNote(`apply failed: ${String(e)}`))
      .finally(() => setBusy(null));
  }, [requireAgree, refresh, onChanged]);

  const undo = useCallback((sqid: string) => {
    setBusy(sqid);
    undoClustering(sqid).then((r) => {
      setNote(`undone — ${(r.retracted as number) ?? 0} membership(s) retracted`);
      refresh(); onChanged();
    }).catch((e) => setNote(`undo failed: ${String(e)}`)).finally(() => setBusy(null));
  }, [refresh, onChanged]);

  const backdrop: React.CSSProperties = {
    position: "fixed", inset: 0, zIndex: 84, background: t("ui.app.bg"),
    display: "flex", alignItems: "flex-start", justifyContent: "center", overflow: "auto", padding: 32,
  };

  return (
    <Modal onClose={onClose} backdropStyle={backdrop} containerStyle={card} labelledBy="clustering-title">
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <h2 id="clustering-title" style={{ margin: 0, fontSize: 16, color: t("ui.text") }}>Clustering</h2>
          <button style={{ ...btn, marginLeft: "auto" }} onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p style={{ fontSize: 11, color: t("ui.muted"), margin: 0 }}>
          Each heuristic is a separate, confidence-tagged, reversible cluster claim — shown side-by-side,
          never merged. Co-spend (Meiklejohn 2013) is always on; everything below defaults off. CoinJoin is
          never used to link addresses.
        </p>

        {/* require-N-agree knob for the BlockSci change heuristics — defaults to ≥2 (conservative) */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: t("ui.text.secondary"), flexWrap: "wrap" }}>
          <span>BTC change: require ≥</span>
          <input type="number" min={1} max={6} value={requireAgree} style={num}
                 onChange={(e) => setRequireAgree(Math.max(1, Math.min(6, Number(e.target.value) || 1)))} />
          <span>heuristics to agree (default 2 — BlockSci: never a single heuristic alone)</span>
          {requireAgree < 2 && (
            <span style={{ color: t("ui.warning"), fontSize: 11 }}>⚠ ≥1 is permissive — false-positive-prone</span>
          )}
        </div>

        {/* the opt-in heuristics */}
        {heuristics.filter((h) => !h.always_on && !h.visual_only).map((h) => (
          <div key={h.name} style={box}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, color: t("ui.text") }}>{h.label}</span>
              <span style={{ fontSize: 10, color: t("ui.muted") }}>[{h.chain}]</span>
              <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                <button style={btn} disabled={busy === h.name} onClick={() => preview(h.name)}>Preview</button>
                <button style={{ ...btn, borderColor: t("node.seed.marker") }} disabled={busy === h.name}
                        onClick={() => apply(h.name)}>Apply</button>
              </div>
            </div>
          </div>
        ))}

        {/* Leiden community VISUAL overlay (never persisted, never an ownership claim) */}
        <div style={box}>
          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: t("ui.text") }}>
            <input type="checkbox" checked={community} onChange={(e) => onToggleCommunity(e.target.checked)} />
            Leiden community overlay (Traag 2019) — <span style={{ color: t("group.community.border") }}>visual structure, not ownership</span>
          </label>
          {communityNote && <span style={{ fontSize: 10, color: t("ui.muted") }}>{communityNote}</span>}
        </div>

        {note && <p style={{ ...box, color: t("node.annotation.ring"), fontSize: 12 }}>{note}</p>}

        {/* applied runs — each undoable as a unit */}
        {runs.length > 0 && (
          <div style={{ ...box }}>
            <span style={{ fontSize: 11, color: t("ui.text.secondary") }}>Applied runs</span>
            {runs.map((r) => (
              <div key={r.source_query_id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
                <code style={{ color: t("ui.text"), fontSize: 11 }}>{r.connector}</code>
                <span style={{ color: t("ui.muted") }}>{r.active}/{r.memberships} active</span>
                <button style={{ ...btn, marginLeft: "auto", padding: "2px 8px" }}
                        disabled={busy === r.source_query_id || r.active === 0}
                        onClick={() => undo(r.source_query_id)}>Undo</button>
              </div>
            ))}
          </div>
        )}

        {/* per-heuristic cluster summary (which heuristic formed each cluster + confidence) */}
        {Object.keys(summary).length > 0 && (
          <div style={{ ...box }}>
            <span style={{ fontSize: 11, color: t("ui.text.secondary") }}>Clusters (side-by-side per heuristic)</span>
            {Object.entries(summary).map(([source, s]) => (
              <div key={source} style={{ fontSize: 11, color: t("ui.text") }}>
                <code>{source}</code>: {s.n_clusters} cluster(s), {s.n_addresses} addr ·{" "}
                {s.clusters.slice(0, 4).map((c) => `${c.size}@${c.confidence_min ?? "?"}–${c.confidence_max ?? "?"}`).join(", ")}
                {s.clusters.length > 4 ? " …" : ""}
              </div>
            ))}
          </div>
        )}
    </Modal>
  );
}
