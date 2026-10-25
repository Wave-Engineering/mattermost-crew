"""Story 3.9: `scripts/smoke-devspace.sh` refuses any target it was not told is the dev space."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke-devspace.sh"


def run(**env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in ("MM_URL", "MM_ADMIN_TOKEN")}
    base.pop("SMOKE_TARGET", None)
    return subprocess.run(
        ["bash", str(SCRIPT)], env={**base, **env}, capture_output=True, text=True, timeout=30
    )


def test_needs_the_server() -> None:
    result = run()
    assert result.returncode != 0
    assert "MM_URL" in result.stderr


def test_refuses_without_the_devspace_statement() -> None:
    result = run(MM_URL="https://mm.invalid", MM_ADMIN_TOKEN="t")
    assert result.returncode == 2
    assert "SMOKE_TARGET=oaw-devspace" in result.stderr
    assert "CP-03" in result.stderr


@pytest.mark.parametrize("target", ["prod", "devspace", "OAW-DEVSPACE"])
def test_refuses_any_other_target(target: str) -> None:
    result = run(MM_URL="https://mm.invalid", MM_ADMIN_TOKEN="t", SMOKE_TARGET=target)
    assert result.returncode == 2


def test_local_target_must_be_localhost() -> None:
    result = run(MM_URL="https://mm.invalid", MM_ADMIN_TOKEN="t", SMOKE_TARGET="local")
    assert result.returncode == 2
    assert "localhost" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "https://mm.invalid",
        "http://chat.apps.oakai.waveeng.com",
        "https://chat.apps.oakai.waveeng.com.evil.invalid",
        "https://evil.invalid/chat.apps.oakai.waveeng.com",
        "https://xchat.apps.oakai.waveeng.com",
    ],
)
def test_devspace_target_is_pinned_to_its_host(url: str, tmp_path: Path) -> None:
    """#99: `oaw-devspace` with any other server stops before a single request, so the role
    patch can never reach a partner's production server on the statement alone (CP-03)."""
    marker = tmp_path / "requested"
    shim = tmp_path / "curl"
    shim.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    shim.chmod(0o755)
    path = f"{tmp_path}:{os.environ['PATH']}"
    result = run(MM_URL=url, MM_ADMIN_TOKEN="t", SMOKE_TARGET="oaw-devspace", PATH=path)
    assert result.returncode == 2
    assert "MM_URL=https://chat.apps.oakai.waveeng.com" in result.stderr
    assert not marker.exists()


def test_devspace_url_passes_the_pin(tmp_path: Path) -> None:
    """The dev space itself (a trailing `/` is dropped) gets past the pin, to the server check."""
    marker = tmp_path / "requested"
    shim = tmp_path / "curl"
    shim.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    shim.chmod(0o755)
    path = f"{tmp_path}:{os.environ['PATH']}"
    url = "https://chat.apps.oakai.waveeng.com/"
    result = run(MM_URL=url, MM_ADMIN_TOKEN="t", SMOKE_TARGET="oaw-devspace", PATH=path)
    assert result.returncode == 2
    assert "no answer from" in result.stderr  # past the pin: the shim refused the request
    assert marker.exists()


def test_only_the_devspace_allows_a_remote_role_patch() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count("BROKER_ROLE_PATCH_ALLOW_REMOTE=1") == 1
    gate = text.index('if [[ "$allow_remote" == 1 ]]; then')
    assert gate < text.index("export BROKER_ROLE_PATCH_ALLOW_REMOTE=1") < text.index("fi", gate)


def test_runs_every_e2e_flow() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for flow in ("test_cross_crew.py", "test_outsider_switch.py", "test_broadcast.py"):
        assert f"tests/e2e/{flow}" in text, flow
