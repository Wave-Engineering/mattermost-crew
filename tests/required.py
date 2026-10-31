"""A test that needs a host tool (#156). On a workstation without it, the test skips. On CI,
`CREWCHAT_REQUIRE_TEST_TOOLS=1` says the job set the tool up, so a missing tool fails the test
instead: a skip there would hide that the check never ran."""

from __future__ import annotations

import os
from typing import NoReturn

import pytest

REQUIRE_ENV = "CREWCHAT_REQUIRE_TEST_TOOLS"


def missing(tool: str) -> NoReturn:
    """Skip, or fail under `CREWCHAT_REQUIRE_TEST_TOOLS=1`, naming the tool."""
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{tool} is required here ({REQUIRE_ENV}=1)")
    pytest.skip(f"{tool} is not installed")
