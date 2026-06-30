"""Unit tests for union-find (docs/algorithms.md §4)."""

from __future__ import annotations

from backend.app.services.entities import UnionFind


def test_transitive_clustering():
    uf = UnionFind()
    for a, b in [("1", "2"), ("3", "4"), ("2", "3")]:  # 1-2, 3-4, then 2-3 bridges all
        uf.union(a, b)
    assert len({uf.find(str(i)) for i in (1, 2, 3, 4)}) == 1


def test_separate_clusters_stay_separate():
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("x", "y")
    assert uf.find("a") == uf.find("b") != uf.find("x") == uf.find("y")
    groups = {frozenset(g) for g in uf.groups()}
    assert groups == {frozenset({"a", "b"}), frozenset({"x", "y"})}


def test_deterministic_survivor_is_min_id():
    uf = UnionFind()
    uf.union("b", "a")
    uf.union("c", "b")
    assert uf.find("a") == uf.find("b") == uf.find("c") == "a"  # min id is the root


def test_same_cluster_iff_transitively_cospent():
    uf = UnionFind()
    edges = [("w", "x"), ("x", "y")]  # w-x-y connected; z isolated
    for a, b in edges:
        uf.union(a, b)
    uf.find("z")
    assert uf.find("w") == uf.find("y")
    assert uf.find("z") != uf.find("w")
