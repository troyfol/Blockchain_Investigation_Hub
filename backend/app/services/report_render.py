"""Render the self-contained report HTML to a PDF using an OS-available browser engine (P3).

WHY this exists: the original report path drove a **bundled** Chromium via Playwright (a ~150 MB
download the packaged app would otherwise have to ship/install). P3 drops that from the packaged path
and prints the PDF with whatever Chromium-based engine the OS **already** ships — Microsoft Edge /
WebView2 on Windows (the very engine the pywebview launcher uses), or a system Chrome/Chromium on
macOS/Linux — through the documented headless ``--print-to-pdf`` CLI. No bundled browser, no Playwright
required. Playwright remains an OPTIONAL dev/CI fallback when it happens to be installed.

The PDF is a **rendered artifact only**. The report's ``content_hash`` is frozen over the canonical
self-contained HTML (``services/reporting.py``), NOT the PDF bytes, because PDF output is not
byte-deterministic across engines/versions. So a missing engine never blocks producing (or verifying)
a report — it only means the convenience PDF isn't rendered this run.

CONFIRM-FIRST (CLAUDE.md §6). A true *in-process* pywebview "print the loaded page to PDF" would be the
ideal packaged path (one engine, already on screen). It is **not exposed by pywebview 6.2.1**: ``webview
.Window`` has no ``get_pdf``/``print``/``save_as_pdf`` method (verified against the installed package);
only the macOS backend has an *interactive* print dialog (``cocoa.print_webview``), and the Windows
WebView2 (``CoreWebView2.PrintToPdfAsync``) / WKWebView (``createPDF``) / WebKitGTK print-to-pdf engine
APIs are not surfaced. Per the directive we DO NOT reach into private engine objects to invent the call.
``TODO: confirm`` — when pywebview exposes a programmatic page->PDF API, add it here as the preferred
backend (it would render the same HTML the launcher already shows, with zero extra process).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class NoRendererError(RuntimeError):
    """No PDF-capable engine is available (no Edge/Chrome on PATH, no Playwright). Callers treat this
    as 'HTML produced, PDF skipped' — never as a failure to produce the report."""


class DenseRenderError(NoRendererError):
    """An engine IS present but the (dense) report graph never became ready within the render budget, so
    the engine exited 0 without writing a PDF (P8.7.1 #3). A NoRendererError subclass — still 'HTML
    produced, PDF skipped' — but carries an ACTIONABLE message (narrow the view / retry), distinct from
    'no engine installed'. ``render_pdf`` retries once with a larger budget before raising this."""


# Standard install locations for the OS Chromium engine, by platform. Edge is preferred on Windows
# because it is the WebView2 engine the desktop launcher already depends on (so the report renders with
# the exact engine the app uses).
_WIN_EDGE = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
_WIN_CHROME = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
_MAC_CHROME = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
_NIX_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "microsoft-edge", "microsoft-edge-stable"]


def _first_existing(paths: list[str]) -> "str | None":
    return next((p for p in paths if Path(p).exists()), None)


def find_engines() -> "list[tuple[str, str]]":
    """Locate every OS Chromium engine that could print, in preference order. Returns
    ``[(name, exe_path), ...]`` (possibly empty).

    ``BIH_REPORT_RENDERER`` overrides the choice: ``edge`` / ``chrome`` / ``chromium`` pin an engine
    (the list then holds at most that one), ``playwright`` forces the optional fallback (handled in
    :func:`render_pdf`), ``none`` forces the no-renderer path (so a test can deterministically
    exercise the clean skip), ``auto`` (default) lists Edge first (the WebView2 engine the desktop
    launcher already uses) then Chrome/Chromium — ``render_pdf`` tries them ALL, so one broken
    engine can no longer mask a working one right next to it (review finding BASE-01)."""
    raw = os.environ.get("BIH_REPORT_RENDERER", "auto").strip()
    choice = raw.lower()
    if choice in ("none", "playwright"):
        return []

    # An explicit path wins (operator override for an unusual install). Checked/returned with the
    # operator's ORIGINAL casing — paths are case-sensitive on Linux/macOS, so lowercasing first
    # would silently break a valid pin.
    if raw and Path(raw).exists():
        return [("custom", raw)]

    edge = (_first_existing(_WIN_EDGE) if sys.platform == "win32"
            else (_first_existing(_MAC_CHROME[2:]) if sys.platform == "darwin"
                  else (shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable"))))
    chrome = (_first_existing(_WIN_CHROME) if sys.platform == "win32"
              else (_first_existing(_MAC_CHROME) if sys.platform == "darwin"
                    else next((shutil.which(n) for n in _NIX_NAMES if shutil.which(n)), None)))

    if choice in ("edge",):
        return [("edge", edge)] if edge else []
    if choice in ("chrome", "chromium"):
        return [("chrome", chrome)] if chrome else []
    engines: "list[tuple[str, str]]" = []
    if edge:
        engines.append(("edge", edge))
    # On Linux/macOS an Edge-only machine can resolve BOTH probes to the same binary (Edge is in
    # the generic Chromium name lists) — never list one executable twice.
    if chrome and chrome != edge:
        engines.append(("chrome", chrome))
    return engines


def find_engine() -> "tuple[str, str] | None":
    """The preferred OS engine — first of :func:`find_engines`, or ``None`` (availability checks)."""
    engines = find_engines()
    return engines[0] if engines else None


def renderer_available() -> bool:
    """True if a PDF can be rendered this run — mirrors :func:`render_pdf`'s decision tree exactly
    (including ``BIH_REPORT_RENDERER`` pins), so an availability probe can never say yes when the
    render path itself would refuse (e.g. a pinned-but-absent engine does NOT fall back to
    Playwright, and neither does this check)."""
    choice = os.environ.get("BIH_REPORT_RENDERER", "auto").strip().lower()
    if choice == "none":
        return False
    if choice == "playwright":
        return _playwright_available()
    if find_engine() is not None:
        return True
    pinned = choice not in ("auto", "")
    return (not pinned) and _playwright_available()


def _playwright_available() -> bool:
    if os.environ.get("BIH_SKIP_PLAYWRIGHT_BROWSERS"):
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


def _render_with_engine(exe: str, html_path: Path, pdf_path: Path, *, budget_ms: int = 12000) -> None:
    """Headless print-to-PDF via the OS Chromium engine. ``--virtual-time-budget`` advances the page's
    timers so the Cytoscape ``cose`` layout settles (the report sets ``window.__CY_READY__`` when done)
    before the page is captured. ``--no-pdf-header-footer`` stays set so the browser prints NO default
    header/footer of its own — leaving the report's court-formal running footer (case id + "Page N of M",
    a CSS ``@page`` margin-box footer the report HTML carries, FN-12) as the ONLY footer, and keeping the
    local ``file://`` path the browser's default footer would show from leaking into the exhibit. (Blink
    renders CSS ``@page`` margin-box ``counter(page)``/``counter(pages)`` on this CLI path — verified this
    phase.) A FRESH ``--user-data-dir`` is mandatory: without it, a headless print invocation silently
    no-ops when the user already has Edge/Chrome open on the default profile (a well-known gotcha).

    On a DENSE graph the synchronous cose layout can exceed the budget, leaving the engine to exit 0 but
    write NO PDF — we raise :class:`DenseRenderError` (a NoRendererError subclass, so it still degrades to
    HTML-only) so the caller can RETRY with a bigger budget + surface an actionable reason (P8.7.1 #3)."""
    import tempfile

    profile = Path(tempfile.mkdtemp(prefix="bih-render-"))
    # --print-to-pdf must be ABSOLUTE: the engine resolves a relative path against its OWN working
    # directory (not ours), so a relative target silently lands elsewhere and we'd see "no PDF produced".
    out = pdf_path.resolve()
    cmd = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        f"--user-data-dir={profile}",  # isolate from any running browser so print-to-pdf doesn't no-op
        "--run-all-compositor-stages-before-draw",
        f"--virtual-time-budget={budget_ms}",
        "--no-pdf-header-footer",
        f"--print-to-pdf={out}",
        html_path.resolve().as_uri(),
    ]
    try:
        # A generous timeout; a hung engine must not wedge report generation.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        # Surface a hang as the check-next-path signal (NoRendererError), never as a raw
        # TimeoutExpired that would skip the remaining engines and crash report generation.
        raise NoRendererError(f"engine {exe!r} hung (no exit within 120 s) — killed, no PDF produced")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not (out.exists() and out.stat().st_size > 0):
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        # exit-0-but-no-PDF == the page never became ready within the budget (dense graph) — a DISTINCT,
        # retryable condition, not a missing/broken engine.
        if proc.returncode == 0:
            raise DenseRenderError(
                f"the report graph did not render within {budget_ms} ms — the view may be too dense. "
                f"HTML written; narrow the view (focus/hops, fold dust/spam) or retry. {detail}".strip())
        raise NoRendererError(f"engine {exe!r} did not produce a PDF (exit {proc.returncode}): {detail}")


def _render_with_playwright(html_path: Path, pdf_path: Path) -> None:
    """Optional dev/CI fallback: the prior headless-Chromium-via-Playwright path. Kept so a machine
    with Playwright installed still renders, but it is no longer REQUIRED to produce a report."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1240, "height": 1000})
            page.goto(html_path.resolve().as_uri())
            page.wait_for_function("window.__CY_READY__ === true", timeout=30000)
            # P14: honor the report's CSS `@page` size + margins (preferCSSPageSize) so the Playwright
            # fallback paginates identically to the default Edge/Chrome `--print-to-pdf` path, instead of
            # overriding them with hardcoded margins here.
            page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


def render_pdf(html_path, pdf_path) -> str:
    """Render ``html_path`` to ``pdf_path``, trying EVERY available render path before giving up
    (review finding BASE-01: a broken preferred engine must not mask a working one): each discovered
    OS engine at the standard budget, then one bigger-budget retry for dense-style failures
    (P8.7.1 #3), then the optional Playwright fallback. An explicitly pinned engine
    (``BIH_REPORT_RENDERER=edge|chrome|chromium|<path>``) is honored strictly — its failure
    surfaces; nothing silently unpins it. Returns the name of the engine that rendered.

    Raises :class:`NoRendererError` when nothing can render (:class:`DenseRenderError` when every
    path failed dense-style — the view itself likely never settled); the caller then keeps the
    HTML-only report.
    """
    html_path, pdf_path = Path(html_path), Path(pdf_path)
    choice = os.environ.get("BIH_REPORT_RENDERER", "auto").strip().lower()

    if choice == "none":
        raise NoRendererError("PDF rendering disabled (BIH_REPORT_RENDERER=none)")

    if choice == "playwright":
        if not _playwright_available():
            raise NoRendererError("BIH_REPORT_RENDERER=playwright but Playwright/Chromium is unavailable")
        _render_with_playwright(html_path, pdf_path)
        return "playwright"

    pinned = choice not in ("auto", "")  # edge / chrome / chromium / explicit path
    engines = find_engines()
    attempts: "list[tuple[str, str, NoRendererError]]" = []  # (name, exe, first-pass failure)
    failures: "list[tuple[str, NoRendererError]]" = []

    # Pass 1 — every engine at the standard budget. BASE-01: Edge 149 headless printed nothing even
    # for a trivial page while the Chrome beside it worked; trying only the first engine turned a
    # broken preference into "no PDF at all".
    for name, exe in engines:
        try:
            _render_with_engine(exe, html_path, pdf_path, budget_ms=12000)
            return name
        except NoRendererError as exc:
            attempts.append((name, exe, exc))
            failures.append((name, exc))

    # Pass 2 — dense-style failures (exit 0, no PDF) get ONE bigger-budget retry (P8.7.1 #3): a
    # genuinely dense view needs more virtual time; a broken engine just fails again quickly.
    for name, exe, exc in attempts:
        if isinstance(exc, DenseRenderError):
            try:
                _render_with_engine(exe, html_path, pdf_path, budget_ms=40000)
                return name
            except NoRendererError as exc2:
                failures.append((name, exc2))

    # Playwright fallback — also when engines exist but ALL failed, not only when none are
    # installed. A pinned engine is an operator decision: never silently unpin it.
    if not pinned and _playwright_available():
        try:
            _render_with_playwright(html_path, pdf_path)
            return "playwright"
        except Exception as exc:  # keep the 'HTML produced, PDF skipped' contract — never crash the report
            failures.append(("playwright", NoRendererError(f"Playwright fallback failed: {exc}")))

    if failures:
        reasons = "; ".join(f"{name}: {exc}" for name, exc in failures)
        if all(isinstance(exc, DenseRenderError) for _, exc in failures):
            raise DenseRenderError(f"no engine rendered the view within budget — {reasons}")
        raise NoRendererError(f"every render path failed — {reasons}")

    if pinned:
        raise NoRendererError(
            f"BIH_REPORT_RENDERER={choice!r} pins an engine that was not found on this machine — "
            f"unset it (auto-discovery) or point it at an installed Edge/Chrome. The report HTML "
            f"(the hashed source of truth) was still written.")
    raise NoRendererError(
        "no PDF engine found — install Microsoft Edge or Google Chrome (the report prints with the OS "
        "browser engine), or `pip install -e \".[dev]\"` for the Playwright fallback. The report HTML "
        "(the hashed source of truth) was still written.")
