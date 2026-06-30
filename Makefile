# Blockchain Investigation Hub — task runner (CLAUDE.md §4)
#
# Recipes delegate to Python/npm so each is a single command that works under both
# cmd.exe (Windows default Make shell) and /bin/sh (Linux/CI). Heavy logic lives in
# scripts/*.py to stay cross-platform.

ifeq ($(OS),Windows_NT)
  VENV_PY := .venv/Scripts/python.exe
else
  VENV_PY := .venv/bin/python
endif

# Base interpreter used to *create* the venv. Override on CI: `make setup PYTHON=python`.
PYTHON ?= py -3.13
# Target case DB for migrate/audit. Override: `make audit CASE=cases/foo/case.db`.
CASE   ?= cases/dev/case.db

.PHONY: setup migrate test audit smoke run app report export package package-debug smoke-frozen installer sign verify-sign retest retest-clustering clean help

help:
	@$(PYTHON) -c "print('targets: setup migrate test audit smoke run app report export clean')"

setup:
	$(PYTHON) scripts/setup.py

migrate:
	$(VENV_PY) -m backend.app.db.migrate "$(CASE)"

test:
	$(VENV_PY) -m pytest backend/tests

audit:
	$(VENV_PY) -m backend.app.db.migrate "$(CASE)"
	$(VENV_PY) -m backend.app.audits.runner --db "$(CASE)"

smoke:
	$(VENV_PY) -m pytest backend/tests -m smoke

run:
	$(VENV_PY) scripts/dev_run.py

# One-click packaged app: serve the built SPA + API on one origin, open a native pywebview window.
app:
	$(VENV_PY) scripts/launch.py

report:
	$(VENV_PY) scripts/report.py "$(CASE)"

export:
	$(VENV_PY) scripts/export.py "$(CASE)" --verify

# Build the one-folder desktop app (PyInstaller): npm run build -> dist/BIH/ (windowed, no console).
package:
	$(VENV_PY) scripts/package.py

# Same, but a console build that prints frozen tracebacks (for debugging a frozen failure).
package-debug:
	$(VENV_PY) scripts/package.py --console

# Frozen end-to-end smoke — the P8 DoD gate. Runs the BUILT exe's headless --check and asserts the
# frozen app is correct (health/graph/keyring/TLS + writes under app-data, none under _MEIPASS).
smoke-frozen:
	$(VENV_PY) scripts/frozen_smoke.py

# P9 — build the Windows installer (Inno Setup) bundling dist/BIH/ into a single UNSIGNED setup.exe:
# per-user/Program-Files, Start-Menu + Desktop shortcuts (8.ico), clean uninstaller that preserves
# %APPDATA% cases. No cert needed; auto-signs only if one is configured (else cleanly unsigned).
installer:
	$(VENV_PY) scripts/installer.py

# P9 — OPTIONAL Authenticode signing of the built exe + installer. Cleanly SKIPS (no error) when no cert
# is configured; signs with signtool + an RFC3161 timestamp when BIH_SIGN_PFX+BIH_SIGN_PASSWORD (or
# BIH_SIGN_THUMBPRINT) is set. Enabling signing is exactly this one-line config change — nothing else.
sign:
	$(VENV_PY) scripts/sign.py

# P9 — verify signatures (signtool verify /pa) on whatever is signed; a no-op when nothing is signed.
verify-sign:
	$(VENV_PY) scripts/sign.py --verify

# Re-run the 3 verification cases (Colonial · BTC, Tornado · EVM, Vitalik · EVM shallow) end-to-end into
# the app-data cases folder: fresh case -> ingest -> synchronous valuation -> check intel -> report.
# LIVE (real API calls); EVM needs the keyring Etherscan key. Prints a per-case sanctions/valuation summary.
retest:
	$(VENV_PY) scripts/retest_cases.py

# P8.8 — demonstrate the clustering heuristics end-to-end (co-spend + BlockSci change on a BTC case;
# Victor deposit-reuse + the Leiden community overlay on an EVM case): apply, split/undo, a current-view
# report, and a per-heuristic cluster summary. LIVE; EVM needs the keyring Etherscan key.
retest-clustering:
	$(VENV_PY) scripts/retest_clustering.py

clean:
	$(PYTHON) scripts/clean.py
