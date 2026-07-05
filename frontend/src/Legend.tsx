import { useEffect, useState } from "react";
import { legendItems, t, type LegendItem } from "./theme/theme";
import type { GraphData } from "./Graph";

// A legend swatch whose shape hints at the element it labels (node / edge / halo / ring). Moved here from
// App's header in P34 together with the legend itself.
function Swatch({ item }: { item: LegendItem }) {
  const base: React.CSSProperties = { display: "inline-block", width: 12, height: 12, marginRight: 6,
    verticalAlign: "middle", flex: "none" };
  if (item.marker === "edge")
    return <span style={{ ...base, height: 0, borderTop: `3px solid ${item.color}`, marginBottom: 2 }} />;
  if (item.marker === "halo")
    return <span style={{ ...base, borderRadius: "50%", background: item.color, opacity: 0.5,
      boxShadow: `0 0 0 2px ${item.color}` }} />;
  if (item.marker === "ring")
    return <span style={{ ...base, borderRadius: "50%", border: `2.5px solid ${item.color}` }} />;
  return <span style={{ ...base, borderRadius: "50%", background: item.color }} />;
}

// P34/UX-01 — the graph legend as a COLLAPSIBLE on-canvas overlay (it used to be crammed into the header
// row). It stays CONTEXT-AWARE: legendItems(data) returns only the element types actually present in THIS
// view, so the legend shrinks/grows with the graph and renders nothing when the view has no keyed elements.
// Absolutely positioned bottom-left over Graph's relative container — clear of the meta banner (top) and
// the SidePanel (right). The open/collapsed choice persists (a display pref, like the font steppers), and
// the panel is slightly translucent so the canvas stays legible behind it.
export default function Legend({ data }: { data: GraphData }) {
  const [open, setOpen] = useState<boolean>(() => localStorage.getItem("bih.legendOpen") !== "0");
  useEffect(() => { localStorage.setItem("bih.legendOpen", open ? "1" : "0"); }, [open]);

  const items = legendItems(data);
  if (items.length === 0) return null;   // nothing keyed in this view -> no legend at all (context-aware)

  const shell: React.CSSProperties = {
    position: "absolute", left: 10, bottom: 10, zIndex: 8, maxWidth: 240,
    background: `${t("ui.panel.bg")}f2`, border: `1px solid ${t("ui.border")}`, borderRadius: 8,
    boxShadow: "0 2px 12px rgba(0,0,0,0.28)", fontSize: 12, color: t("ui.muted"), overflow: "hidden",
  };
  const toggle: React.CSSProperties = {
    display: "flex", alignItems: "center", gap: 6, width: "100%", cursor: "pointer",
    background: "transparent", border: 0, color: t("ui.text.secondary"), font: "inherit",
    padding: "5px 9px", textAlign: "left",
  };

  return (
    <div style={shell}>
      <button style={toggle} onClick={() => setOpen((v) => !v)} aria-expanded={open}
              title={open ? "Collapse legend" : "Expand legend"}>
        <span style={{ fontSize: 10 }}>{open ? "▾" : "▸"}</span>
        <span style={{ fontWeight: 600 }}>Legend</span>
        <span style={{ marginLeft: "auto", color: t("ui.muted") }}>{items.length}</span>
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "0 10px 9px" }}>
          {items.map((item) => (
            <span key={item.label} style={{ whiteSpace: "nowrap", display: "flex", alignItems: "center" }}>
              <Swatch item={item} />{item.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
