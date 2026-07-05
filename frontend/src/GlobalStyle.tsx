// GlobalStyle.tsx — the app's first global stylesheet (P31/UX-12). Before this there was none: App writes
// the theme catalog as CSS custom properties on :root, but no rule ever CONSUMED them globally. This mounts a
// single injected <style> (the same SSR/test-safe pattern Progress uses for its keyframes — no CSS-file or
// build wiring) that gives every focusable control a visible KEYBOARD focus ring via :focus-visible, so the
// ring shows for keyboard / AT users but not on a mouse click. The color resolves through the same --bih-*
// custom property applyThemeVars() writes on :root, so the ring follows the active theme; currentColor is the
// defensive fallback for the boot instant before the vars are applied (no hardcoded hex).
export default function GlobalStyle() {
  return (
    <style>{
      ":focus-visible{outline:2px solid var(--bih-node-seed-marker, currentColor);outline-offset:2px}" +
      ":focus:not(:focus-visible){outline:none}"
    }</style>
  );
}
