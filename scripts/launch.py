"""One-click desktop launcher (post-v1 packaging) + its hardened lifecycle (P2).

Serves the app on a private localhost port (FastAPI serving both the built SPA and the API on one
origin) and opens it in a native window via pywebview — no browser, no terminal juggling.

    python scripts/launch.py                 # open the desktop window (splash -> app)
    python scripts/launch.py --check         # headless: start server, verify /health, exit (CI/tests)
    python scripts/launch.py --case cases/acme/case.db   # open a specific case

P2 hardens the lifecycle around the existing one-origin serving (``backend/app/web.py``):

  * **Splash + background startup.** The window opens IMMEDIATELY on a bundled, dependency-free
    loading page; build/migrate/serve/wait run on a background thread; the window swaps to the app
    only once ``/health`` is green. A startup failure (build, migrate, port-bind) is SHOWN in the
    splash with a copyable detail — never a ``SystemExit`` to a terminal nobody is watching.
  * **Port hardening.** The free port is bound ONCE and the open socket is handed straight to
    uvicorn (``Server.run(sockets=[sock])``) — no pick -> close -> rebind gap (the old TOCTOU race).
  * **Graceful shutdown.** One idempotent teardown path runs on window-close, on SIGINT/SIGTERM, and
    on window-closed-mid-startup: it stops uvicorn (``should_exit`` + a bounded join/force_exit),
    releases the bound socket, the control channel, and the single-instance lock.
  * **Single-instance guard.** An OS advisory lock in the app-data dir stops a second launch from
    starting a second server on the same ``case.db`` (WAL contention). The second launch asks the
    running instance (over a localhost control channel) to raise its window instead.

Source-mode only: the frozen-exe data path + bundled-dist specifics are P7 (not pulled forward here).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASE = "cases/dev/case.db"
WINDOW_TITLE = "Blockchain Investigation Hub"
APP_NAME = "BlockchainInvestigationHub"


class LauncherError(Exception):
    """A startup failure that must be SHOWN to the user (in the splash window), not dumped to a
    terminal nobody is watching. Carries a one-line ``message`` + a copyable multi-line ``detail``."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail or ""


class _Cancelled(Exception):
    """Internal: startup was cancelled (the window was closed before the server became ready)."""


# --------------------------------------------------------------------------- prerequisites

def ensure_frontend_build(*, rebuild: bool = False) -> Path:
    """Return the built SPA dir, building it with ``npm run build`` if missing (or if ``rebuild``).

    Build failures raise :class:`LauncherError` (surfaced in the splash) rather than ``SystemExit``."""
    from backend.app.web import FRONTEND_DIST

    if FRONTEND_DIST.joinpath("index.html").exists() and not rebuild:
        return FRONTEND_DIST
    npm = shutil.which("npm")
    if not npm:
        raise LauncherError(
            "the interface is not built and npm was not found",
            detail="Install Node.js LTS, then run:\n  cd frontend\n  npm install\n  npm run build")
    print(">> building frontend (npm run build)…", flush=True)
    proc = subprocess.run("npm run build", cwd=str(ROOT / "frontend"), shell=True,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise LauncherError("building the interface failed (npm run build)",
                            detail=((proc.stdout or "") + "\n" + (proc.stderr or "")).strip())
    if not FRONTEND_DIST.joinpath("index.html").exists():
        raise LauncherError("npm run build did not produce frontend/dist/index.html",
                            detail=(proc.stdout or "").strip())
    return FRONTEND_DIST


def ensure_case_db(path: str) -> str:
    """Migrate (idempotently) the case DB so the window opens to a functional case, not a 503.

    Any failure becomes a :class:`LauncherError` so it can be shown in the splash."""
    try:
        from backend.app.db import apply_migrations, get_connection
        from backend.app.db import repository as repo

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        apply_migrations(p)  # forward-only + idempotent on an already-migrated DB
        conn = get_connection(p)
        try:
            if conn.execute("SELECT 1 FROM case_meta LIMIT 1").fetchone() is None:
                repo.init_case(conn, title="Local Case")
        finally:
            conn.close()  # per-call connection; nothing stays open to pin the WAL
        return str(p)
    except LauncherError:
        raise
    except Exception as exc:  # migration / IO / corrupt DB — show it, don't crash
        raise LauncherError(f"could not open or migrate the case database at {path}",
                            detail=f"{type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------------- port (no TOCTOU)

def bind_listen_socket(host: str = "127.0.0.1", port: int = 0) -> socket.socket:
    """Bind (and KEEP) a listening socket, to hand straight to uvicorn — closing the
    pick -> close -> rebind gap of the old ``pick_free_port`` helper. With ``port=0`` the OS assigns a
    free port; an explicit busy port raises a clear :class:`LauncherError` (shown in the splash)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR only on POSIX: there it just permits rebinding a TIME_WAIT port (safe). On
        # Windows it would allow HIJACKING a live listener, so we deliberately leave it off there and
        # let a busy port fail loudly.
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        raise LauncherError(f"could not bind {host}:{port}",
                            detail=f"{type(exc).__name__}: {exc}") from exc
    return sock


# --------------------------------------------------------------------------- server lifecycle

def start_server(sock: socket.socket, *, log_level: str = "warning"):
    """Start uvicorn(``backend.app.main:app``) on the ALREADY-BOUND ``sock`` in a daemon thread.

    Returns ``(server, thread)``. uvicorn uses the handed socket directly (no second bind), so there
    is no window in which the port is closed and could be stolen.

    DO NOT add ``--reload`` / ``reload=True`` here. The packaged app deliberately runs the server
    IN-PROCESS on a pre-bound socket so the splash, idempotent teardown, single-instance lock, and
    ControlServer all own this one process. uvicorn's reloader spawns a supervisor + worker SUBPROCESS,
    which breaks both the bound-socket handoff and the in-process teardown. Hot-reload is a dev-only
    concern and lives in ``scripts/dev_run.py`` (``make run``), never here."""
    import uvicorn

    host, port = sock.getsockname()[0], sock.getsockname()[1]
    config = uvicorn.Config("backend.app.main:app", host=host, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="uvicorn",
                              daemon=True)
    thread.start()
    return server, thread


def wait_until_ready(server, host: str, port: int, *, timeout: float = 30.0,
                     cancel: "threading.Event | None" = None) -> dict:
    """Block until the server reports started and ``/health`` answers 200. Returns the health body.

    Cancellable: if ``cancel`` is set (the window was closed mid-startup) it raises
    :class:`_Cancelled`; if the server exits first or never comes up it raises
    :class:`LauncherError`."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        if getattr(server, "should_exit", False):
            raise LauncherError("the local server stopped before it was ready")
        if getattr(server, "started", False):
            try:
                r = httpx.get(f"http://{host}:{port}/health", timeout=2.0)
                if r.status_code == 200:
                    return r.json()
            except httpx.HTTPError:
                pass
        time.sleep(0.1)
    raise LauncherError(f"the local server did not become ready on {host}:{port} within {timeout:.0f}s")


# --------------------------------------------------------------------------- single-instance guard

def app_data_dir() -> Path:
    """The per-user app-data dir for the lock + control endpoint files (and the P4 case registry).
    Delegates to the single backend resolver so launcher + backend agree on one location; still
    overridable via ``BIH_APP_DATA_DIR`` (tests). Source-mode only — the frozen-exe path is P7."""
    from backend.app.app_paths import app_data_dir as _app_data_dir

    return _app_data_dir()


def _lock_handle(fh) -> None:
    """Take an exclusive, NON-BLOCKING OS advisory lock on ``fh``. Raises ``OSError`` if held."""
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(fh) -> None:
    if sys.platform == "win32":
        import msvcrt
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class SingleInstance:
    """Cross-platform single-instance guard.

    A second launch would start a SECOND server on the same ``case.db`` (WAL contention, two windows
    of the same case). This guards against that with an OS advisory lock on a file in the app-data
    dir. The OS releases the lock automatically if the holder dies, so a crash leaves no stale lock
    to clear. The holder also records its control endpoint in a sidecar file so a refused second
    launch can ask it to raise its window."""

    def __init__(self, lock_path: Path, endpoint_path: Path) -> None:
        self.lock_path = lock_path
        self.endpoint_path = endpoint_path
        self._fh = None
        self.acquired = False

    def acquire(self) -> bool:
        """Try to take the lock. ``True`` if this is the only instance; ``False`` if one is running."""
        fh = open(self.lock_path, "a+")
        try:
            _lock_handle(fh)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        self.acquired = True
        return True

    def write_endpoint(self, host: str, port: int) -> None:
        """Record this instance's control endpoint (a refused second launch reads it to FOCUS us)."""
        self.endpoint_path.write_text(f"{os.getpid()}\n{host}\n{port}\n", encoding="utf-8")

    def read_endpoint(self) -> "tuple[str, int] | None":
        """Read the running instance's ``(host, port)`` control endpoint, or ``None`` if unreadable."""
        try:
            parts = self.endpoint_path.read_text(encoding="utf-8").splitlines()
            return parts[1], int(parts[2])
        except (OSError, IndexError, ValueError):
            return None

    def release(self) -> None:
        if self._fh is not None:
            _unlock_handle(self._fh)
            self._fh.close()
            self._fh = None
        self.acquired = False


# --------------------------------------------------------------------------- focus control channel

class ControlServer:
    """A localhost-only control channel for the single-instance guard. The running instance listens;
    a refused second launch connects and sends ``FOCUS`` to raise the existing window instead of
    starting a second server. Bound to 127.0.0.1 on an OS-assigned port (recorded in the endpoint
    file). Daemon thread; ``stop()`` is idempotent."""

    def __init__(self, on_focus) -> None:
        self._on_focus = on_focus
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self.host = "127.0.0.1"
        self.port = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind((self.host, 0))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="bih-control", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(64)
                    if data.strip() == b"FOCUS":
                        conn.sendall(b"OK\n")
                        try:
                            self._on_focus()
                        except Exception:
                            pass
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def send_focus(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """Ask a running instance to raise its window. ``True`` if it acknowledged, ``False`` if the
    endpoint is unreachable (a stale endpoint file — the instance is actually gone)."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as c:
            c.sendall(b"FOCUS\n")
            return c.recv(16).strip() == b"OK"
    except OSError:
        return False


# --------------------------------------------------------------------------- splash / error pages

def _neo_palette() -> dict:
    """The Neo-Tokyo colors for the splash, pulled from the SINGLE token catalog (no hardcoded hex).
    Falls back to a tiny built-in palette only if the catalog can't be read, so a splash always
    renders even when something is broken."""
    try:
        from backend.app.theme import theme_tokens

        t = theme_tokens("neo-tokyo-night")
        return {
            "bg": t["canvas.background"], "panel": t["ui.panel.elevated"], "border": t["ui.border"],
            "text": t["ui.text"], "muted": t["ui.text.secondary"], "accent": t["node.seed.marker"],
            "error": t["ui.error"],
        }
    except Exception:
        return {"bg": "#100c1e", "panel": "#231a46", "border": "#322a5c", "text": "#ece6ff",
                "muted": "#aea3d6", "accent": "#4deeea", "error": "#ff6b6b"}


def splash_html(status: str = "Starting…") -> str:
    """The bundled, dependency-free loading page (Neo-Tokyo styled; no external assets/fonts). Shown
    instantly while build/migrate/serve run on a background thread."""
    c = _neo_palette()
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{WINDOW_TITLE}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{ background: {c['bg']}; color: {c['text']};
    font-family: -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center; }}
  .wrap {{ text-align: center; max-width: 520px; padding: 32px; }}
  .ring {{ width: 64px; height: 64px; margin: 0 auto 24px; border-radius: 50%;
    border: 3px solid {c['border']}; border-top-color: {c['accent']};
    animation: spin 0.9s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  h1 {{ font-size: 20px; font-weight: 600; margin: 0 0 8px; letter-spacing: 0.3px; }}
  .accent {{ color: {c['accent']}; }}
  #bih-status {{ color: {c['muted']}; font-size: 14px; margin: 0; min-height: 18px; }}
</style></head>
<body><div class="wrap">
  <div class="ring"></div>
  <h1>Blockchain <span class="accent">Investigation Hub</span></h1>
  <p id="bih-status">{status}</p>
</div>
<script>
  window.__bihStatus = function (t) {{
    var el = document.getElementById('bih-status'); if (el) el.textContent = t;
  }};
</script>
</body></html>"""


def error_html(message: str, detail: str = "") -> str:
    """The splash's error state: a readable message + a copyable detail (build/migrate/port failure).
    Never a SystemExit to a terminal nobody sees."""
    c = _neo_palette()
    # Escape for safe embedding in HTML text + a JS string literal.
    import html
    import json as _json

    safe_msg = html.escape(message)
    safe_detail = html.escape(detail) if detail else ""
    detail_block = (f'<pre id="bih-detail">{safe_detail}</pre>'
                    '<button id="bih-copy" onclick="bihCopy()">Copy details</button>') if detail else ""
    detail_js = _json.dumps(detail)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{WINDOW_TITLE}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{ background: {c['bg']}; color: {c['text']};
    font-family: -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center; }}
  .wrap {{ max-width: 640px; padding: 32px; }}
  h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 6px; color: {c['error']}; }}
  p.msg {{ color: {c['text']}; font-size: 15px; margin: 0 0 16px; }}
  pre {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 12px; font-size: 12px; line-height: 1.4; color: {c['muted']};
    max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-word; }}
  button {{ margin-top: 12px; background: {c['accent']}; color: {c['bg']}; border: 0;
    border-radius: 6px; padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer; }}
</style></head>
<body><div class="wrap">
  <h1>Couldn't start the Investigation Hub</h1>
  <p class="msg">{safe_msg}</p>
  {detail_block}
</div>
<script>
  function bihCopy() {{
    var txt = {detail_js};
    if (navigator.clipboard) {{ navigator.clipboard.writeText(txt); }}
    var b = document.getElementById('bih-copy'); if (b) b.textContent = 'Copied';
  }}
</script>
</body></html>"""


# --------------------------------------------------------------------------- the launcher

class Launcher:
    """Owns every lifecycle resource (bound socket, uvicorn server, control channel, instance lock,
    window) behind ONE idempotent :meth:`teardown`. The windowed run shows a splash immediately and
    does the heavy startup on a background thread; ``--check`` runs the same startup headlessly."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sock: "socket.socket | None" = None
        self.server = None
        self.server_thread: "threading.Thread | None" = None
        self.window = None
        self.instance: "SingleInstance | None" = None
        self.control: "ControlServer | None" = None
        self.url: "str | None" = None
        self.case: "str | None" = None
        self._cancel = threading.Event()       # set on shutdown / window-closed-during-startup
        self._stop_block = threading.Event()    # releases the headless wait loop
        self._torn = False
        self._teardown_lock = threading.Lock()

    # -- startup steps (raise LauncherError -> shown in the splash) --------------------------

    def prepare(self) -> None:
        """Build the UI (unless skipped) + resolve the startup case, then point the app at it.

        Startup case (P4): an explicit ``--case`` / ``BIH_CASE_DB`` wins; else the registry's
        last-opened case; else NO case — the app opens on the entry screen (empty state) instead of
        forcing ``cases/dev``."""
        if not self.args.no_build:
            ensure_frontend_build(rebuild=self.args.rebuild)
        self.case = self._resolve_startup_case()
        if self.case:
            os.environ["BIH_CASE_DB"] = self.case
        else:
            os.environ.pop("BIH_CASE_DB", None)  # empty state -> the picker

    def _resolve_startup_case(self) -> "str | None":
        """The case to open at startup, or ``None`` for the empty-state picker. A named/env case is
        created+migrated; a registry last-opened is migrated forward; both are made the active case."""
        from backend.app.services import case_registry, cases

        target = self.args.case or os.environ.get("BIH_CASE_DB") or case_registry.last_opened_path()
        if not target:
            return None
        path = ensure_case_db(target)   # create (if named new) + forward-migrate (LauncherError on failure)
        cases.set_active_case(path)     # register in Recent + make it the in-process active case
        return path

    def serve(self) -> dict:
        """Bind the port ONCE, hand the socket to uvicorn, and wait for ``/health`` (cancellable)."""
        self.sock = bind_listen_socket(self.args.host, self.args.port)
        host, port = self.sock.getsockname()[0], self.sock.getsockname()[1]
        self.url = f"http://{host}:{port}/"
        self.server, self.server_thread = start_server(self.sock)
        return wait_until_ready(self.server, host, port, cancel=self._cancel)

    # -- teardown (one path, idempotent, always runs) ---------------------------------------

    def teardown(self) -> None:
        with self._teardown_lock:
            if self._torn:
                return
            self._torn = True
        self._cancel.set()
        self._stop_block.set()
        if self.control is not None:
            self.control.stop()
        if self.server is not None:
            # Graceful drain first; fall back to force_exit so teardown can never hang.
            self.server.should_exit = True
            if self.server_thread is not None:
                self.server_thread.join(timeout=8.0)
                if self.server_thread.is_alive():
                    self.server.force_exit = True
                    self.server_thread.join(timeout=2.0)
        if self.sock is not None:
            try:
                self.sock.close()  # harmless if uvicorn already closed it on shutdown
            except OSError:
                pass
        if self.instance is not None:
            self.instance.release()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):
            self.teardown()
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not the main thread / unsupported on this platform

    # -- window focus (single-instance) ------------------------------------------------------

    def _focus_window(self) -> None:
        w = self.window
        if w is None:
            return
        for meth in ("restore", "show"):
            try:
                getattr(w, meth)()
            except Exception:
                pass
        try:  # best-effort raise-to-front
            w.on_top = True
            w.on_top = False
        except Exception:
            pass

    def _on_window_closed(self) -> None:
        # Window closed (possibly mid-startup) -> cancel the background startup and tear down.
        self._cancel.set()
        self.teardown()

    def _set_status(self, text: str) -> None:
        w = self.window
        if w is None:
            return
        try:
            import json as _json

            w.evaluate_js(f"window.__bihStatus && window.__bihStatus({_json.dumps(text)})")
        except Exception:
            pass  # status updates are best-effort cosmetic; never let them break startup

    # -- run modes --------------------------------------------------------------------------

    def _run_startup(self) -> None:
        """The background-thread body for windowed mode: prepare -> serve -> swap the splash for the
        app, or show the failure in the splash. Never raises (a bg-thread crash would be invisible)."""
        try:
            self._set_status("Preparing the interface…")
            self.prepare()
            if self._cancel.is_set():
                return
            self._set_status("Starting the local server…")
            self.serve()
            if self._cancel.is_set():
                return
            self._emit_keyring_warning()  # SEC-08: non-fatal keyring-unavailable notice
            self._set_status("Ready.")
            self.window.load_url(self.url)
        except _Cancelled:
            return  # window closed during startup; teardown already running
        except LauncherError as exc:
            if not self._cancel.is_set() and self.window is not None:
                try:
                    self.window.load_html(error_html(str(exc), exc.detail))
                except Exception:
                    pass
        except Exception as exc:  # truly unexpected — still surface it, don't die silently
            if not self._cancel.is_set() and self.window is not None:
                try:
                    self.window.load_html(error_html("Unexpected startup error",
                                                     f"{type(exc).__name__}: {exc}"))
                except Exception:
                    pass

    def run_windowed(self) -> int:
        try:
            import webview
        except ImportError:
            return self._run_headless_blocking()

        self._install_signal_handlers()
        # Stand up the focus control channel BEFORE the window so a racing second launch can reach us.
        if self.instance is not None:
            self.control = ControlServer(on_focus=self._focus_window)
            self.control.start()
            self.instance.write_endpoint(self.control.host, self.control.port)

        self.window = webview.create_window(
            WINDOW_TITLE, html=splash_html("Starting the Investigation Hub…"),
            width=1280, height=860, min_size=(900, 600))
        # Register the window so the backend's native file-dialog endpoint (case picker: open/import)
        # can run the OS dialog against it. Dev/browser mode never registers one (HTML upload fallback).
        try:
            from backend.app.services import cases as _cases

            _cases.register_native_window(self.window)
        except Exception:
            pass  # non-fatal: the dialog endpoint just reports 'unavailable' and the UI falls back
        try:
            self.window.events.closed += self._on_window_closed
        except Exception:
            pass  # older/newer pywebview event API differences — non-fatal

        try:
            webview.start(self._run_startup)  # shows the splash, runs _run_startup on a worker thread
            return 0
        finally:
            self.teardown()

    def _run_headless_blocking(self) -> int:
        """Fallback when pywebview is not installed: serve headlessly and block until interrupted,
        so the printed URL is actually usable."""
        print("pywebview is not installed — serving headlessly; open the URL below in a browser. "
              "(`pip install -e \".[app]\"` for a native window.)", file=sys.stderr)
        self._install_signal_handlers()
        try:
            self.prepare()
            health = self.serve()
        except LauncherError as exc:
            print(f">> startup failed: {exc}", file=sys.stderr)
            if exc.detail:
                print(exc.detail, file=sys.stderr)
            self.teardown()
            return 2
        print(f">> serving {WINDOW_TITLE} at {self.url}  (case: {self.case})")
        print(f">> health: {health}")
        self._emit_keyring_warning()  # SEC-08: non-fatal keyring-unavailable notice
        print(">> press Ctrl-C to stop.")
        try:
            self._stop_block.wait()
        finally:
            self.teardown()
        return 0

    def _keyring_warning(self) -> str | None:
        """SEC-08: a NON-FATAL warning when the OS keyring backend is unavailable — API keys can't be saved
        (a clean 503 at key-write) until a backend is present. Returned (for tests) + printed at startup so a
        keyring-less host is told up front instead of only discovering it at key-write time."""
        try:
            from backend.app.secrets import keyring_status
            st = keyring_status()
        except Exception:
            return None
        if st.get("available"):
            return None
        return (">> WARNING: no usable OS keyring backend "
                f"(backend={st.get('backend')!r}); API keys cannot be saved until one is available "
                "(Settings → Connectors will report this). The app runs; ingest that needs a key will fail "
                "with a clear message.")

    def _emit_keyring_warning(self) -> None:
        msg = self._keyring_warning()
        if msg:
            print(msg, file=sys.stderr)

    def _self_check_probes(self, health: dict) -> dict:
        """The frozen DoD battery (P8): beyond /health, exercise the graph API, the keyring backend, the
        bundled-certifi TLS path, and report the writable/bundle path split — all from INSIDE the (frozen)
        process so the smoke proves the real exe, not a source-mode re-creation. Returns a JSON-able dict;
        ``ok`` is the hard gate (health + graph + TLS); keyring availability is reported (the smoke decides
        per-OS, since a host may legitimately lack a backend)."""
        import httpx

        from backend.app.app_paths import bundle_dir, cases_root, is_frozen, settings_path, user_data_dir
        from backend.app.runtime import ca_bundle
        from backend.app.secrets import keyring_status

        result: dict = {"ok": True, "health_status": health.get("status")}

        # graph API — proves the migrated case reads back through the frozen DB/repository/serialization.
        try:
            r = httpx.get(self.url + "api/graph", timeout=5.0)
            g = r.json() if r.status_code == 200 else {}
            result["graph"] = {"http_status": r.status_code,
                               "nodes": len(g.get("nodes", [])), "edges": len(g.get("edges", []))}
            if r.status_code != 200:
                result["ok"] = False
        except Exception as exc:
            result["graph"] = {"error": f"{type(exc).__name__}: {exc}"}
            result["ok"] = False

        # keyring — must RESOLVE (not crash) and report a backend. SEC-08: in the FROZEN app the backend
        # must also be AVAILABLE on ANY OS (a broken keyring means key-write 503s later) — that is a hard
        # gate. Source-mode --check reports it but does not gate (a keyring-less CI host is legitimate).
        try:
            result["keyring"] = keyring_status()
            if is_frozen() and not result["keyring"].get("available"):
                result["ok"] = False
        except Exception as exc:
            result["keyring"] = {"error": f"{type(exc).__name__}: {exc}"}
            result["ok"] = False

        # TLS — constructing an httpx client with the bundled certifi CA must load the cafile (no network;
        # offline-first holds). This is exactly what every HTTPS connector does at init.
        try:
            ca = ca_bundle()
            httpx.Client(verify=ca).close()
            result["tls"] = {"ca_bundle": ca if isinstance(ca, str) else None,
                             "exists": bool(isinstance(ca, str) and os.path.exists(ca))}
            if not result["tls"]["exists"]:
                result["ok"] = False
        except Exception as exc:
            result["tls"] = {"error": f"{type(exc).__name__}: {exc}"}
            result["ok"] = False

        # path split — the smoke asserts these land under %APPDATA%/BIH and NOT under _MEIPASS.
        result["paths"] = {
            "frozen": is_frozen(),
            "bundle_dir": str(bundle_dir()),
            "user_data_dir": str(user_data_dir()),
            "cases_root": str(cases_root()),
            "settings_path": str(settings_path()),
            "meipass": getattr(sys, "_MEIPASS", None),
        }

        # P39 — the bundled first-run sample must import + open + read back through the FROZEN app (the
        # one-click "Explore the sample case"). FROZEN-ONLY: in source mode cases_root is the repo cases/
        # dir, so extracting a copy there would pollute the tree — the frozen smoke is the gate. When
        # frozen it hard-gates ``ok`` (a build that can't open its own sample is a ship defect).
        if is_frozen():
            try:
                from backend.app.services import cases as _cases

                if _cases.sample_casefile_path() is None:
                    result["sample"] = {"available": False}
                    result["ok"] = False
                else:
                    imp = _cases.import_sample_case()
                    verified = bool(imp.get("verification", {}).get("ok"))
                    rs = httpx.get(self.url + "api/graph", timeout=8.0)
                    gs = rs.json() if rs.status_code == 200 else {}
                    nodes = len(gs.get("nodes", []))
                    result["sample"] = {"available": True, "imported": bool(imp.get("opened")),
                                        "verified": verified, "graph_status": rs.status_code,
                                        "nodes": nodes, "edges": len(gs.get("edges", []))}
                    if not (imp.get("opened") and verified and rs.status_code == 200 and nodes > 0):
                        result["ok"] = False
            except Exception as exc:
                result["sample"] = {"error": f"{type(exc).__name__}: {exc}"}
                result["ok"] = False
        else:
            result["sample"] = {"skipped": "source-mode (frozen-only probe)"}
        return result

    def run_check(self) -> int:
        """Headless verification path (CI/tests + the P8 FROZEN smoke gate): build/serve, verify
        ``/health``, then run the self-check battery (graph API / keyring / TLS / path split) and emit a
        single ``SELFCHECK <json>`` line for the smoke to assert on. Tear down, exit (0 only if the hard
        gate passed)."""
        import json as _json

        self._install_signal_handlers()
        try:
            self.prepare()
            health = self.serve()
        except LauncherError as exc:
            print(f">> startup failed: {exc}", file=sys.stderr)
            if exc.detail:
                print(exc.detail, file=sys.stderr)
            self.teardown()
            return 1
        print(f">> serving {WINDOW_TITLE} at {self.url}  (case: {self.case})")
        print(f">> health: {health}")
        probes = self._self_check_probes(health)
        print("SELFCHECK " + _json.dumps(probes))
        print(f">> self-check: {'OK' if probes['ok'] else 'FAILED'}")
        self.teardown()
        return 0 if probes["ok"] else 1


# --------------------------------------------------------------------------- entrypoint

def _parse_args(argv: "list[str] | None") -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="One-click desktop launcher for the Investigation Hub.")
    ap.add_argument("--case", default=os.environ.get("BIH_CASE_DB"),
                    help="case DB to open (default: the last-opened case, else the entry-screen picker)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    ap.add_argument("--check", action="store_true",
                    help="headless: build/serve, verify /health, then exit (no window)")
    ap.add_argument("--no-build", action="store_true", help="do not build the frontend if missing")
    ap.add_argument("--rebuild", action="store_true", help="force-rebuild the frontend")
    ap.add_argument("--allow-multiple", action="store_true",
                    help="skip the single-instance guard (open a second window/server)")
    return ap.parse_args(argv)


def main(argv: "list[str] | None" = None) -> int:
    # P7: the frozen-app entrypoint — configure TLS (bundled certifi) + the OS keyring backend before
    # anything builds/serves/fetches. No-op-cheap in source mode.
    from backend.app.runtime import configure_frozen_runtime

    configure_frozen_runtime()
    args = _parse_args(argv)
    launcher = Launcher(args)

    # --check is a transient health probe (CI/tests): no window, no single-instance semantics.
    if args.check:
        return launcher.run_check()

    if not args.allow_multiple:
        d = app_data_dir()
        instance = SingleInstance(d / "instance.lock", d / "instance.endpoint")
        if not instance.acquire():
            # Already running: ask it to raise its window instead of starting a second server.
            ep = instance.read_endpoint()
            if ep is not None and send_focus(*ep):
                print(">> Investigation Hub is already running — raised the existing window.")
                return 0
            print(">> Investigation Hub appears to be already running, but it could not be contacted "
                  "to focus its window. Close the other instance and try again "
                  "(or pass --allow-multiple).", file=sys.stderr)
            return 3
        launcher.instance = instance

    return launcher.run_windowed()


if __name__ == "__main__":
    raise SystemExit(main())
