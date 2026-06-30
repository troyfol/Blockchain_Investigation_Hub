"""Property tests for canonicalization (docs/testing.md §4)."""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from backend.app.normalization.canonical import canonical_address

evm_address = st.text(alphabet="0123456789abcdefABCDEF", min_size=40, max_size=40).map(
    lambda body: "0x" + body
)


@pytest.mark.property
@given(evm_address)
def test_canonical_is_idempotent(addr):
    once = canonical_address("ethereum", addr)
    assert canonical_address("ethereum", once) == once


@pytest.mark.property
@given(evm_address)
def test_case_variants_canonicalize_equal(addr):
    # EVM checksum vs lowercase vs uppercase are the SAME address.
    assert (
        canonical_address("ethereum", addr.lower())
        == canonical_address("ethereum", addr.upper())
        == canonical_address("ethereum", addr)
    )
