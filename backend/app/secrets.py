"""Secret storage (phase_00 step 3).

API keys live in the OS keyring. The single exception is a **loud, explicit dev
opt-in**: when ``BIH_ALLOW_PLAINTEXT_KEYS=1`` a secret may be read from the
``BIH_SECRET_<NAME>`` environment variable instead — and every such read logs a
SECURITY warning. There is no other plaintext path, and plaintext is never written.
"""

from __future__ import annotations

import logging
import os
import sys

import keyring

logger = logging.getLogger("bih.secrets")

SERVICE = "blockchain-investigation-hub"
PLAINTEXT_ENV_FLAG = "BIH_ALLOW_PLAINTEXT_KEYS"


def _plaintext_allowed() -> bool:
    return os.environ.get(PLAINTEXT_ENV_FLAG) == "1"


def keyring_status() -> dict:
    """Report the OS keyring backend availability (CONFIRM-FIRST, keyring 25.7).

    A usable backend = Windows Credential Manager / macOS Keychain / Linux SecretService. When NONE is
    present (e.g. headless Linux with no Secret Service), ``keyring`` falls back to a ``fail`` backend
    whose every call raises — surface that as a clear, per-OS message instead of letting a key write die
    obscurely. ``plaintext_active`` reflects the loud ``BIH_ALLOW_PLAINTEXT_KEYS=1`` dev opt-in. NEVER
    returns any secret value.
    """
    from keyring.backends import fail

    kr = keyring.get_keyring()
    available = not isinstance(kr, fail.Keyring) and getattr(kr, "priority", 1) > 0
    backend = f"{type(kr).__module__}.{type(kr).__name__}"
    message = None
    if not available:
        if sys.platform == "win32":
            message = "No Windows Credential Manager backend is available; API keys cannot be stored."
        elif sys.platform == "darwin":
            message = "No macOS Keychain backend is available; API keys cannot be stored."
        else:
            message = ("No Secret Service keyring backend is available — run a keyring daemon "
                       "(gnome-keyring / KWallet). A headless fallback is a later milestone (P7).")
    return {"backend": backend, "available": available,
            "plaintext_active": _plaintext_allowed(), "message": message}


def _plaintext_env_name(name: str) -> str:
    return f"BIH_SECRET_{name.upper()}"


def get_secret(name: str) -> str | None:
    """Return secret ``name`` from the keyring, or the plaintext env opt-in if enabled.

    Resolution order:
      1. If ``BIH_ALLOW_PLAINTEXT_KEYS=1`` and ``BIH_SECRET_<NAME>`` is set → use it
         (and log a loud SECURITY warning).
      2. Otherwise → OS keyring (``None`` if absent).
    """
    if _plaintext_allowed():
        env_name = _plaintext_env_name(name)
        value = os.environ.get(env_name)
        if value is not None:
            logger.warning(
                "SECURITY: reading secret %r from plaintext env %s because %s=1. "
                "This is a development convenience only — do not use it for real keys.",
                name,
                env_name,
                PLAINTEXT_ENV_FLAG,
            )
            return value
    try:
        return keyring.get_password(SERVICE, name)
    except keyring.errors.KeyringError:
        # No USABLE backend (e.g. headless Linux / CI with no Secret Service): keyring's `fail` backend
        # RAISES on every call rather than returning None. Degrade to "no key stored" so a read can never
        # crash a request — /health -> paid_status -> get_secret must not 500 when the backend is absent.
        # keyring_status() surfaces the unavailable backend loudly; this keeps reads honest + non-fatal.
        return None


def set_secret(name: str, value: str) -> None:
    """Store secret ``name`` in the OS keyring. Plaintext is never written to disk."""
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    """Remove secret ``name`` from the OS keyring (no error if absent)."""
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass
