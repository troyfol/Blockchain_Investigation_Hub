"""Frozen-app path safety (P7) — simulate a PyInstaller-frozen environment WITHOUT building an exe.

Monkeypatch ``sys.frozen=True`` + a temp ``sys._MEIPASS`` and assert the hard invariants: bundled
read-only resources resolve INSIDE the bundle; all writable data (cases / settings / logs) resolves
under ``user_data_dir()`` and NEVER under the bundle; the portable sentinel flips data beside the exe;
the keyring-absent fallback is graceful; and TLS is pointed at the bundled certifi CA.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.app import app_paths as ap
from backend.app import runtime


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Simulate a frozen one-file app: sys.frozen=True + a temp _MEIPASS (the read-only bundle root),
    a temp exe location, and a temp writable user-data dir. Returns (bundle, user, exe_dir)."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    exe_dir = tmp_path / "exe"
    exe_dir.mkdir()
    user = tmp_path / "user"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "BIH.exe"), raising=False)
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(user))
    monkeypatch.delenv("BIH_CASES_ROOT", raising=False)
    monkeypatch.delenv("BIH_PORTABLE", raising=False)
    return bundle, user, exe_dir


# --------------------------------------------------------------------------- source mode

def test_source_mode_resolves_repo_resources():
    assert ap.is_frozen() is False
    # In source the bundle root is the repo root and every bundled resource exists there.
    repo_root = Path(__file__).resolve().parents[3]
    assert ap.bundle_dir() == repo_root
    for rel in ap.BUNDLED_RESOURCES.values():
        assert ap.resource_path(rel).exists(), f"{rel} missing in source bundle"


# --------------------------------------------------------------------------- frozen: resources in bundle

def test_frozen_resources_resolve_inside_the_bundle(frozen):
    bundle, _user, _exe = frozen
    assert ap.is_frozen() is True
    assert ap.bundle_dir() == bundle
    for rel in ap.BUNDLED_RESOURCES.values():
        p = ap.resource_path(rel)
        assert p == bundle / rel
        assert p.is_relative_to(ap.bundle_dir())   # bundled resources live INSIDE _MEIPASS


# --------------------------------------------------------------------------- frozen: writes outside bundle

def test_frozen_user_data_is_never_under_the_bundle(frozen):
    bundle, user, _exe = frozen
    write_paths = [ap.user_data_dir(), ap.app_data_dir(), ap.cases_root(),
                   ap.settings_path(), ap.settings_path().parent, ap.logs_dir()]
    for p in write_paths:
        assert not p.is_relative_to(bundle), f"{p} would write UNDER the read-only bundle"
        assert p.is_relative_to(user), f"{p} is not under user_data_dir"


def test_frozen_cases_root_defaults_under_user_data(frozen):
    _bundle, user, _exe = frozen
    assert ap.cases_root() == user / "cases"        # frozen default: cases under user data
    assert ap.settings_path() == user / "settings.json"
    assert ap.logs_dir() == user / "logs"


# --------------------------------------------------------------------------- portable mode

def test_portable_env_flag_writes_next_to_the_exe(frozen, monkeypatch):
    _bundle, _user, exe_dir = frozen
    monkeypatch.delenv("BIH_APP_DATA_DIR", raising=False)  # override would beat portable
    monkeypatch.setenv("BIH_PORTABLE", "1")
    assert ap.user_data_dir() == exe_dir / "data"
    assert ap.cases_root() == exe_dir / "data" / "cases"


def test_portable_sentinel_file_flips_to_beside_the_exe(frozen, monkeypatch):
    _bundle, _user, exe_dir = frozen
    monkeypatch.delenv("BIH_APP_DATA_DIR", raising=False)
    (exe_dir / ap.PORTABLE_SENTINEL).write_text("portable", encoding="utf-8")
    assert ap.user_data_dir() == exe_dir / "data"


def test_app_data_override_beats_portable(frozen, monkeypatch):
    _bundle, user, _exe = frozen
    monkeypatch.setenv("BIH_PORTABLE", "1")            # override (set by the fixture) still wins
    assert ap.user_data_dir() == user


# --------------------------------------------------------------------------- no-write-under-bundle guard

def test_no_runtime_write_path_targets_the_bundle(frozen):
    """The load-bearing invariant: every path the app WRITES to resolves outside bundle_dir()."""
    bundle = ap.bundle_dir()
    # The registry + settings + lock + logs + new cases all derive from these roots.
    for getter in (ap.user_data_dir, ap.app_data_dir, ap.cases_root, ap.settings_path, ap.logs_dir):
        p = getter()
        assert not Path(p).is_relative_to(bundle), f"{getter.__name__}() -> {p} is under the bundle!"


# --------------------------------------------------------------------------- keyring fallback (graceful)

def test_keyring_absent_fallback_is_graceful(monkeypatch):
    import keyring
    from keyring.backends import fail

    from backend.app.secrets import keyring_status

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    st = keyring_status()                       # must NOT crash
    assert st["available"] is False
    assert st["message"]                        # a clear per-OS message is surfaced


def test_configure_keyring_selects_a_real_backend_when_frozen(monkeypatch):
    """When frozen and discovery yielded the fail backend, configure_keyring explicitly selects the OS
    backend (on a host that HAS one). A host with none keeps the fail backend (graceful)."""
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    set_calls: list = []
    monkeypatch.setattr(keyring, "set_keyring", lambda kr: set_calls.append(kr))

    runtime.configure_keyring()
    if set_calls:                               # this machine has a viable backend (e.g. Windows)
        assert not isinstance(set_calls[0], fail.Keyring)


def test_configure_keyring_is_a_noop_in_source(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "set_keyring", lambda kr: pytest.fail("must not set a backend in source"))
    runtime.configure_keyring()                 # not frozen -> no-op, no crash


# --------------------------------------------------------------------------- certifi / TLS

def test_ca_bundle_is_a_real_certifi_path():
    bundle = runtime.ca_bundle()
    assert isinstance(bundle, str) and Path(bundle).exists()
    import certifi

    assert bundle == certifi.where()


def test_configure_tls_points_ssl_at_certifi(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    runtime.configure_tls()
    import os

    assert os.environ["SSL_CERT_FILE"] == runtime.ca_bundle()
    assert os.environ["REQUESTS_CA_BUNDLE"] == runtime.ca_bundle()


def test_offline_first_no_network_in_runtime_config(monkeypatch):
    """configure_frozen_runtime must require NO network (offline-first): make any socket attempt fail
    and assert it still completes."""
    import socket

    def _boom(*a, **k):
        raise AssertionError("startup configuration attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    runtime.configure_frozen_runtime()          # TLS + keyring config, no network


# --------------------------------------------------------------------------- review-hardening (P7)

def test_frozen_bundled_resources_read_through(frozen):
    """Beyond path math: materialize each bundled resource in the simulated bundle and confirm it READS
    back through resource_path() (the read-through a frozen app relies on). The ACTUAL --add-data
    presence in a real build is a P8 check; here we prove the resolution + read path is correct."""
    bundle, _user, _exe = frozen
    for rel in ap.BUNDLED_RESOURCES.values():
        p = ap.resource_path(rel)
        if p.suffix:                                 # a file resource (tokens.json / confidence.csv)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("frozen-resource", encoding="utf-8")
            assert p.read_text(encoding="utf-8") == "frozen-resource"
        else:                                        # a directory resource (dist / migrations / templates)
            p.mkdir(parents=True, exist_ok=True)
            assert p.is_dir()
        assert p.is_relative_to(bundle)              # still INSIDE the read-only bundle


def test_user_data_dir_is_created_on_first_use(tmp_path, monkeypatch):
    """user_data_dir() must materialize its (possibly deep) path on first call — a frozen app's first run
    has no pre-existing app-data dir."""
    target = tmp_path / "fresh" / "appdata"
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(target))
    assert not target.exists()
    d = ap.user_data_dir()
    assert d == target and d.is_dir()                # created on first use
    assert ap.logs_dir().is_dir() and ap.logs_dir().is_relative_to(target)


def test_real_writes_land_under_user_data_not_the_bundle(frozen):
    """Integration (not just path math): a real settings write goes through settings_path() and lands
    under user_data_dir(), never under the bundle."""
    from backend.app.services import settings_store

    bundle, user, _exe = frozen
    settings_store.set_offline(True)                 # a real file write via the runtime path
    try:
        sp = ap.settings_path()
        assert sp.exists() and sp.read_text(encoding="utf-8")   # actually wrote there
        assert sp.is_relative_to(user) and not sp.is_relative_to(bundle)
    finally:
        settings_store.set_offline(False)


def test_cases_root_env_override_wins_even_when_frozen(frozen, monkeypatch, tmp_path):
    custom = tmp_path / "custom_cases"
    monkeypatch.setenv("BIH_CASES_ROOT", str(custom))   # the dev/test override beats the frozen default
    assert ap.cases_root() == custom


def test_configure_tls_skips_a_missing_ca_bundle(tmp_path, monkeypatch):
    """A misconfigured build (cacert.pem not collected) must NOT point SSL at a nonexistent file or
    crash — httpx keeps its own default verification."""
    import os

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setattr(runtime, "ca_bundle", lambda: str(tmp_path / "nope" / "cacert.pem"))
    runtime.configure_tls()                          # must not crash
    assert os.environ.get("SSL_CERT_FILE") is None   # never set to a path that doesn't exist
