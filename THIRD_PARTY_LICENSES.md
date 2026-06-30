# Third-party licenses

The Blockchain Investigation Hub is licensed **GPL-3.0-or-later** (see [`LICENSE`](LICENSE)). This file
records the licenses of the third-party dependencies and **why the project license is GPL-3.0** — so the
copyleft claim is accurate and nothing GPL ships under a misleading permissive label.

> Regenerate the Python side with `python -m piplicenses --format=markdown --order=license`; the frontend
> side with the node tally in `PROGRESS.md` (P10). Versions below are the v1.0.0 build set.

## Why GPL-3.0 (not MIT)

Two **runtime** dependencies are copyleft and ship as part of the product, so a bare MIT label would be
inaccurate. GPL-3.0-or-later is the smallest license that cleanly accommodates both:

| Dependency | Role | License | Compatibility |
|---|---|---|---|
| **python-igraph** | Leiden community-detection (the `community` visual overlay) | **GPL-2.0-or-later** | GPLv2-**or-later** combines into a GPLv3 work ✓ |
| **cytoscape-svg** | SVG exhibit export (graph → vector exhibit) | **GPL-3.0** | same license as the project ✓ |

GPLv2-**only** would have been *incompatible* with GPLv3; igraph's `LICENSE` carries the explicit
"either version 2 … or (at your option) any later version" clause, so it is GPLv2+ and compatible. Every
other dependency is permissive (MIT / BSD / Apache-2.0 / MPL-2.0 / PSF / ISC), all of which are
GPLv3-compatible. There are **no GPLv2-only or proprietary runtime dependencies**.

`python-igraph` remains an **optional** extra (`pip install -e ".[clustering]"`); when it is absent the
community overlay degrades to a clear "unavailable" note and the rest of the app is unaffected.

## Python runtime dependencies (shipped in the frozen app)

| License | Packages |
|---|---|
| MIT | fastapi, pydantic, pydantic-core, pydantic-settings, annotated-types, typing-inspection, httptools, watchfiles, h11, keyring, jaraco.classes/context/functools, pyyaml, proxy_tools, bottle, pythonnet, clr_loader (MIT per upstream; PyPI metadata omits the field), cffi, anyio, sniffio, zipp, greenlet (MIT/PSF) |
| BSD-2/3-Clause | uvicorn, starlette, websockets, click, colorama, python-dotenv, httpx, httpcore, idna, sqlparse, pycparser, pywin32-ctypes |
| Apache-2.0 | yoyo-migrations, importlib_metadata |
| MPL-2.0 | certifi |
| PSF-2.0 | typing_extensions |
| **GPL-2.0-or-later** | **igraph** (optional — Leiden community detection) + texttable (its dep) |

## Frontend runtime dependencies (bundled into `frontend/dist`)

| License | Packages |
|---|---|
| MIT | react, react-dom, cytoscape, cytoscape-fcose, cose-base, layout-base |
| **GPL-3.0** | **cytoscape-svg** (SVG exhibit export) |

## Build- / dev-time only (NOT part of the distributed product)

These run on the build machine; they are not linked into the shipped source, the frozen binary, or the
frontend bundle, so their licenses do not govern the product.

- **PyInstaller** (GPL-2.0 **with the standard bootloader exception** that explicitly permits building and
  distributing apps under *any* license) + pyinstaller-hooks-contrib, pefile, altgraph.
- **Inno Setup** (the installer compiler — its own license; only our files + Inno's redistributable stub
  end up in the produced `setup.exe`).
- Frontend toolchain: vite, typescript (Apache-2.0), vitest, @vitejs/plugin-react, esbuild, rollup,
  @types/* (MIT); **caniuse-lite** ships browser-compat *data* under **CC-BY-4.0** (build-time data, not
  shipped).
- Test/dev: pytest (MIT), pytest-asyncio (Apache-2.0), hypothesis (MPL-2.0), respx (BSD), playwright
  (Apache-2.0), pip-licenses (MIT).

## Bundled vendored assets

- `backend/app/report_templates/cytoscape.min.js` — Cytoscape.js 3.34.0, **MIT**.
- OFAC SDN extract + GraphSense TagPack snapshots (`backend/app/intel/`) are **public-domain / openly
  published** government + community attribution data, used for offline screening (provenance recorded per
  Invariant #3). See the file headers for source + edition date.
