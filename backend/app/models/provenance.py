"""Provenance models — the spine every fact/claim references (Invariant #3)."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    return str(uuid4())


class SourceQuery(BaseModel):
    """A record of one external call (or import). Facts/claims FK to this, written in the
    same transaction. ``params`` is a dict here (serialized to JSON on write) and MUST include
    applied expansion bounds for address-scoped capabilities (audit #10)."""

    id: str = Field(default_factory=_new_id)
    connector: str
    capability: str
    endpoint: str
    params: dict | None = None
    requested_at: str
    completed_at: str | None = None
    status: Literal["ok", "error", "partial"] = "ok"
    result_summary: str | None = None
    # raw_response_ref / raw_response_hash are set by the writer from the raw bytes.


class Exhibit(BaseModel):
    id: str = Field(default_factory=_new_id)
    exhibit_type: Literal["screenshot", "file", "export"]
    source: str | None = None
    captured_at: str
    file_ref: str
    content_hash: str
    description: str | None = None
