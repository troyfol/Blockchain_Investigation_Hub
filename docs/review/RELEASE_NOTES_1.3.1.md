# Blockchain Investigation Hub — v1.3.1

**Release date:** 2026-07-05 · Previous release: v1.3.0.

A small patch release on top of v1.3.0.

## New — delete a trace

You can now remove a trace you built (a mistaken or experimental path) directly from the trace list. In
keeping with the tool's tamper-evident design it is a **soft delete**: the trace disappears from the case —
the trace list, the graph overlay, the court report, and the activity timeline — but the record is preserved
(append-only), never destroyed, so the audit trail stays intact. This mirrors the existing per-edge/per-link
retraction, one level up at the whole trace. A confirm (with an optional reason) guards the action; the
`trace-retraction-append-only` invariant audit covers it, so a deleted trace can never be silently
un-deleted or rewritten.

## Fixed — runs cleanly without an OS keyring backend

On a machine with no keyring backend (for example a headless / Secret-Service-less Linux box), the app no
longer errors when a request checks whether the optional paid sources are keyed. It degrades to "no key
stored" and surfaces the missing backend loudly as before, instead of returning a 500. (This was a genuine
robustness gap; it also turned the project's continuous-integration suite green.)

## Upgrade notes (existing case DBs)

- **Schema advances 10 → 11** — one additive forward-only migration (`0015`, the whole-trace retraction
  table) applies automatically the first time v1.3.1 opens an existing case. No fact rows are rewritten.
- **Existing cases and `.casefile` bundles from v1.3.0 open unchanged** — they import, verify, and
  forward-migrate. The invariant audits tolerate the new table being absent on an older-schema case DB.
- **No new runtime dependencies.**

## Distribution

The Windows installer (`BIH-Setup-1.3.1.exe`, ~31 MB) ships **unsigned** — on download/first run Windows
SmartScreen shows an "unknown publisher" prompt; click **More info → Run anyway**. The uninstaller preserves
your cases under `%APPDATA%`. See the README "Distribution" note for the one-line signing enablement.
