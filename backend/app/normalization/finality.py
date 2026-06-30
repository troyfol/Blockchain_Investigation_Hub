"""Finality computation (Phase 1, docs/algorithms.md §2).

``confirmations = max(0, tip_height - block_height + 1)`` (0 if unconfirmed); a transaction is
``final`` once ``confirmations >= threshold(chain)``, else ``provisional`` (Invariant #6).
Recompute on every fetch; provisional rows may be upserted/deleted, final rows are frozen.
"""

from __future__ import annotations

from typing import Literal

FinalityStatus = Literal["provisional", "final"]


def compute_confirmations(tip_height: int | None, block_height: int | None) -> int:
    """Confirmations for a tx at ``block_height`` given chain ``tip_height``.

    Unconfirmed (mempool) or unknown tip → 0.
    """
    if block_height is None or tip_height is None:
        return 0
    return max(0, tip_height - block_height + 1)


def compute_finality(confirmations: int, threshold: int) -> FinalityStatus:
    return "final" if confirmations >= threshold else "provisional"


def finality_for(
    *, tip_height: int | None, block_height: int | None, threshold: int
) -> tuple[int, FinalityStatus]:
    """Return ``(confirmations, finality_status)`` for the given heights and chain threshold."""
    confirmations = compute_confirmations(tip_height, block_height)
    return confirmations, compute_finality(confirmations, threshold)
