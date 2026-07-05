// Pure key-decision helpers for the shared Modal a11y shell (P31/UX-12). DOM-free on purpose: vitest runs
// in node here (no jsdom), so the focus-trap/Esc DECISIONS are unit-tested in isolation while the DOM wiring
// (querying focusables, moving focus) lives in Modal.tsx and is exercised by the manual/headless a11y pass.

// The controls a focus trap must cycle through, in DOM order. Excludes disabled + hidden inputs and anything
// explicitly removed from the tab order (tabindex="-1", e.g. the dialog container itself).
export const FOCUSABLE_SELECTOR = [
  "a[href]",
  "area[href]",
  'input:not([disabled]):not([type="hidden"])',
  "select:not([disabled])",
  "textarea:not([disabled])",
  "button:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

/** True for the Escape keydown (accepts the legacy IE "Esc" value defensively). */
export function isEscape(key: string): boolean {
  return key === "Escape" || key === "Esc";
}

/**
 * The focus-trap wrap target when Tab is pressed inside a dialog. Given the number of focusable controls,
 * the index of the currently-focused one (-1 when focus is on none of them — e.g. the dialog container), and
 * whether Shift is held, returns the index to focus — or null to let the browser handle it (an interior move
 * that stays inside the dialog needs no interception).
 *   Shift+Tab on the first (or off-list) control -> wrap to the last.
 *   Tab on the last (or off-list) control        -> wrap to the first.
 *   any interior control                          -> null (default browser behavior keeps focus inside).
 * A single control returns itself in both directions (keeps focus trapped on the lone control).
 */
export function tabWrapTarget(count: number, activeIndex: number, shift: boolean): number | null {
  if (count <= 0) return null;
  if (count === 1) return 0;
  if (shift) return activeIndex <= 0 ? count - 1 : null;
  return activeIndex === count - 1 || activeIndex < 0 ? 0 : null;
}
