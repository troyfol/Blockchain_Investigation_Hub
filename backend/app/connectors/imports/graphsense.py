"""GraphSense TagPack import — the free attribution pillar (Phase A; docs/findings/
graphsense_tagpack_reconciliation.md, docs/connectors.md §6).

GraphSense TagPacks are free, open (MIT), public-source attribution tags in YAML, designed for
*provenance-aware* sharing — they map almost one-to-one onto BIH's `attribution` + `source_query`
spine (Invariants #1/#3/#4). This connector ingests a YAML TagPack from a local clone/download of the
public repo (`github.com/graphsense/graphsense-tagpacks`, `packs/`) — a **structured import of public
data** (Invariant #1, no scraping). It fills the `attribution`/`entity_membership` capability that has
had no correct producer since the Arkham re-scope.

Pipeline: `_load_doc` (I/O) reads the YAML and resolves `header: !include other.yaml`; the pure
`adapt_tagpack`/`adapt_actorpack` adapters do header→tag inheritance + the canonical mapping; the
connector writes via the repository. The TagPack file's bytes are stored as the import's
`source_query.raw_response` (hashed), so provenance holds (Invariant #3).

Capabilities:
  - `get_attributions` (Phase A): tags -> `attribution` (idempotent on `(address,label,source,note)`).
  - `get_risk`        (Phase B): tags carrying `abuse` -> categorical `risk_assessment` (score=None).
  - `get_entities`    (Phase C): ActorPacks -> `entity`; tag `actor` refs -> `entity_membership`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

import yaml

from ...db import repository as repo
from ...models import Address, Attribution, EntityMembership, RiskAssessment
from ...normalization.graphsense_adapter import adapt_actorpack, adapt_tagpack
from ..base import ConnectorError
from .base import ImportConnector, known_case_addresses


class _Include:
    """Sentinel for an unresolved ``!include <path>`` node (resolved against the file's dir)."""

    __slots__ = ("target",)

    def __init__(self, target: str):
        self.target = target


class _TagPackLoader(yaml.SafeLoader):
    """SafeLoader + the TagPack-specific ``!include`` tag. SafeLoader still rejects any other
    custom/Python tag, so an untrusted pack can't execute code (Invariant #1 — public data, but
    parsed defensively)."""


# An unquoted EVM address (``0x`` + hex) would otherwise resolve to a YAML hex *integer*, mangling the
# address (and losing leading zeros). Give the subclass its OWN resolver table (copy — never mutate the
# shared SafeLoader one) and make ``0x…`` resolve to a string with priority, so addresses survive whether
# the pack quotes them or not. BTC/base58 addresses (1…/3…/L…) and all other scalars are unaffected.
if "yaml_implicit_resolvers" not in _TagPackLoader.__dict__:
    _TagPackLoader.yaml_implicit_resolvers = {
        ch: rs[:] for ch, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()}
_TagPackLoader.yaml_implicit_resolvers.setdefault("0", []).insert(
    0, ("tag:yaml.org,2002:str", re.compile(r"^0[xX][0-9a-fA-F]+$")))

# add_constructor copies yaml_constructors onto the subclass first, so this !include tag does not leak
# into the global SafeLoader registry.
_TagPackLoader.add_constructor("!include", lambda loader, node: _Include(loader.construct_scalar(node)))

_MAX_INCLUDE_DEPTH = 8  # guard against an include cycle / runaway chain


def _is_unsafe_include(target: str) -> bool:
    """An ``!include`` target must stay within the pack tree: reject absolute paths (POSIX *or*
    Windows-drive) and any ``..`` component. Data enters only from the LOCAL pack clone (Invariant #1
    — structured import of public data, not an arbitrary-local-file read). Mirrors the export
    connector's untrusted-file-ref hardening."""
    t = str(target)
    norm = PurePosixPath(t.replace("\\", "/"))
    return norm.is_absolute() or Path(t).is_absolute() or ".." in norm.parts


class GraphSenseImporter(ImportConnector):
    name = "graphsense-import"
    source = "graphsense"

    def capabilities(self) -> set[str]:
        return {"get_attributions", "get_risk", "get_entities"}

    # --- YAML load + !include resolution (I/O — kept out of the pure adapter) -----------------

    def _load_doc(self, path: Path) -> tuple[dict, list]:
        """Return ``(resolved_doc, include_manifest)``. The manifest records each ``!include``d file's
        name + SHA-256 so provenance is reproducible even though ``raw_response`` is only the top file."""
        includes: list[dict] = []
        doc = self._load_yaml(path, depth=0, includes=includes)
        if not isinstance(doc, dict):
            raise ConnectorError(
                f"GraphSense file {path.name!r} is not a YAML mapping (got {type(doc).__name__}).")
        return doc, includes

    def _load_yaml(self, path: Path, *, depth: int, includes: list):
        if depth > _MAX_INCLUDE_DEPTH:
            raise ConnectorError(f"GraphSense !include nesting too deep (>{_MAX_INCLUDE_DEPTH}) at {path.name!r}")
        if not path.exists():
            raise ConnectorError(f"GraphSense include target not found: {path}")
        raw = path.read_bytes()
        try:
            node = yaml.load(raw, Loader=_TagPackLoader)  # noqa: S506 (custom SafeLoader)
        except yaml.YAMLError as exc:
            raise ConnectorError(f"GraphSense YAML parse error in {path.name!r}: {exc}") from exc
        if depth > 0:  # an included file — capture its provenance (name + hash) so it's reproducible
            includes.append({"file": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
        return self._resolve_includes(node, path.parent, depth, includes)

    def _resolve_includes(self, node, base_dir: Path, depth: int, includes: list):
        if isinstance(node, _Include):
            if _is_unsafe_include(node.target):
                raise ConnectorError(
                    f"GraphSense unsafe !include target {node.target!r}: must be a relative path within "
                    f"the pack (no absolute paths or '..').")
            return self._load_yaml(base_dir / node.target, depth=depth + 1, includes=includes)
        if isinstance(node, dict):
            return {k: self._resolve_includes(v, base_dir, depth, includes) for k, v in node.items()}
        if isinstance(node, list):
            return [self._resolve_includes(v, base_dir, depth, includes) for v in node]
        return node

    @staticmethod
    def _include_params(includes: list) -> dict | None:
        return {"includes": includes} if includes else None

    # --- Phase A: attributions ---------------------------------------------------------------

    def get_attributions(self, conn, file_path, *, now=None, only_known_addresses=False) -> dict:
        """Ingest a TagPack's tags as `attribution` rows. Idempotent re-ingest (Invariant #7). With
        ``only_known_addresses`` (the intel-enrichment path, P8.7.1 #1) ONLY addresses already in the case
        are enriched — a snapshot's other addresses are never injected."""
        doc, includes = self._load_doc(Path(file_path))
        return self._ingest(conn, file_path=file_path, capability="get_attributions",
                            parse=lambda c, sqid, _raw, n: self._write_attributions(c, sqid, doc, n, only_known_addresses),
                            now=now, extra_params=self._include_params(includes))

    @staticmethod
    def _raise_on_errors(notes: dict) -> None:
        """A malformed tag on a *supported* chain fails the whole import (all-or-nothing), so a corrupt
        pack errors loudly rather than writing a partial set or raising a raw traceback."""
        if notes["errors"]:
            first = notes["errors"][0]
            raise ConnectorError(
                f"GraphSense TagPack has {len(notes['errors'])} unparseable tag(s); first at tag "
                f"#{first['tag']}: {first['reason']}. Nothing was imported.")

    @staticmethod
    def _skip_report(notes: dict) -> dict:
        return {"skipped_unsupported": len(notes["skipped_unsupported"]),
                "unsupported_currencies": sorted(
                    {s["currency"] for s in notes["skipped_unsupported"]}),
                "unknown_confidence": notes["unknown_confidence"],
                "abuse_tags": notes["abuse_tags"], "actor_tags": notes["actor_tags"]}

    def _write_attributions(self, c, sqid, doc, now, only_known=False) -> dict:
        tags, notes = adapt_tagpack(doc)
        self._raise_on_errors(notes)
        known = known_case_addresses(c) if only_known else None
        n = 0
        for t in tags:
            addr_id = self._resolve_address(c, sqid, t.chain, t.address_display, known)
            if addr_id is None:
                continue  # intel scoping: this snapshot address is not in the case -> skip (never inject)
            repo.upsert_attribution(c, Attribution(
                address_id=addr_id, label=t.label, category=t.category, source=self.source,
                confidence=t.confidence, note=t.note, retrieved_at=now), sqid)
            n += 1
        return {"attributions": n, **self._skip_report(notes)}

    # --- Phase B: abuse -> categorical risk (free risk bonus) --------------------------------

    def get_risk(self, conn, file_path, *, now=None, only_known_addresses=False) -> dict:
        """A tag carrying an `abuse` type also writes a CATEGORICAL `risk_assessment` (score=None) —
        a free, partial risk signal. No numeric score is invented (Invariant #4 — never synthesize).
        ``only_known_addresses`` scopes enrichment to addresses already in the case (P8.7.1 #1)."""
        doc, includes = self._load_doc(Path(file_path))
        return self._ingest(conn, file_path=file_path, capability="get_risk",
                            parse=lambda c, sqid, _raw, n: self._write_risk(c, sqid, doc, n, only_known_addresses),
                            now=now, extra_params=self._include_params(includes))

    def _write_risk(self, c, sqid, doc, now, only_known=False) -> dict:
        tags, notes = adapt_tagpack(doc)
        self._raise_on_errors(notes)
        known = known_case_addresses(c) if only_known else None
        n = 0
        for t in tags:
            if not t.abuse:
                continue
            addr_id = self._resolve_address(c, sqid, t.chain, t.address_display, known)
            if addr_id is None:
                continue
            rationale = f"{t.label} — {t.source_backlink}" if t.source_backlink else t.label
            repo.upsert_risk_assessment(c, RiskAssessment(
                address_id=addr_id, score=None, score_scale=None, category=t.abuse,
                source=self.source, rationale=rationale, retrieved_at=now), sqid)
            n += 1
        return {"risks": n, **self._skip_report(notes)}

    # --- Phase C: actors -> entities + memberships -------------------------------------------

    def get_entities(self, conn, file_path, *, now=None, only_known_addresses=False) -> dict:
        """Ingest the entity graph from a GraphSense YAML, dispatching on file shape:
          - an **ActorPack** (`actors:`) -> `entity` rows (origin='source', idempotent on the actor id);
          - a **TagPack** (`tags:`) -> `entity_membership` rows for tags that carry an `actor` ref
            (method='tagpack-actor'), creating/resolving the actor's entity by id so order doesn't matter.
        ``only_known_addresses`` (intel path, P8.7.1 #1) scopes memberships+entities to addresses already
        in the case — so a TagPack never injects an entity for an address the case doesn't have.
        """
        path = Path(file_path)
        doc, includes = self._load_doc(path)
        if "actors" in doc:
            parse = lambda c, sqid, _raw, n: self._write_actors(c, sqid, doc, n)  # noqa: E731
        elif "tags" in doc:
            parse = lambda c, sqid, _raw, n: self._write_memberships(c, sqid, doc, n, only_known_addresses)  # noqa: E731
        else:
            raise ConnectorError(
                f"GraphSense file {path.name!r} has neither `actors:` (ActorPack) nor `tags:` (TagPack).")
        return self._ingest(conn, file_path=path, capability="get_entities", parse=parse, now=now,
                            extra_params=self._include_params(includes))

    def _write_actors(self, c, sqid, doc, now) -> dict:
        actors, notes = adapt_actorpack(doc)
        if notes["errors"]:
            first = notes["errors"][0]
            raise ConnectorError(
                f"GraphSense ActorPack has {len(notes['errors'])} unparseable actor(s); first at actor "
                f"#{first['actor']}: {first['reason']}. Nothing was imported.")
        created = updated = 0
        for a in actors:
            _, was_created = repo.find_or_create_source_entity(
                c, external_id=a.external_id, name=a.name, entity_type=a.entity_type, now=now)
            created += was_created
            updated += not was_created
        return {"actors": len(actors), "entities_created": created, "entities_resolved": updated}

    def _write_memberships(self, c, sqid, doc, now, only_known=False) -> dict:
        tags, notes = adapt_tagpack(doc)
        self._raise_on_errors(notes)
        known = known_case_addresses(c) if only_known else None
        n = 0
        for t in tags:
            if not t.actor:
                continue
            addr_id = self._resolve_address(c, sqid, t.chain, t.address_display, known)
            if addr_id is None:
                continue  # intel scoping: skip an address the case doesn't have (no injected entity)
            # Resolve (or stub) the actor's entity by id; seed entity_type from the tag's category per
            # the spec mapping (actor -> Entity(entity_type=category)). A later ActorPack's authoritative
            # categories[0] still wins (find_or_create only fills entity_type when currently empty).
            entity_id, _ = repo.find_or_create_source_entity(
                c, external_id=t.actor, name=t.actor, entity_type=t.category, now=now)
            flags = "cluster-definer" if t.is_cluster_definer else None
            repo.upsert_entity_membership(c, EntityMembership(
                entity_id=entity_id, address_id=addr_id, source=self.source, method="tagpack-actor",
                confidence=t.confidence, flags=flags), sqid, now=now)
            n += 1
        return {"memberships": n, **self._skip_report(notes)}
