"""Unit tests for keyring secret storage + the loud plaintext opt-in (phase_00 step 3)."""

from __future__ import annotations

import logging

import keyring
import pytest
from keyring.backend import KeyringBackend

from backend.app import secrets


class InMemoryKeyring(KeyringBackend):
    """A deterministic, offline keyring backend for tests."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    keyring.set_keyring(InMemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


def test_keyring_round_trip(memory_keyring):
    assert secrets.get_secret("etherscan") is None
    secrets.set_secret("etherscan", "ABC123")
    assert secrets.get_secret("etherscan") == "ABC123"
    secrets.delete_secret("etherscan")
    assert secrets.get_secret("etherscan") is None


def test_plaintext_requires_flag(memory_keyring, monkeypatch):
    # Env secret set, but the opt-in flag is OFF -> keyring path only (None here).
    monkeypatch.delenv(secrets.PLAINTEXT_ENV_FLAG, raising=False)
    monkeypatch.setenv("BIH_SECRET_ETHERSCAN", "from-env")
    assert secrets.get_secret("etherscan") is None


def test_plaintext_optin_logs_warning(memory_keyring, monkeypatch, caplog):
    monkeypatch.setenv(secrets.PLAINTEXT_ENV_FLAG, "1")
    monkeypatch.setenv("BIH_SECRET_ETHERSCAN", "from-env")
    with caplog.at_level(logging.WARNING, logger="bih.secrets"):
        value = secrets.get_secret("etherscan")
    assert value == "from-env"
    assert any("SECURITY" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records)
