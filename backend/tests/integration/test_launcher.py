"""Lifecycle tests for the one-click desktop launcher (post-v1 packaging, hardened in P2).

Two layers:
  * A headless ``--check`` subprocess smoke (the CI path) — start uvicorn in a thread, verify
    ``/health``, exit — unchanged from before so we never regress the windowless flow.
  * In-process tests for the P2 lifecycle: socket-handoff port binding (no rebind race), splash
    background-startup success + failure-surfaced, idempotent teardown on window-close + signal, and
    the single-instance guard (refuse a second launch + focus the first).

The GUI itself (pywebview) is never opened here; the launcher is factored so the *logic*
(prepare/serve/teardown, lock, control channel) is driven directly with fakes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import types
from pathlib import Path

import httpx
import pytest

import scripts.launch as L

ROOT = Path(__file__).resolve().parents[3]  # backend/tests/integration -> repo root


def _args(**kw) -> types.SimpleNamespace:
    base = dict(case="cases/dev/case.db", host="127.0.0.1", port=0, check=False,
                no_build=True, rebuild=False, allow_multiple=True)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Never touch the real per-user app-data dir, and never leak ``BIH_CASE_DB`` (prepare() sets it
    directly on os.environ) into sibling tests."""
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    prev = os.environ.get("BIH_CASE_DB")
    os.environ.pop("BIH_CASE_DB", None)
    yield
    # prepare() now sets the in-process active case (P4) — clear it so it never leaks into sibling tests.
    from backend.app.services import cases

    cases.clear_active_case()
    if prev is None:
        os.environ.pop("BIH_CASE_DB", None)
    else:
        os.environ["BIH_CASE_DB"] = prev


# --------------------------------------------------------------------------- fakes

class _FakeWindow:
    """Stand-in for a pywebview window: records the splash swaps the launcher drives."""

    def __init__(self) -> None:
        self.loaded_url = None
        self.loaded_html = None
        self.events = types.SimpleNamespace(closed=None)

    def load_url(self, url):
        self.loaded_url = url

    def load_html(self, html):
        self.loaded_html = html

    def evaluate_js(self, js):
        return None


class _FakeServer:
    def __init__(self) -> None:
        self.should_exit = False
        self.force_exit = False
        self.started = False


class _FakeThread:
    def __init__(self) -> None:
        self.joins = 0
        self._alive = True

    def join(self, timeout=None):
        self.joins += 1
        self._alive = False

    def is_alive(self):
        return self._alive


# --------------------------------------------------------------------------- headless --check (CI)

def test_launcher_check_mode_serves_and_reports_health(tmp_path):
    case = tmp_path / "case.db"
    proc = subprocess.run(
        [sys.executable, "scripts/launch.py", "--check", "--no-build", "--case", str(case)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "health:" in proc.stdout and "status" in proc.stdout
    assert case.exists()  # ensure_case_db migrated a fresh case so the window opens functional


def test_launcher_check_emits_selfcheck_battery(tmp_path):
    """``--check`` emits the ``SELFCHECK <json>`` battery the P8 FROZEN smoke parses+asserts on. Guard
    that contract HERE in fast source-mode CI so a launcher change can't silently break the frozen gate
    (paths.frozen is False in source; the frozen exe flips it True)."""
    import json

    case = tmp_path / "case.db"
    proc = subprocess.run(
        [sys.executable, "scripts/launch.py", "--check", "--no-build", "--case", str(case)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"

    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("SELFCHECK ")), None)
    assert line is not None, f"no SELFCHECK line in:\n{proc.stdout}"
    probes = json.loads(line[len("SELFCHECK "):])

    assert probes["ok"] is True
    assert probes["health_status"] == "ok"
    assert probes["graph"]["http_status"] == 200          # migrated case reads back through the API
    assert probes["tls"]["exists"] is True                # certifi CA resolves (frozen: the bundled one)
    assert "backend" in probes["keyring"]                 # keyring RESOLVES (availability is per-host)
    assert probes["paths"]["frozen"] is False             # source mode; the frozen exe reports True here


# --------------------------------------------------------------------------- port (no TOCTOU)

def test_bind_listen_socket_holds_the_port_continuously():
    """The bound socket KEEPS the port — a second bind to it fails — so there is no close/rebind
    gap for another process to slip into (the old pick_free_port TOCTOU)."""
    sock = L.bind_listen_socket("127.0.0.1", 0)
    try:
        port = sock.getsockname()[1]
        assert port > 0
        with pytest.raises(L.LauncherError):
            L.bind_listen_socket("127.0.0.1", port)  # still held -> refused
    finally:
        sock.close()


def test_explicit_busy_port_raises_a_clear_launcher_error():
    held = L.bind_listen_socket("127.0.0.1", 0)
    try:
        port = held.getsockname()[1]
        with pytest.raises(L.LauncherError) as ei:
            L.bind_listen_socket("127.0.0.1", port)
        assert str(port) in str(ei.value)
    finally:
        held.close()


def test_socket_handoff_serves_drains_and_releases(monkeypatch, tmp_path):
    """The EXACT socket the launcher binds is the one uvicorn serves on (no rebind), /health is
    green, and teardown drains the server thread + releases the port."""
    launcher = L.Launcher(_args(case=str(tmp_path / "case.db")))
    launcher.prepare()  # migrates the case (no build)

    handed = {}
    real_start = L.start_server

    def spy(sock, **kw):
        handed["fileno"] = sock.fileno()
        return real_start(sock, **kw)

    monkeypatch.setattr(L, "start_server", spy)
    health = launcher.serve()
    host, port = launcher.sock.getsockname()[0], launcher.sock.getsockname()[1]
    try:
        assert health.get("status") == "ok"
        assert handed["fileno"] == launcher.sock.fileno()  # same fd: never closed + reopened
        assert httpx.get(f"http://{host}:{port}/health", timeout=2.0).status_code == 200
    finally:
        launcher.teardown()

    assert launcher._torn
    assert not launcher.server_thread.is_alive()  # drained
    freed = L.bind_listen_socket(host, port)  # port released -> re-bindable
    freed.close()


# --------------------------------------------------------------------------- splash / startup

def test_startup_failure_is_surfaced_in_the_splash_not_raised(monkeypatch, tmp_path):
    launcher = L.Launcher(_args(case=str(tmp_path / "case.db")))
    launcher.window = _FakeWindow()

    def boom():
        raise L.LauncherError("could not migrate the case database", detail="disk full at sector 7")

    monkeypatch.setattr(launcher, "prepare", boom)
    launcher._run_startup()  # MUST NOT raise — a bg-thread crash would be invisible

    assert launcher.window.loaded_url is None
    assert launcher.window.loaded_html is not None
    assert "could not migrate the case database" in launcher.window.loaded_html
    assert "disk full at sector 7" in launcher.window.loaded_html  # copyable detail present


def test_successful_startup_swaps_the_splash_for_the_app(monkeypatch, tmp_path):
    launcher = L.Launcher(_args(case=str(tmp_path / "case.db")))
    launcher.window = _FakeWindow()
    monkeypatch.setattr(launcher, "prepare", lambda: None)
    monkeypatch.setattr(launcher, "serve", lambda: {"status": "ok"})
    launcher.url = "http://127.0.0.1:9999/"

    launcher._run_startup()

    assert launcher.window.loaded_url == "http://127.0.0.1:9999/"
    assert launcher.window.loaded_html is None  # no error page


# --------------------------------------------------------------------------- teardown / signals

def test_teardown_is_idempotent_and_stops_everything(tmp_path):
    launcher = L.Launcher(_args())
    launcher.server = _FakeServer()
    launcher.server_thread = _FakeThread()
    stops = {"control": 0, "release": 0}
    launcher.control = types.SimpleNamespace(stop=lambda: stops.__setitem__("control", stops["control"] + 1))
    launcher.instance = types.SimpleNamespace(release=lambda: stops.__setitem__("release", stops["release"] + 1))
    launcher.sock = L.bind_listen_socket("127.0.0.1", 0)

    launcher.teardown()
    launcher.teardown()  # second call is a no-op (idempotent)

    assert launcher.server.should_exit is True
    assert launcher.server_thread.joins == 1  # joined once, not on the second teardown
    assert stops == {"control": 1, "release": 1}


def test_wait_until_ready_is_cancellable_on_window_close():
    cancel = threading.Event()
    cancel.set()  # window already closed
    with pytest.raises(L._Cancelled):
        L.wait_until_ready(_FakeServer(), "127.0.0.1", 9, timeout=5.0, cancel=cancel)


def test_window_closed_during_startup_bails_and_tears_down(monkeypatch, tmp_path):
    launcher = L.Launcher(_args(case=str(tmp_path / "case.db")))
    launcher.window = _FakeWindow()
    started = threading.Event()
    release = threading.Event()

    def slow_prepare():
        started.set()
        release.wait(2.0)  # hold inside startup until the window 'closes'

    monkeypatch.setattr(launcher, "prepare", slow_prepare)
    monkeypatch.setattr(launcher, "serve",
                        lambda: pytest.fail("serve must not run after the window is closed"))

    th = threading.Thread(target=launcher._run_startup)
    th.start()
    assert started.wait(2.0)
    launcher._on_window_closed()  # close mid-startup -> cancel + teardown
    release.set()
    th.join(2.0)

    assert launcher._torn
    assert launcher.window.loaded_url is None  # never loaded the app


def test_signal_handler_runs_the_one_teardown(monkeypatch):
    launcher = L.Launcher(_args())
    calls = {"n": 0}
    monkeypatch.setattr(launcher, "teardown", lambda: calls.__setitem__("n", calls["n"] + 1))

    orig_int = signal.getsignal(signal.SIGINT)
    orig_term = signal.getsignal(signal.SIGTERM)
    try:
        launcher._install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)  # simulate Ctrl-C
        assert calls["n"] == 1
    finally:
        signal.signal(signal.SIGINT, orig_int)
        signal.signal(signal.SIGTERM, orig_term)


# --------------------------------------------------------------------------- single-instance guard

def test_second_instance_is_refused_and_focuses_the_first(tmp_path):
    lock, ep = tmp_path / "instance.lock", tmp_path / "instance.endpoint"
    first = L.SingleInstance(lock, ep)
    assert first.acquire() is True

    focused = threading.Event()
    control = L.ControlServer(on_focus=focused.set)
    control.start()
    first.write_endpoint(control.host, control.port)
    try:
        second = L.SingleInstance(lock, ep)
        assert second.acquire() is False  # refused — first holds the lock
        addr = second.read_endpoint()
        assert addr == (control.host, control.port)
        assert L.send_focus(*addr) is True  # ask the first to raise its window
        assert focused.wait(2.0)  # the first instance's focus callback fired
    finally:
        control.stop()
        first.release()

    # stale-lock recovery: once the holder releases (or dies), a fresh launch acquires cleanly.
    third = L.SingleInstance(lock, ep)
    assert third.acquire() is True
    third.release()


def test_send_focus_to_a_dead_endpoint_returns_false():
    # A stale endpoint file (the instance is actually gone) -> unreachable, reported honestly.
    assert L.send_focus("127.0.0.1", 1) is False


def test_app_data_dir_is_overridable_for_isolation(tmp_path, monkeypatch):
    target = tmp_path / "custom-appdata"
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(target))
    d = L.app_data_dir()
    assert d == target and d.is_dir()
