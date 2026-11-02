"""THROWAWAY (#215): a deliberately failing test, to prove `ci-gate` blocks a red PR.

This file exists only on a PR that is closed unmerged. It must never land on main.
"""


def test_deliberately_red() -> None:
    raise AssertionError("throwaway: proves the required check blocks a red PR (#215)")
