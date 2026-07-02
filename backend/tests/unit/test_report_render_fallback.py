"""BASE-01 (docs/review/FINDINGS.md): ``render_pdf`` must not give up while a working render path exists.

The R0 review baseline caught headless Edge 149 producing no PDF even for a trivial page while
Chrome and Playwright on the same machine rendered fine — and ``render_pdf`` only ever tried the
FIRST discovered engine, so every report PDF failed with a misleading "too dense" skip reason.

These tests pin the fallback chain: every discovered engine is tried at the standard budget, then
dense-style failures get one bigger-budget retry, then the optional Playwright fallback — and an
explicitly pinned engine (``BIH_REPORT_RENDERER=edge|chrome|<path>``) is honored strictly, never
silently unpinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.services import report_render as rr


@pytest.fixture
def html(tmp_path):
    p = tmp_path / "report.html"
    p.write_text("<html><body><script>window.__CY_READY__=true;</script>ok</body></html>",
                 encoding="utf-8")
    return p


def _engine_stub(behaviors: dict, calls: list):
    """A ``_render_with_engine`` stand-in: ``behaviors[exe]`` is an exception to raise, or ``None``
    to write a PDF (success). Every invocation is recorded as ``(exe, budget_ms)``."""

    def stub(exe, html_path, pdf_path, *, budget_ms=12000):
        calls.append((exe, budget_ms))
        exc = behaviors[exe]
        if exc is not None:
            raise exc
        Path(pdf_path).write_bytes(b"%PDF-1.7 fake")

    return stub


def _use_engines(monkeypatch, engines):
    """Point both discovery entry points at a fake engine list (find_engines may not exist yet on
    the pre-fix module — raising=False keeps the failure on the behavior, not the attribute)."""
    monkeypatch.setattr(rr, "find_engines", lambda: list(engines), raising=False)
    monkeypatch.setattr(rr, "find_engine", lambda: (engines[0] if engines else None))


def test_second_engine_renders_when_first_cannot_print(html, tmp_path, monkeypatch):
    # The R0 machine state: Edge discovered first but broken (exit 0, no PDF — the DenseRenderError
    # signature), Chrome right next to it working. The PDF must come from Chrome.
    pdf = tmp_path / "report.pdf"
    calls: list = []
    monkeypatch.delenv("BIH_REPORT_RENDERER", raising=False)
    _use_engines(monkeypatch, [("edge", "fake-edge"), ("chrome", "fake-chrome")])
    monkeypatch.setattr(rr, "_render_with_engine", _engine_stub(
        {"fake-edge": rr.DenseRenderError("no pdf within budget"), "fake-chrome": None}, calls))

    assert rr.render_pdf(html, pdf) == "chrome"
    assert pdf.exists() and pdf.stat().st_size > 0
    # Chrome was reached on the FIRST pass — no 40s dense-retry against the broken engine first.
    assert calls == [("fake-edge", 12000), ("fake-chrome", 12000)]


def test_playwright_renders_when_every_engine_fails(html, tmp_path, monkeypatch):
    # Playwright is the fallback when engines are present-but-failing, not only when none exist.
    pdf = tmp_path / "report.pdf"
    calls: list = []
    monkeypatch.delenv("BIH_REPORT_RENDERER", raising=False)
    _use_engines(monkeypatch, [("edge", "fake-edge"), ("chrome", "fake-chrome")])
    monkeypatch.setattr(rr, "_render_with_engine", _engine_stub(
        {"fake-edge": rr.NoRendererError("exit 1"),
         "fake-chrome": rr.NoRendererError("exit 1")}, calls))
    monkeypatch.setattr(rr, "_playwright_available", lambda: True)
    monkeypatch.setattr(rr, "_render_with_playwright",
                        lambda h, p: Path(p).write_bytes(b"%PDF-1.7 pw"))

    assert rr.render_pdf(html, pdf) == "playwright"
    assert pdf.exists() and pdf.stat().st_size > 0


def test_dense_failure_still_retries_big_budget_then_raises_dense(html, tmp_path, monkeypatch):
    # Behavior preservation (P8.7.1 #3): a genuinely dense view still gets the one bigger-budget
    # retry, and with no other path the actionable DenseRenderError still surfaces.
    pdf = tmp_path / "report.pdf"
    calls: list = []
    monkeypatch.delenv("BIH_REPORT_RENDERER", raising=False)
    _use_engines(monkeypatch, [("edge", "fake-edge")])
    monkeypatch.setattr(rr, "_render_with_engine", _engine_stub(
        {"fake-edge": rr.DenseRenderError("still not ready")}, calls))
    monkeypatch.setattr(rr, "_playwright_available", lambda: False)

    with pytest.raises(rr.DenseRenderError):
        rr.render_pdf(html, pdf)
    assert [b for _, b in calls] == [12000, 40000]


def test_playwright_failure_degrades_to_no_renderer_not_a_crash(html, tmp_path, monkeypatch):
    # The 'HTML produced, PDF skipped' contract: an internal Playwright error in the auto fallback
    # surfaces as NoRendererError (which generate_report catches), never an unrelated exception.
    pdf = tmp_path / "report.pdf"
    monkeypatch.delenv("BIH_REPORT_RENDERER", raising=False)
    _use_engines(monkeypatch, [])
    monkeypatch.setattr(rr, "_playwright_available", lambda: True)

    def boom(h, p):
        raise TimeoutError("page never became ready")

    monkeypatch.setattr(rr, "_render_with_playwright", boom)

    with pytest.raises(rr.NoRendererError):
        rr.render_pdf(html, pdf)


def test_pinned_engine_never_falls_back(html, tmp_path, monkeypatch):
    # BIH_REPORT_RENDERER=edge is an operator PIN: its failure must surface; a silent fallback to
    # another engine/Playwright would defy the documented override semantics.
    pdf = tmp_path / "report.pdf"
    calls: list = []
    monkeypatch.setenv("BIH_REPORT_RENDERER", "edge")
    _use_engines(monkeypatch, [("edge", "fake-edge")])
    monkeypatch.setattr(rr, "_render_with_engine", _engine_stub(
        {"fake-edge": rr.NoRendererError("exit 1")}, calls))
    monkeypatch.setattr(rr, "_playwright_available", lambda: True)  # would work — must NOT be used

    with pytest.raises(rr.NoRendererError):
        rr.render_pdf(html, pdf)
    assert calls == [("fake-edge", 12000)]


def test_hung_engine_becomes_no_renderer_error(html, tmp_path, monkeypatch):
    # A hung engine must surface as the check-next-path signal (NoRendererError), never as a raw
    # subprocess.TimeoutExpired that would skip the remaining engines and crash generate_report.
    import subprocess

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 120))

    monkeypatch.setattr(rr.subprocess, "run", hang)
    with pytest.raises(rr.NoRendererError, match="hung"):
        rr._render_with_engine("fake-engine", html, tmp_path / "report.pdf", budget_ms=12000)


def test_same_binary_is_never_discovered_twice(tmp_path, monkeypatch):
    # An Edge-only machine can resolve BOTH probes to one binary (Edge is in the generic Chromium
    # name lists on Linux/macOS) — the engine list must not try the same executable twice.
    from types import SimpleNamespace

    exe = tmp_path / "msedge.exe"
    exe.write_bytes(b"")
    monkeypatch.delenv("BIH_REPORT_RENDERER", raising=False)
    monkeypatch.setattr(rr, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(rr, "_WIN_EDGE", [str(exe)])
    monkeypatch.setattr(rr, "_WIN_CHROME", [str(exe)])  # same binary found via both probes

    assert rr.find_engines() == [("edge", str(exe))]


def test_custom_path_pin_preserves_original_case(tmp_path, monkeypatch):
    # Paths are case-sensitive on Linux/macOS: the pinned path must be checked and returned with
    # the operator's original casing, never lowercased.
    exe = tmp_path / "MixedCase-Engine.Exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("BIH_REPORT_RENDERER", str(exe))

    assert rr.find_engines() == [("custom", str(exe))]


def test_renderer_available_mirrors_render_pdf_pin_semantics(monkeypatch):
    # The availability probe must never say yes when render_pdf itself would refuse.
    monkeypatch.setattr(rr, "find_engine", lambda: None)
    monkeypatch.setattr(rr, "_playwright_available", lambda: True)

    monkeypatch.setenv("BIH_REPORT_RENDERER", "edge")   # pinned + absent -> render_pdf refuses
    assert rr.renderer_available() is False
    monkeypatch.setenv("BIH_REPORT_RENDERER", "none")   # disabled -> refuses
    assert rr.renderer_available() is False
    monkeypatch.delenv("BIH_REPORT_RENDERER", raising=False)  # auto -> Playwright fallback allowed
    assert rr.renderer_available() is True


def test_pinned_absent_engine_error_names_the_pin(html, tmp_path, monkeypatch):
    # The failure message must point at the pin, not tell the operator to install a browser that
    # may already be installed under a different name.
    monkeypatch.setenv("BIH_REPORT_RENDERER", "edge")
    _use_engines(monkeypatch, [])
    monkeypatch.setattr(rr, "_playwright_available", lambda: True)  # pin still wins

    with pytest.raises(rr.NoRendererError, match="BIH_REPORT_RENDERER"):
        rr.render_pdf(html, tmp_path / "report.pdf")
