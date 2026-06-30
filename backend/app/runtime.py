"""Frozen-runtime configuration (P7): TLS (certifi) + keyring backend selection.

A frozen app can't rely on the system cert store or on keyring's entry-point backend discovery, so this
module makes both work without a build:

  * **TLS** — point SSL/httpx at the bundled ``certifi`` CA bundle so HTTPS works with no system certs.
    ``ca_bundle()`` returns the path (httpx already defaults to certifi, but a frozen app can fail to ship
    ``cacert.pem`` unless it's collected — pointing ``verify`` + ``SSL_CERT_FILE`` at it is the robust
    belt-and-suspenders). This needs NO network — offline-first holds (launch never reaches out).
  * **keyring** — when frozen, explicitly select the OS backend if discovery yielded the ``fail`` backend
    (PyInstaller misses keyring's dynamic entry-point backends). Guarded: a host with no usable backend
    (e.g. headless Linux with no Secret Service) keeps the ``fail`` backend and ``secrets.keyring_status``
    reports it gracefully — never a crash.

CONFIRM-FIRST (CLAUDE.md §6). TODO: confirm at the P8 build that the keyring backend modules and certifi's
``cacert.pem`` are actually collected into the bundle. The P8 PyInstaller spec needs:
  * certifi: ``--collect-data certifi`` (ships ``cacert.pem``).
  * keyring backends, per target OS:
      Windows: ``--hidden-import keyring.backends.Windows`` (+ pywin32: ``win32ctypes`` is pulled in).
      macOS:   ``--hidden-import keyring.backends.macOS``.
      Linux:   ``--hidden-import keyring.backends.SecretService`` (+ ``secretstorage``, ``jeepney``),
               or ``--collect-submodules keyring.backends``.
"""

from __future__ import annotations

import functools
import os
import sys

from .app_paths import is_frozen


@functools.lru_cache(maxsize=1)
def ca_bundle() -> "str | bool":
    """The CA bundle path to hand httpx's ``verify=`` (the bundled certifi), or ``True`` (httpx's own
    default) if certifi can't be imported. Cached — certifi.where() is stable for the process."""
    try:
        import certifi

        return certifi.where()
    except Exception:  # pragma: no cover - certifi is an httpx dependency, but never hard-fail TLS setup
        return True


def configure_tls() -> None:
    """Point SSL/requests at the bundled certifi CA so a frozen app's HTTPS works with no system certs.
    Best-effort + ``setdefault`` (never clobber an operator's explicit override). No network."""
    bundle = ca_bundle()
    if isinstance(bundle, str) and os.path.exists(bundle):
        os.environ.setdefault("SSL_CERT_FILE", bundle)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


def configure_keyring() -> None:
    """When frozen, explicitly select the OS keyring backend if discovery produced the ``fail`` backend
    (PyInstaller doesn't see keyring's entry-point backends). No-op in source mode. Guarded so a host
    with no usable backend keeps the fail backend (``keyring_status`` reports it) instead of crashing."""
    if not is_frozen():
        return
    try:
        import keyring
        from keyring.backends import fail

        if not isinstance(keyring.get_keyring(), fail.Keyring):
            return  # discovery already found a real backend
        if sys.platform == "win32":
            from keyring.backends import Windows

            if getattr(Windows.WinVaultKeyring, "viable", False):
                keyring.set_keyring(Windows.WinVaultKeyring())
        elif sys.platform == "darwin":
            from keyring.backends import macOS

            if getattr(macOS.Keyring, "viable", False):
                keyring.set_keyring(macOS.Keyring())
        else:
            from keyring.backends import SecretService

            if getattr(SecretService.Keyring, "viable", False):
                keyring.set_keyring(SecretService.Keyring())
    except Exception:  # pragma: no cover - no viable/collected backend -> fail backend stays (reported)
        pass


def configure_frozen_runtime() -> None:
    """Idempotent startup hook: configure TLS + the keyring backend. Cheap in source mode (TLS env
    setdefault; keyring is a no-op unless frozen). Called from the app entrypoint + the launcher."""
    configure_tls()
    configure_keyring()
