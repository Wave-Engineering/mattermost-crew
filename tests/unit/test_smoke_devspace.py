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


def test_runs_every_e2e_flow() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for flow in ("test_cross_crew.py", "test_outsider_switch.py", "test_broadcast.py"):
        assert f"tests/e2e/{flow}" in text, flow
