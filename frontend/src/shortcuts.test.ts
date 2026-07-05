import { describe, expect, it } from "vitest";
import { shortcutForKey } from "./shortcuts";

const free = { inEditable: false, ctrl: false, meta: false, alt: false };

describe("shortcutForKey", () => {
  it("maps the documented keys to actions", () => {
    expect(shortcutForKey("a", free)).toBe("add-address");
    expect(shortcutForKey("r", free)).toBe("report");
    expect(shortcutForKey("f", free)).toBe("findings");
    expect(shortcutForKey("/", free)).toBe("focus-search");
  });

  it("is case-insensitive on the letter shortcuts", () => {
    expect(shortcutForKey("A", free)).toBe("add-address");
    expect(shortcutForKey("R", free)).toBe("report");
    expect(shortcutForKey("F", free)).toBe("findings");
  });

  it("returns null for keys that aren't shortcuts", () => {
    for (const k of ["b", "z", "Enter", "Escape", "1", " ", "Tab"]) {
      expect(shortcutForKey(k, free)).toBeNull();
    }
  });

  it("never fires while typing in an editable field", () => {
    for (const k of ["a", "r", "f", "/"]) {
      expect(shortcutForKey(k, { ...free, inEditable: true })).toBeNull();
    }
  });

  it("leaves modifier chords to the browser/OS", () => {
    expect(shortcutForKey("a", { ...free, ctrl: true })).toBeNull();
    expect(shortcutForKey("r", { ...free, meta: true })).toBeNull();
    expect(shortcutForKey("f", { ...free, alt: true })).toBeNull();
    expect(shortcutForKey("/", { ...free, ctrl: true })).toBeNull();
  });
});
