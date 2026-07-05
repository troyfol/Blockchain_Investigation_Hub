import { t } from "./theme/theme";

// A slim progress affordance over the job poller (P29/UX-08). Determinate when a total is known
// (valuation "M of N" -> a filled bar with a percent); indeterminate otherwise (ingest page-fetching has
// no total up front -> a sliding segment). It only READS job state; it never drives the job. Colors
// resolve through the token catalog (no hardcoded hex). The indeterminate keyframes ride in a rendered
// <style> (no document mutation, SSR/test-safe); DOM-free consumers (the node unit tests) never import it.

export default function Progress({ value, max, height = 6, label }: {
  value?: number; max?: number; height?: number; label?: string;
}) {
  const determinate = typeof max === "number" && max > 0;
  const frac = determinate ? Math.max(0, Math.min(1, (value ?? 0) / (max as number))) : 0;
  const track: React.CSSProperties = {
    position: "relative", flex: 1, minWidth: 90, height, borderRadius: height, overflow: "hidden",
    background: t("ui.panel.elevated"), border: `1px solid ${t("ui.border")}`,
  };
  const fill: React.CSSProperties = determinate
    ? { position: "absolute", left: 0, top: 0, bottom: 0, width: `${frac * 100}%`,
        background: t("node.seed.marker"), transition: "width 0.3s ease" }
    : { position: "absolute", top: 0, bottom: 0, left: "-35%", width: "35%",
        background: t("node.seed.marker"), animation: "bih-progress-indet 1.1s ease-in-out infinite" };
  return (
    <div role="progressbar" aria-label={label ?? "progress"}
         aria-valuenow={determinate ? Math.round(frac * 100) : undefined}
         aria-valuemin={determinate ? 0 : undefined} aria-valuemax={determinate ? 100 : undefined}
         style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 110 }}>
      {!determinate && <style>{"@keyframes bih-progress-indet{0%{left:-35%}100%{left:100%}}"}</style>}
      <div style={track}><div style={fill} /></div>
      {determinate && (
        <span style={{ fontSize: 11, color: t("ui.muted"), minWidth: 34, textAlign: "right" }}>
          {Math.round(frac * 100)}%
        </span>
      )}
    </div>
  );
}
