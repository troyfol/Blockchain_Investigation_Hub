"""Case export + verification (phase_10).

A case folder is self-contained: ``case.db`` plus the provenance/exhibit/report files it references
by RELATIVE path (``raw_responses/``, ``exhibits/``, ``reports/``) and the audit baseline sidecar
(``.audit_baselines/`` — tamper-evidence that must travel with the case). Export computes a SHA-256
``manifest.json`` over every such file and zips the folder to ``<case>.casefile``. The final-immutability
baseline ALSO rides INSIDE ``case.db`` as an append-only ``audit_baseline`` anchor (P27/FN-19) — it is
hashed as part of ``case.db`` in the manifest, so it travels and is tamper-evident with everything else.

Re-open verification (Invariants: self-contained, cache-never-a-runtime-dependency, tamper-evident):
1. every manifest file is present and its hash matches; no unlisted file slipped in;
2. the DB opens with NO attached database other than ``main`` (no shared-cache dependency);
3. every DB-referenced file (``raw_response_ref`` / ``exhibit.file_ref`` / ``report
   .rendered_file_ref``) is a SAFE relative path that resolves to a file inside the bundle;
4. all provenance FKs resolve within the bundle (``foreign_key_check`` empty + the no-dangling-fk
   audit passes).

**Threat model — read this.** This is tamper-EVIDENCE, not tamper-PROOFING. Verification detects any
change to an *intact* bundle: a modified/removed file (hash mismatch / missing) or an injected file
(unlisted extra). It does NOT defend against an adversary who rewrites a file AND its manifest entry
in tandem — a plain hash manifest cannot, and adding a hash-of-the-manifest does not help (the same
adversary rewrites that too). Tamper-PROOFING needs a signature or external notarization, a named
deferred item (see README "Trust model"). The strong check is: re-export on a trusted machine and
compare the manifest. Verification of an UNTRUSTED ``.casefile`` is hardened against path-escape
(``..`` / absolute refs) so a malicious bundle cannot make verification probe outside its own folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

MANIFEST_NAME = "manifest.json"
CASE_DB_NAME = "case.db"
# Directories whose every file is hashed into the manifest and shipped in the bundle.
HASHED_SUBDIRS = ("raw_responses", "exhibits", "reports", ".audit_baselines")

# SEC-05/SEC-16: caps + per-member checks for extracting an UNTRUSTED `.casefile` (a bundle is
# attacker-controlled). Generous for a real case (a large case.db + raw_responses) but far below a
# decompression bomb. The size ceiling is enforced against ACTUAL bytes during inflation (a lying header
# can't get past it), and each member's path/symlink is validated before any write.
MAX_CASEFILE_MEMBERS = 100_000
MAX_CASEFILE_BYTES = 2 * 1024 ** 3      # 2 GiB total uncompressed ceiling
MAX_MEMBER_RATIO = 5000                 # per-member compression ratio cap (bomb guard)


def _reject_unsafe_member(zi: "zipfile.ZipInfo", dest: Path) -> None:
    """SEC-16: reject a zip member that is a symlink, or whose name is absolute / drive-qualified /
    escapes ``dest`` via ``..`` — before it is written. Defense-in-depth over the stdlib's own
    sanitization (which a future extractor/Python change could weaken)."""
    name = zi.filename
    if (zi.external_attr >> 16) & 0o170000 == 0o120000:  # S_IFLNK
        raise ValueError(f"casefile contains a symlink member ({name!r}) — refusing to extract")
    if name.startswith(("/", "\\")) or (len(name) >= 2 and name[1] == ":"):
        raise ValueError(f"casefile member has an absolute/drive-qualified path ({name!r})")
    resolved = (dest / name).resolve()
    if resolved != dest.resolve() and dest.resolve() not in resolved.parents:
        raise ValueError(f"casefile member escapes the extraction dir ({name!r})")


def _safe_extract(z: zipfile.ZipFile, dest: Path) -> None:
    """SEC-05/SEC-16: validate members (count / declared-size / ratio / path-safety) then extract
    member-by-member with a running byte budget so a decompression bomb or a lying header can't inflate
    past the ceiling. Raises ``ValueError`` on any breach (the caller cleans up + rejects the bundle)."""
    infos = z.infolist()
    if len(infos) > MAX_CASEFILE_MEMBERS:
        raise ValueError(f"casefile has too many members ({len(infos)} > {MAX_CASEFILE_MEMBERS})")
    if sum(i.file_size for i in infos) > MAX_CASEFILE_BYTES:
        raise ValueError("casefile declares more than the extraction size ceiling")
    for zi in infos:
        _reject_unsafe_member(zi, dest)
        if zi.compress_size > 0 and zi.file_size / zi.compress_size > MAX_MEMBER_RATIO:
            raise ValueError(f"casefile member {zi.filename!r} has a suspicious compression ratio")
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for zi in infos:
        target = dest / zi.filename
        if zi.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(zi) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1 << 16)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_CASEFILE_BYTES:
                    raise ValueError("casefile exceeds the extraction size ceiling during inflation")
                out.write(chunk)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_shippable(f: Path) -> bool:
    return f.is_file() and not f.name.endswith(".tmp") and f.name != MANIFEST_NAME


def _is_unsafe_ref(ref: str) -> bool:
    """A DB-stored file reference must be a relative in-bundle path — reject absolute or ``..``.

    Our writers only ever store safe relative refs; this guards the UNTRUSTED-bundle path so a
    crafted ``.casefile`` cannot make verification touch files outside the case folder.
    """
    if not ref:
        return False
    p = Path(ref)
    return p.is_absolute() or ".." in p.parts or ref.startswith(("/", "\\"))


def _iter_case_files(case_dir: Path):
    """Yield every shippable file in the case folder: case.db + the hashed subdirs."""
    db = case_dir / CASE_DB_NAME
    if db.exists():
        yield db
    for sub in HASHED_SUBDIRS:
        d = case_dir / sub
        if d.exists():
            for f in sorted(d.rglob("*")):
                if _is_shippable(f):
                    yield f


def build_manifest(case_dir) -> dict:
    """SHA-256 of every shippable file, keyed by its POSIX relative path (deterministic order)."""
    case_dir = Path(case_dir)
    if not (case_dir / CASE_DB_NAME).exists():
        raise FileNotFoundError(f"no {CASE_DB_NAME} in {case_dir}")
    files = {}
    for f in _iter_case_files(case_dir):
        files[f.relative_to(case_dir).as_posix()] = _sha256(f)
    return {"manifest_version": 1, "algo": "sha256", "case_db": CASE_DB_NAME,
            "file_count": len(files), "files": dict(sorted(files.items()))}


def write_manifest(case_dir) -> Path:
    case_dir = Path(case_dir)
    manifest = build_manifest(case_dir)
    path = case_dir / MANIFEST_NAME
    # RES-04: temp-then-rename so a crash mid-write never leaves a truncated/desynced manifest.json.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _checkpoint_wal(case_dir: Path) -> None:
    """Flush any committed WAL into ``case.db`` so the bundled file is COMPLETE even if another
    connection (e.g. the desktop app) still has the case open. The DB runs in WAL mode, so without this
    a recently-written-but-uncheckpointed row lives only in the ``-wal`` sidecar (which is not shipped) —
    the export would then be silently incomplete and re-verification would fail. Best-effort: a locked or
    missing DB is left as-is (the manifest still hashes whatever the file holds)."""
    db = case_dir / CASE_DB_NAME
    if not db.exists():
        return
    import sqlite3
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # locked/corrupt -> don't block export; the bundle hashes the file as it stands


def _scrub_export_pii(case_dir: Path) -> None:
    """Privacy: yoyo records the OS **username + hostname** of whoever applied each migration in its
    ``_yoyo_log`` audit table. That is investigator-identifying data with no investigative value, and it
    would otherwise travel inside every exported ``.casefile`` handed to a colleague/court. Clear it on
    export. Migration STATE lives in ``_yoyo_migration`` (left untouched), so an imported case still
    forward-migrates idempotently. Best-effort: a locked/old DB without the table is left as-is."""
    db = case_dir / CASE_DB_NAME
    if not db.exists():
        return
    import sqlite3
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='_yoyo_log'").fetchone():
                conn.execute("DELETE FROM _yoyo_log")
                conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # never block an export over the audit-log scrub


def export_case(case_dir, *, out_path=None) -> Path:
    """Write ``manifest.json`` then zip the folder to ``<case>.casefile``. Returns the bundle path."""
    case_dir = Path(case_dir)
    _scrub_export_pii(case_dir)  # strip yoyo's OS username/hostname audit log BEFORE hashing (privacy)
    _checkpoint_wal(case_dir)  # make case.db self-complete before hashing/zipping (export robustness)
    write_manifest(case_dir)
    out_path = Path(out_path) if out_path else case_dir.parent / f"{case_dir.name}.casefile"
    # RES-04: build the bundle at a temp path and atomically rename it into place only after a clean
    # close, so a crash mid-zip never leaves a TRUNCATED `.casefile` at the expected name (which a
    # colleague could copy before re-verifying). On failure, clean up the partial temp.
    tmp_out = out_path.with_name(out_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(case_dir / MANIFEST_NAME, MANIFEST_NAME)
            for f in _iter_case_files(case_dir):
                z.write(f, f.relative_to(case_dir).as_posix())
        os.replace(tmp_out, out_path)
    except BaseException:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise
    return out_path


# --------------------------------------------------------------------------- verification

def verify_manifest(root_dir) -> dict:
    """Recompute hashes under ``root_dir`` and compare to its ``manifest.json``."""
    root = Path(root_dir)
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    listed = manifest["files"]
    missing, mismatched = [], []
    for rel, expected in listed.items():
        f = root / rel
        if not f.exists():
            missing.append(rel)
        elif _sha256(f) != expected:
            mismatched.append(rel)
    # An unlisted shippable file = tampering (something added after the manifest was sealed).
    present = {f.relative_to(root).as_posix() for f in _iter_case_files(root)}
    extra = sorted(present - set(listed))
    ok = not (missing or mismatched or extra)
    return {"ok": ok, "missing": missing, "mismatched": mismatched, "extra": extra,
            "file_count": len(listed)}


def _verify_db_self_contained(case_db: Path) -> dict:
    """Open the case DB and confirm it stands alone (no cache dep; provenance resolves in-bundle)."""
    from ..audits.baselines import anchor_present
    from ..audits.runner import run_audits
    from ..db import get_connection

    conn = get_connection(case_db)
    try:
        attached = [name for _seq, name, _file in conn.execute("PRAGMA database_list").fetchall()
                    if name != "main"]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        # P27/FN-19: confirm the in-DB final-immutability anchor rode along in case.db. Informational
        # (a pre-P27 bundle legitimately has none) — it does NOT gate `ok`; the audit re-run below is
        # what proves tamper-evidence. Lets the import UI tell an anchored case from an older one.
        final_anchor_present = anchor_present(conn, "final-immutability")
        # Collect every file the DB references. A NULL ref is legitimate: locally-computed
        # source_queries (co-spend clustering, valuation, FIFO, same-address) have no external raw
        # response — provenance is the source_query row itself, not a stored payload (Invariant #3).
        refs: list[str] = []
        for sql in ("SELECT raw_response_ref AS ref FROM source_query WHERE raw_response_ref IS NOT NULL",
                    "SELECT file_ref AS ref FROM exhibit WHERE file_ref IS NOT NULL",
                    "SELECT rendered_file_ref AS ref FROM report WHERE rendered_file_ref IS NOT NULL"):
            refs.extend(r["ref"] for r in conn.execute(sql).fetchall())

        unsafe_refs = sorted({ref for ref in refs if _is_unsafe_ref(ref)})
        missing_refs = sorted({ref for ref in refs
                               if not _is_unsafe_ref(ref) and not (case_db.parent / ref).exists()})
    finally:
        conn.close()

    # Re-run all invariant audits to validate the re-opened case end-to-end — this also re-checks
    # the shipped baselines (final-immutability #4, append-only-claims #6) for regression.
    audit_results = run_audits(db_path=str(case_db))
    audit_ok = all(a.passed for a in audit_results)
    failed_audits = [a.name for a in audit_results if not a.passed]
    return {
        "ok": not attached and not fk_violations and not missing_refs and not unsafe_refs and audit_ok,
        "attached_databases": attached,        # must be empty -> no shared-cache dependency
        "fk_violations": len(fk_violations),
        "missing_referenced_files": missing_refs,
        "unsafe_referenced_paths": unsafe_refs,  # absolute / .. refs in an untrusted bundle
        "audits_passed": audit_ok,
        # The specific failing audit(s) — lets the import UI distinguish + NAME an authentic-but-drifted
        # case's invariant warnings (e.g. a post-finality 'final-immutability' drift) from real tampering.
        "failed_audits": failed_audits,
        # Whether the in-DB append-only final-immutability anchor (P27/FN-19) traveled in this bundle.
        "final_anchor_present": final_anchor_present,
    }


def verify_casefile(casefile_path, *, extract_to=None) -> dict:
    """Extract a ``.casefile`` and fully validate it (hashes + DB self-containment). Returns a report."""
    casefile_path = Path(casefile_path)
    if extract_to is None:
        import tempfile
        extract_to = Path(tempfile.mkdtemp(prefix="casefile_"))
    extract_to = Path(extract_to)
    # SEC-05/SEC-16: bounded, path-safe extraction BEFORE any hash/manifest check runs. On breach, clean
    # up the partial extraction and reject the bundle (a ValueError the import routes surface as a 400).
    try:
        with zipfile.ZipFile(casefile_path, "r") as z:
            _safe_extract(z, extract_to)
    except (ValueError, zipfile.BadZipFile) as exc:
        shutil.rmtree(extract_to, ignore_errors=True)
        raise ValueError(f"refusing to import this .casefile: {exc}") from exc

    manifest_check = verify_manifest(extract_to)
    db_check = _verify_db_self_contained(extract_to / CASE_DB_NAME)
    return {
        "ok": manifest_check["ok"] and db_check["ok"],
        "extracted_to": str(extract_to),
        "manifest": manifest_check,
        "self_contained": db_check,
    }
