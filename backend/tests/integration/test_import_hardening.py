"""Batch 8 (SEC-05 / SEC-16 / SEC-09 / SEC-15 / SEC-10): harden untrusted-bundle extraction and the
CSV import parser, plus the case-folder slug.

- SEC-05: `.casefile` extraction had no member-count/size/ratio cap and ran BEFORE hash verification —
  a zip bomb / manifest-of-huge-files could fill the disk. Must reject before/while inflating.
- SEC-16: reject absolute / `..` / drive-qualified / symlink members explicitly (defense-in-depth).
- SEC-09/SEC-15: a hostile CSV (null byte / oversized field / wrong encoding / over-cap) must raise a clean
  `ConnectorError`, not a raw `csv.Error`/`UnicodeDecodeError`.
- SEC-10: a case title that is a Windows reserved device name / very long must produce a safe slug.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from backend.app.connectors.base import ConnectorError
from backend.app.connectors.imports.base import ImportConnector
from backend.app.services import cases, export


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_sec05_oversized_casefile_rejected_before_inflating(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "MAX_CASEFILE_BYTES", 1000)  # tiny ceiling for the test
    bundle = tmp_path / "bomb.casefile"
    bundle.write_bytes(_zip_bytes({"case.db": b"A" * 50_000}))  # 50 KB > 1 KB ceiling
    dest = tmp_path / "extract"
    with pytest.raises(ValueError):
        export.verify_casefile(bundle, extract_to=dest)
    # Nothing (or nothing oversized) was left inflated in the cases area.
    assert not dest.exists() or not any(p.stat().st_size > 1000 for p in dest.rglob("*") if p.is_file())


def test_sec16_traversal_member_rejected(tmp_path):
    bundle = tmp_path / "evil.casefile"
    bundle.write_bytes(_zip_bytes({"../../evil": b"x", "case.db": b"ok"}))
    with pytest.raises(ValueError):
        export.verify_casefile(bundle, extract_to=tmp_path / "extract")


def test_sec16_symlink_member_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        zi = zipfile.ZipInfo("link")
        zi.external_attr = 0o120777 << 16  # S_IFLNK
        z.writestr(zi, "/etc/passwd")
        z.writestr("case.db", b"ok")
    bundle = tmp_path / "sym.casefile"
    bundle.write_bytes(buf.getvalue())
    with pytest.raises(ValueError):
        export.verify_casefile(bundle, extract_to=tmp_path / "extract")


def test_sec09_oversized_field_csv_raises_connector_error():
    # A field larger than csv's default 128 KiB limit raises csv.Error mid-iteration → clean ConnectorError.
    big = b"a,b\n1," + b"x" * 200_000 + b"\n"
    with pytest.raises(ConnectorError):
        ImportConnector.read_csv(big)


def test_sec09_utf16_csv_raises_connector_error():
    with pytest.raises(ConnectorError):
        ImportConnector.read_csv("a,b\n1,2\n".encode("utf-16"))


def test_sec15_oversized_csv_raises_connector_error(monkeypatch):
    from backend.app.connectors.imports import base as base_mod

    monkeypatch.setattr(base_mod, "MAX_CSV_BYTES", 100)
    with pytest.raises(ConnectorError):
        ImportConnector.read_csv(b"a,b\n" + b"1,2\n" * 1000)


def test_sec10_reserved_and_long_slug():
    # A Windows reserved device name must not survive as the whole slug.
    for reserved in ("CON", "NUL", "com1", "PRN", "aux", "lpt9"):
        s = cases._slug(reserved)
        assert s.lower() not in {"con", "nul", "com1", "prn", "aux", "lpt9"}, f"{reserved} -> {s}"
    long = cases._slug("x" * 500)
    assert 0 < len(long) <= 64, "slug component not length-capped (SEC-10)"
