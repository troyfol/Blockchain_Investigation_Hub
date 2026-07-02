"""Import connector base (Phase 7; docs/connectors.md §6).

Ingests a human-exported file (no scraping — Invariant #1). The file itself is stored as the
``source_query.raw_response`` (hashed → ``raw_response_hash``), so provenance holds (Invariant #3):
every imported claim references the import's ``source_query``. Bespoke per-tool parsers subclass
this; swapping an importer for that tool's future API connector is a drop-in (same capability,
same canonical output).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from ..base import ConnectorError
from ...db import repository as repo
from ...db.repository import utc_now_iso
from ...models import Address, SourceQuery
from ...normalization.canonical import canonical_address
from ...provenance.atomic import write_with_provenance

# SEC-15: cap an imported CSV so a hostile/huge file can't exhaust memory if the import is ever exposed
# via a route. Generous for a real Arkham/GraphSense export.
MAX_CSV_BYTES = 64 * 1024 * 1024   # 64 MiB
MAX_CSV_ROWS = 1_000_000


def parse_float(v):
    if v is None or str(v).strip() == "":
        return None
    return float(v)


def known_case_addresses(conn) -> dict[tuple[str, str], str]:
    """``{(chain, canonical_address): address_id}`` for addresses ALREADY in the case. Used so an intel
    run (P8.7.1 #1) enriches ONLY pre-existing addresses — it must never INJECT a snapshot's other
    addresses (e.g. the bundled OFAC/GraphSense entries) into an unrelated case, which would surface a
    false 'this case contains a sanctioned/attributed entity' in the report + DB."""
    return {(r["chain"], r["address"]): r["id"]
            for r in conn.execute("SELECT id, chain, address FROM address").fetchall()}


class ImportConnector:
    name = "import"
    source = "import"

    def __init__(self, *, settings=None):
        self.settings = settings

    @staticmethod
    def read_csv(raw_bytes: bytes) -> list[dict]:
        """Decode + materialize a CSV import. SEC-09/SEC-15: a hostile CSV (null byte / oversized field /
        wrong encoding / over-cap size) raises a clean ``ConnectorError`` with a hint — never a raw
        ``csv.Error``/``UnicodeDecodeError`` traceback."""
        if len(raw_bytes) > MAX_CSV_BYTES:
            raise ConnectorError(
                f"CSV import is {len(raw_bytes)} bytes, over the {MAX_CSV_BYTES} cap — refusing to import.")
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConnectorError(f"CSV import is not valid UTF-8 (offset {exc.start}): {exc.reason}.") from exc
        try:
            rows = []
            for i, row in enumerate(csv.DictReader(io.StringIO(text))):
                if i >= MAX_CSV_ROWS:
                    raise ConnectorError(f"CSV import exceeds the {MAX_CSV_ROWS}-row cap — refusing to import.")
                rows.append(row)
            return rows
        except csv.Error as exc:
            raise ConnectorError(f"malformed CSV import: {exc}.") from exc

    @staticmethod
    def _resolve_address(c, sqid, chain: str, display: str, known: dict | None):
        """Map a snapshot address to a case ``address_id``. When ``known`` is provided (intel scoping,
        P8.7.1 #1) return the EXISTING id or ``None`` (skip — never inject a new address); when ``None``
        (direct file import, the validation flow) upsert it as before."""
        if known is None:
            return repo.upsert_address(c, Address(chain=chain, address_display=display), sqid)
        try:
            canon = canonical_address(chain, display)
        except ValueError:
            return None
        return known.get((chain, canon))

    def _ingest(self, conn, *, file_path, capability: str, parse, now: str | None = None,
                extra_params: dict | None = None, endpoint: str | None = None):
        """Read the export file, write its bytes as provenance, and parse into canonical rows.

        ``extra_params`` is merged into the recorded ``source_query.params`` (the ``bounds`` key is
        always preserved) — e.g. a GraphSense ``!include`` manifest (each included file's name + hash)
        so the provenance is reproducible even though ``raw_response`` is only the top-level file.
        ``endpoint`` overrides the recorded ``source_query.endpoint`` (default = the file name) — e.g.
        the OFAC SDN publication date, so the provenance records *which edition* of a mutable list this
        is (sanctions change between fetches)."""
        now = now or utc_now_iso()
        raw_bytes = Path(file_path).read_bytes()
        params = {"file": str(file_path), "bounds": "default"}
        if extra_params:
            params.update(extra_params)
        sq = SourceQuery(
            connector=self.name, capability=capability, endpoint=endpoint or Path(file_path).name,
            params=params, requested_at=now, completed_at=now, status="ok")
        _, result = write_with_provenance(
            conn, sq, lambda c, sqid: parse(c, sqid, raw_bytes, now), raw_response=raw_bytes)
        return result
