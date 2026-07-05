import { useMemo } from "react";
import {
  CATALOG, type CanvasPreset, clearCustomOverride, currentColor, getActivePreset,
  hasOverride, isLockedPreset, resetCustomOverrides, setActivePreset, setCustomOverride, t,
} from "./theme/theme";
import Modal from "./Modal";

// Customize-colors drawer (P6) — edits the CUSTOM preset's overrides only. Right-docked so the canvas
// stays visible and updates LIVE as you pick (every edit bumps the theme store -> App re-renders -> the
// graph restyles). dark/light are LOCKED: the editor is disabled with a "switch to Custom" message.
// Only canvas COLOR tokens are editable (Report/Report-pills are print-light report-only; Sizing tokens
// aren't colors), so the report/exhibit are never affected by Custom edits.

const EXCLUDED_CATEGORIES = new Set(["Report", "Report pills", "Sizing"]);
const isColorToken = (value: string) => /^#[0-9a-fA-F]{6}$/.test(value);

type Props = { onClose: () => void };

const panel: React.CSSProperties = {
  position: "fixed", top: 0, right: 0, bottom: 0, width: 380, zIndex: 70,
  background: t("ui.panel.bg"), borderLeft: `1px solid ${t("ui.border")}`,
  display: "flex", flexDirection: "column", boxShadow: "-6px 0 24px rgba(0,0,0,0.35)",
};
const btn: React.CSSProperties = {
  background: t("ui.panel.elevated"), color: t("ui.text"), border: `1px solid ${t("ui.border")}`,
  borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer", whiteSpace: "nowrap",
};
const hint: React.CSSProperties = { fontSize: 11, color: t("ui.muted"), margin: 0 };

export default function ThemeCustomize({ onClose }: Props) {
  const active = getActivePreset();
  const locked = isLockedPreset(active);

  // Canvas color tokens grouped by category (stable order from the catalog).
  const groups = useMemo(() => {
    const byCat = new Map<string, typeof CATALOG>();
    for (const tk of CATALOG) {
      if (EXCLUDED_CATEGORIES.has(tk.category) || !isColorToken(tk.value)) continue;
      const arr = byCat.get(tk.category) ?? [];
      arr.push(tk);
      byCat.set(tk.category, arr);
    }
    return [...byCat.entries()];
  }, []);

  const edit = (id: string, value: string) => {
    if (locked) return;
    try { setCustomOverride(id, value); } catch { /* locked race — ignored (editor is disabled) */ }
  };

  // Right-docked drawer with a SUBTLE scrim backdrop (P33): click-out + Esc dismiss (via Modal/P31), and the
  // scrim stays light (canvas.background at ~25% alpha) so the canvas remains VISIBLE for live color preview
  // while you pick. role="dialog" + aria-modal + aria-labelledby all come from Modal.
  return (
    <Modal onClose={onClose} containerStyle={panel} labelledBy="theme-title"
           backdropStyle={{ position: "fixed", inset: 0, zIndex: 69, background: `${t("canvas.background")}40` }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px",
                    borderBottom: `1px solid ${t("ui.border")}` }}>
        <strong id="theme-title" style={{ color: t("ui.text"), fontSize: 14 }}>Customize colors</strong>
        <span style={{ ...hint, color: t("node.seed.marker") }}>· Custom preset</span>
        <button style={{ ...btn, marginLeft: "auto" }} onClick={onClose} aria-label="Close customize">✕</button>
      </div>

      {locked ? (
        <div style={{ margin: 14, padding: 12, border: `1px solid ${t("ui.warning")}`, borderRadius: 8,
                      display: "flex", flexDirection: "column", gap: 8 }}>
          <b style={{ color: t("ui.warning"), fontSize: 13 }}>
            The "{active}" preset is locked
          </b>
          <span style={{ ...hint, color: t("ui.text.secondary") }}>
            Dark and Light are read-only modern themes. Switch to the Custom preset to edit colors.
          </span>
          <button style={{ ...btn, alignSelf: "flex-start", borderColor: t("node.seed.marker") }}
                  onClick={() => setActivePreset("custom" as CanvasPreset)}>Switch to Custom</button>
        </div>
      ) : (
        <div style={{ padding: "10px 14px", display: "flex", alignItems: "center", gap: 8,
                      borderBottom: `1px solid ${t("ui.border")}` }}>
          <span style={hint}>Pick a color — the canvas updates live.</span>
          <button style={{ ...btn, marginLeft: "auto" }} onClick={() => resetCustomOverrides()}
                  title="Revert every token to the Neo-Tokyo defaults">Reset Custom to defaults</button>
        </div>
      )}

      <div style={{ flex: 1, overflow: "auto", padding: "8px 14px 24px" }}>
        {groups.map(([category, tokens]) => (
          <section key={category} style={{ marginBottom: 14 }}>
            <h3 style={{ fontSize: 11, fontWeight: 600, letterSpacing: 0.4, textTransform: "uppercase",
                         color: t("ui.text.secondary"), margin: "8px 0 6px" }}>{category}</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {tokens.map((tk) => {
                const value = currentColor(tk.id);
                const overridden = hasOverride(tk.id) && !locked;
                return (
                  <div key={tk.id} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input type="color" value={value} disabled={locked} aria-label={tk.label}
                           onChange={(e) => edit(tk.id, e.target.value)}
                           style={{ width: 32, height: 26, padding: 0, border: `1px solid ${t("ui.border")}`,
                                    borderRadius: 4, background: "transparent",
                                    cursor: locked ? "not-allowed" : "pointer" }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12, color: t("ui.text"), overflow: "hidden",
                                    textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tk.label}</div>
                      <div style={{ fontSize: 10, color: t("ui.muted"), fontFamily: "monospace" }}>{value}</div>
                    </div>
                    {overridden && (
                      <button style={{ ...btn, padding: "2px 7px", fontSize: 11 }} title="Reset this token"
                              onClick={() => clearCustomOverride(tk.id)}>↺</button>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </Modal>
  );
}
