// Pure decision for the app's GLOBAL keyboard shortcuts (P32/UX-07). DOM-free so it unit-tests in node: the
// App-level keydown listener supplies `inEditable` (the event target is an input / textarea / select /
// contenteditable) plus the modifier state, and this decides the action. Keeping it pure makes the two
// safety rules provable in a test rather than merely asserted in the handler: a shortcut NEVER fires while
// typing in a field, and a modifier chord (Ctrl/Cmd/Alt) is always left to the browser/OS.

export type ShortcutAction = "add-address" | "report" | "findings" | "focus-search";

export function shortcutForKey(key: string, ctx: {
  inEditable: boolean; ctrl: boolean; meta: boolean; alt: boolean;
}): ShortcutAction | null {
  if (ctx.inEditable || ctx.ctrl || ctx.meta || ctx.alt) return null;
  switch (key.toLowerCase()) {
    case "a": return "add-address";
    case "r": return "report";
    case "f": return "findings";
    case "/": return "focus-search";
    default: return null;
  }
}
