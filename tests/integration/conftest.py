"""The Mattermost fixture every integration test uses.

`scripts/ci/mattermost-up.sh` starts a throwaway server and exports `MM_URL` and
`MM_ADMIN_TOKEN`. Without them, every test that asks for `mattermost` skips, with a reason
that says how to get a server, so a plain `make test` on a laptop stays green.

`MM_NATIVE_TOKEN_EXPIRY` is set per CI leg (`absent` on the 11.8 leg, CT-11). A test that
needs native token expiry must not skip when the server lacks it: it asserts that the absence
is *detected*, by comparing its runtime probe with `expected_native_token_expiry` (R-07).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import pytest

from tests.integration.mmhttp import mm_request

__all__ = ["MattermostServer", "mattermost_from_env", "mm_request"]

NativeTokenExpiry = Literal["absent", "present"]

SKIP_REASON = (
    "no Mattermost server: MM_URL and MM_ADMIN_TOKEN are unset "
    "(start one with scripts/ci/mattermost-up.sh, then source .mattermost.env)"
)


@dataclass(frozen=True)
class MattermostServer:
    """A running Mattermost server and a system admin's personal access token."""

    url: str
    # Kept out of the repr: a failing test prints its fixtures into the junit report.
    admin_token: str = field(repr=False)
    expected_native_token_expiry: NativeTokenExpiry | None

    @property
    def api(self) -> str:
        return f"{self.url}/api/v4"

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}


def mattermost_from_env(environ: Mapping[str, str]) -> MattermostServer:
    """Build the server description from the environment, or skip the calling test."""
    url = environ.get("MM_URL", "").strip()
    token = environ.get("MM_ADMIN_TOKEN", "").strip()
    if not url or not token:
        pytest.skip(SKIP_REASON)
    expiry = environ.get("MM_NATIVE_TOKEN_EXPIRY", "").strip() or None
    if expiry not in (None, "absent", "present"):
        raise pytest.UsageError(
            f"MM_NATIVE_TOKEN_EXPIRY must be 'absent' or 'present', not {expiry!r}"
        )
    return MattermostServer(
        url=url.rstrip("/"),
        admin_token=token,
        expected_native_token_expiry=expiry,  # type: ignore[arg-type]
    )


@pytest.fixture(scope="session")
def mattermost() -> MattermostServer:
    return mattermost_from_env(os.environ)
