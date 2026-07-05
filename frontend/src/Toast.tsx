import { t } from "./theme/theme";

// A floating, dismissible message that sits ABOVE the graph without displacing it (P29/UX-08). Used for a
// transient ACTION failure (a failed intel / valuation / trace / label / export call) so a momentary
// error never blanks the investigation canvas — the full-screen error state is reserved for a genuine
// VIEW-LOAD failure (see chooseMainView in ./appView). Not a modal: it gates nothing and traps no focus.
// Colors resolve through the token catalog (no hardcoded hex).

export default function Toast({ message, kind = "error", onDismiss }: {
  message: string; kind?: "error" | "info"; onDismiss: () => void;
}) {
  const accent = kind === "error" ? t("ui.error") : t("node.entity.ring");
  return (
    <div role={kind === "error" ? "alert" : "status"} aria-live={kind === "error" ? "assertive" : "polite"}
         style={{ position: "fixed", left: "50%", bottom: 22, transform: "translateX(-50%)", zIndex: 70,
                  maxWidth: "min(680px, 92vw)", display: "flex", alignItems: "flex-start", gap: 10,
                  padding: "10px 14px", borderRadius: 8, background: t("ui.panel.bg"),
                  border: `1px solid ${accent}`, boxShadow: "0 6px 22px rgba(0,0,0,0.4)",
                  color: t("ui.text"), fontSize: 13 }}>
      <b style={{ color: accent, whiteSpace: "nowrap" }}>{kind === "error" ? "Action failed:" : "Note:"}</b>
      <span style={{ overflowWrap: "anywhere" }}>{message}</span>
      <button onClick={onDismiss} aria-label="Dismiss"
              style={{ marginLeft: 6, background: t("ui.panel.elevated"), color: t("ui.text"),
                       border: `1px solid ${t("ui.border")}`, borderRadius: 4, padding: "2px 9px",
                       fontSize: 12, cursor: "pointer", lineHeight: 1.2 }}>✕</button>
    </div>
  );
}
