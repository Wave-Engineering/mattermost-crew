"""The CI leg's server is up, is the image the leg names, and is set up as the dev space."""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.integration.mmhttp import mm_request

# `mattermost` is conftest.MattermostServer; conftest is not importable as a module.


def test_server_answers_ping(mattermost: Any) -> None:
    resp = mm_request(mattermost, "GET", "/system/ping", timeout=10)
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"


def test_server_version_matches_leg(mattermost: Any) -> None:
    image = os.environ.get("MM_IMAGE", "")
    if ":" not in image:
        pytest.skip("MM_IMAGE is unset, so there is no leg version to compare")
    tag = image.rsplit(":", 1)[1]
    resp = mm_request(mattermost, "GET", "/system/ping", timeout=10)
    # X-Version-Id reads e.g. "11.11.3.<build>.<hash>.<flag>"; the release is its prefix.
    assert resp.headers["X-Version-Id"].startswith(f"{tag}.")


def test_admin_token_is_system_admin(mattermost: Any) -> None:
    resp = mm_request(mattermost, "GET", "/users/me", timeout=10)
    assert resp.status_code == 200
    assert "system_admin" in resp.json()["roles"].split()


def test_native_token_expiry_is_detected(mattermost: Any) -> None:
    """The leg's expectation of native token expiry matches what the server shows (R-07, CT-11).

    11.11 returns `expires_at` on a new access token; 11.8 does not. The broker's capability
    probe (Story 1.3) keys on the same field, so this keeps the 11.8 leg honest about it.
    """
    expected = mattermost.expected_native_token_expiry
    if expected is None:
        pytest.skip("MM_NATIVE_TOKEN_EXPIRY is unset; CI sets it on every leg")
    me = mm_request(mattermost, "GET", "/users/me", timeout=10)
    assert me.status_code == 200, me.text
    resp = mm_request(
        mattermost,
        "POST",
        f"/users/{me.json()['id']}/tokens",
        json={"description": "native expiry probe"},
        timeout=10,
    )
    assert resp.status_code == 200
    token = resp.json()
    try:
        detected = "present" if "expires_at" in token else "absent"
        assert detected == expected
    finally:
        mm_request(
            mattermost,
            "POST",
            "/users/tokens/revoke",
            json={"token_id": token["id"]},
            timeout=10,
        )


def test_dev_space_settings_are_applied(mattermost: Any) -> None:
    resp = mm_request(mattermost, "GET", "/config", timeout=10)
    assert resp.status_code == 200
    config = resp.json()
    assert config["ServiceSettings"]["EnableBotAccountCreation"] is True
    assert config["ServiceSettings"]["EnableUserAccessTokens"] is True
    limits = config["RateLimitSettings"]
    # The dev space's limits (CT-06): per user, 10/s with a burst of 50.
    assert limits["Enable"] is True
    assert limits["VaryByUser"] is True
    assert limits["PerSec"] == 10
    assert limits["MaxBurst"] == 50
