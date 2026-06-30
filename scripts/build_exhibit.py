"""P10 — render the curated showcase view to an SVG + PNG exhibit (the README hero).

Reuses the SAME view the report draws (``build_view`` + the print-light Cytoscape style) so the exhibit
matches the report exactly, then drives the OS Edge/Chrome engine headlessly:

  * PNG  — ``--screenshot`` of a full-bleed Cytoscape canvas on white (crisp, @2x).
  * SVG  — cytoscape-svg's ``cy.svg()`` written into a DOM node + extracted via ``--dump-dom`` (vector,
    the court-exhibit-preferred format).

Run AFTER ``scripts/build_showcase.py`` (it reads that run's view spec from the scratchpad and writes
``exhibit.png`` / ``exhibit.svg`` into the same ``examples/<case>/`` folder).
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.db import get_connection
from backend.app.services.graph_view import build_view
from backend.app.services.report_render import find_engine
from backend.app.theme import cytoscape_style_json

# Portable temp dir shared with scripts/build_showcase.py (the view spec hand-off). NOT a committed path.
SCRATCH = Path(tempfile.gettempdir()) / "bih_showcase"
CYTO_JS = ROOT / "backend" / "app" / "report_templates" / "cytoscape.min.js"
CYTO_SVG_JS = ROOT / "frontend" / "node_modules" / "cytoscape-svg" / "cytoscape-svg.js"


def _exhibit_html(graph: dict) -> str:
    elements = [{"data": n} for n in graph["nodes"]] + [{"data": e} for e in graph["edges"]]
    # cytoscape FIRST (defines window.cytoscape) so cytoscape-svg auto-registers cy.svg() on load.
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;height:100%;background:#ffffff;}}#cy{{position:absolute;inset:0;background:#ffffff;}}</style>
<script>{CYTO_JS.read_text(encoding='utf-8')}</script>
<script>{CYTO_SVG_JS.read_text(encoding='utf-8')}</script>
</head><body>
<div id="cy"></div>
<div id="svgout" style="display:none"></div>
<script>
var ELEMENTS = {json.dumps(elements)};
var STYLE = {cytoscape_style_json()};
var cy = cytoscape({{ container: document.getElementById('cy'), elements: ELEMENTS, style: STYLE }});
function finish() {{
  if (window.__CY_READY__) return;
  try {{ cy.fit(undefined, 35); }} catch (e) {{}}
  try {{ document.getElementById('svgout').textContent = cy.svg({{ full: true, bg: '#ffffff', scale: 1 }}); }}
  catch (e) {{ document.getElementById('svgout').textContent = 'SVGERR:' + e; }}
  window.__CY_READY__ = true;
}}
var layout = cy.layout({{ name: 'cose', animate: false, padding: 45, nodeRepulsion: 12000 }});
layout.one('layoutstop', finish);
setTimeout(function () {{ try {{ layout.run(); }} finally {{ finish(); }} }}, 0);
</script></body></html>"""


def _base_flags(profile: Path, headless: str = "new") -> list[str]:
    # PNG screenshot wants the modern headless; --dump-dom (SVG extraction) is only emitted to stdout by the
    # LEGACY headless mode (--headless=old) in current Edge/Chrome — the new mode dropped dump-dom-to-stdout.
    return [f"--headless={headless}", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", f"--user-data-dir={profile}",
            "--run-all-compositor-stages-before-draw", "--virtual-time-budget=9000",
            "--window-size=1600,1000"]


def _render_png(exe: str, html_uri: str, out_png: Path) -> None:
    profile = Path(tempfile.mkdtemp(prefix="bih-exhibit-png-"))
    cmd = [exe, *_base_flags(profile), "--hide-scrollbars", "--force-device-scale-factor=2",
           f"--screenshot={out_png.resolve()}", html_uri]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    finally:
        import shutil
        shutil.rmtree(profile, ignore_errors=True)


def _render_svg(exe: str, html_uri: str, out_svg: Path) -> bool:
    profile = Path(tempfile.mkdtemp(prefix="bih-exhibit-svg-"))
    cmd = [exe, *_base_flags(profile, headless="old"), "--dump-dom", html_uri]
    try:
        # The dumped DOM carries UTF-8 (★ markers, SVG data-URIs); force UTF-8 so Windows' cp1252 default
        # doesn't choke on a high byte.
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
    finally:
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    m = re.search(r'<div id="svgout"[^>]*>(.*?)</div>', proc.stdout or "", re.DOTALL)
    if not m:
        return False
    svg = html.unescape(m.group(1)).strip()
    if not svg.startswith("<?xml") and "<svg" not in svg:
        return False
    out_svg.write_text(svg, encoding="utf-8")
    return True


def main() -> int:
    spec_path = SCRATCH / "showcase_viewspec.json"
    if not spec_path.exists():
        print(f">> no view spec at {spec_path} — run scripts/build_showcase.py first.", file=sys.stderr)
        return 2
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    examples = Path(spec["examples_dir"])
    examples.mkdir(parents=True, exist_ok=True)

    conn = get_connection(Path(spec["case_db"]))
    try:
        graph = build_view(conn, **spec["view_params"])
    finally:
        conn.close()
    print(f">> curated view: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges", flush=True)

    page = SCRATCH / "exhibit.html"
    page.write_text(_exhibit_html(graph), encoding="utf-8")
    html_uri = page.resolve().as_uri()

    engine = find_engine()
    if engine is None:
        print(">> no Edge/Chrome engine found — cannot render the exhibit images.", file=sys.stderr)
        return 2
    name, exe = engine
    print(f">> rendering with {name}: {exe}", flush=True)

    out_png = examples / "exhibit.png"
    out_svg = examples / "exhibit.svg"
    _render_png(exe, html_uri, out_png)
    svg_ok = _render_svg(exe, html_uri, out_svg)

    png_ok = out_png.exists() and out_png.stat().st_size > 0
    print("\n" + "=" * 64)
    print(f">> PNG: {out_png}  ({out_png.stat().st_size//1024 if png_ok else 0} KB)  "
          f"{'OK' if png_ok else 'FAILED'}")
    print(f">> SVG: {out_svg}  ({out_svg.stat().st_size//1024 if svg_ok else 0} KB)  "
          f"{'OK' if svg_ok else 'FAILED'}")
    print("=" * 64)
    return 0 if (png_ok and svg_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
