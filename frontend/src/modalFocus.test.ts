import { describe, expect, it } from "vitest";
import { isEscape, tabWrapTarget } from "./modalFocus";

describe("isEscape", () => {
  it("matches the standard Escape key value", () => {
    expect(isEscape("Escape")).toBe(true);
  });
  it("matches the legacy Esc value defensively", () => {
    expect(isEscape("Esc")).toBe(true);
  });
  it("ignores every other key", () => {
    for (const k of ["Enter", "Tab", "Space", "a", "Escapee", "", "escape"]) {
      expect(isEscape(k)).toBe(false);
    }
  });
});

describe("tabWrapTarget", () => {
  it("returns null when there is nothing to trap", () => {
    expect(tabWrapTarget(0, -1, false)).toBeNull();
    expect(tabWrapTarget(0, 0, true)).toBeNull();
  });

  it("keeps focus on the single control in both directions", () => {
    expect(tabWrapTarget(1, 0, false)).toBe(0);
    expect(tabWrapTarget(1, 0, true)).toBe(0);
    expect(tabWrapTarget(1, -1, false)).toBe(0);
  });

  it("wraps Tab on the last control to the first", () => {
    expect(tabWrapTarget(3, 2, false)).toBe(0);
  });

  it("wraps Shift+Tab on the first control to the last", () => {
    expect(tabWrapTarget(3, 0, true)).toBe(2);
  });

  it("lets the browser handle interior moves (returns null)", () => {
    expect(tabWrapTarget(3, 1, false)).toBeNull();
    expect(tabWrapTarget(3, 1, true)).toBeNull();
    expect(tabWrapTarget(4, 2, false)).toBeNull();
  });

  it("sends an off-list Tab to the first and off-list Shift+Tab to the last", () => {
    expect(tabWrapTarget(3, -1, false)).toBe(0);
    expect(tabWrapTarget(3, -1, true)).toBe(2);
  });
});
