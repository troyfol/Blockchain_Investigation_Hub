import { t } from "./theme/theme";
import Modal from "./Modal";

// Per-denomination filter panel (P8.7 #1): each native denomination/asset present in the current view
// gets its OWN min (drop below) + fold (collapse below) threshold, in that asset's native units — so
// folding the long tail inside one pool (5,000,000 cDAI) never touches another (100,000 DAI). View-state;
// a change re-fetches /api/view with the per-asset thresholds. No hardcoded hex (catalog tokens).

export type DenomFilters = Record<string, { min?: number; fold?: number }>;

type Props = {
  denominations: string[];
  filters: DenomFilters;
  onChange: (next: DenomFilters) => void;
  onClose: () => void;
};

const card: React.CSSProperties = {
  background: t("ui.panel.bg"), border: `1px solid ${t("ui.border")}`, borderRadius: 10, padding: 16,
  display: "flex", flexDirection: "column", gap: 10, width: "100%", maxWidth: 480,
};
const field: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "5px 7px", fontSize: 12, width: 90,
};
const btn: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "5px 11px", fontSize: 12, cursor: "pointer",
};

export default function DenomPanel({ denominations, filters, onChange, onClose }: Props) {
  const set = (asset: string, key: "min" | "fold", raw: string) => {
    const v = Number(raw);
    const next: DenomFilters = { ...filters, [asset]: { ...(filters[asset] || {}) } };
    if (!raw || !Number.isFinite(v) || v <= 0) delete next[asset][key];
    else next[asset][key] = v;
    if (!next[asset].min && !next[asset].fold) delete next[asset];
    onChange(next);
  };

  const backdrop: React.CSSProperties = {
    position: "fixed", inset: 0, zIndex: 83, background: t("ui.app.bg"),
    display: "flex", alignItems: "flex-start", justifyContent: "center", overflow: "auto", padding: 32,
  };

  return (
    <Modal onClose={onClose} backdropStyle={backdrop} containerStyle={card} labelledBy="denom-title">
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <h2 id="denom-title" style={{ margin: 0, fontSize: 16, color: t("ui.text") }}>Per-denomination filters</h2>
          <button style={{ ...btn, marginLeft: "auto" }} onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p style={{ fontSize: 11, color: t("ui.muted"), margin: 0 }}>
          Each asset's min (drop below) + fold (collapse below) is in its OWN native units — folding one
          pool never touches another. Leave blank to ignore.
        </p>
        {denominations.length === 0 ? (
          <p style={{ fontSize: 12, color: t("ui.muted") }}>No denominations in the current view.</p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", gap: 8, fontSize: 11, color: t("ui.text.secondary") }}>
              <span style={{ width: 90 }}>asset</span><span style={{ width: 90 }}>min</span><span>fold &lt;</span>
            </div>
            {denominations.map((asset) => (
              <div key={asset} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <code style={{ width: 90, fontSize: 12, color: t("ui.text") }}>{asset}</code>
                <input type="number" min={0} placeholder="—" style={field}
                       value={filters[asset]?.min ?? ""} onChange={(e) => set(asset, "min", e.target.value)} />
                <input type="number" min={0} placeholder="—" style={field}
                       value={filters[asset]?.fold ?? ""} onChange={(e) => set(asset, "fold", e.target.value)} />
              </div>
            ))}
          </div>
        )}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button style={btn} onClick={() => onChange({})}>Clear all</button>
          <button style={btn} onClick={onClose}>Done</button>
        </div>
    </Modal>
  );
}
