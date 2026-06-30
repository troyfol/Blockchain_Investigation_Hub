"""Property tests: FIFO apportionment conserves value (docs/algorithms.md §6; phase_08).

These run against the pure ``fifo_apportion`` — conservation is a property of the algorithm, so it
is tested where it lives (the schema stores the linkage, not the apportioned amount).
"""

from __future__ import annotations

from collections import defaultdict

import pytest
from hypothesis import given, strategies as st

from backend.app.services.tracing import fifo_apportion

amts = st.lists(st.integers(min_value=1, max_value=10 ** 12), min_size=1, max_size=8)


def _keyed(prefix, values):
    return [(f"{prefix}{i}", v) for i, v in enumerate(values)]


@pytest.mark.property
@given(in_amts=amts, out_amts=amts)
def test_fifo_conserves(in_amts, out_amts):
    total_in, total_out = sum(in_amts), sum(out_amts)
    # FIFO apportions inputs to outputs in order; only meaningful when inputs cover outputs (rest=fee).
    if total_in < total_out:
        return
    inputs, outputs = _keyed("i", in_amts), _keyed("o", out_amts)
    links = fifo_apportion(inputs, outputs)

    assert all(amt > 0 for _, _, amt in links)                 # no zero/negative links

    into = defaultdict(int)
    outof = defaultdict(int)
    for in_k, out_k, amt in links:
        into[out_k] += amt
        outof[in_k] += amt

    in_amt = dict(inputs)
    out_amt = dict(outputs)
    for out_k, need in out_amt.items():
        assert into[out_k] == need                              # each output fully funded
    for in_k, have in in_amt.items():
        assert outof[in_k] <= have                              # never over-spend an input
    assert sum(amt for _, _, amt in links) == total_out         # total linked == total outputs


@pytest.mark.property
@given(out_amts=amts)
def test_no_inputs_means_no_links(out_amts):
    assert fifo_apportion([], _keyed("o", out_amts)) == []


def test_rejects_negative_amounts():
    with pytest.raises(ValueError):
        fifo_apportion([("i0", -5)], [("o0", 5)])
