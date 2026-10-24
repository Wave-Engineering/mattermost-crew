"""Story 1.1: the workspace's packages import, and the Mattermost fixture skips cleanly."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

INTEGRATION_CONFTEST = Path(__file__).resolve().parents[1] / "integration" / "conftest.py"


@pytest.mark.parametrize("module", ["crewchat", "crewbroker", "crewchat_clients"])
def test_packages_import(module: str) -> None:
    mod = importlib.import_module(module)
    assert mod.__version__ == "0.1.0"


def test_mattermost_fixture_skips_without_env(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MM_URL", raising=False)
    monkeypatch.delenv("MM_ADMIN_TOKEN", raising=False)
    pytester.makeconftest(INTEGRATION_CONFTEST.read_text())
    pytester.makepyfile(
        test_needs_server="""
        def test_needs_server(mattermost):
            raise AssertionError("must not run without MM_URL")
        """
    )
    result = pytester.runpytest("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*MM_URL and MM_ADMIN_TOKEN are unset*"])


def test_mattermost_fixture_reads_env(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MM_URL", "http://mm.invalid:8065/")
    monkeypatch.setenv("MM_ADMIN_TOKEN", "t0ken")
    monkeypatch.setenv("MM_NATIVE_TOKEN_EXPIRY", "absent")
    pytester.makeconftest(INTEGRATION_CONFTEST.read_text())
    pytester.makepyfile(
        test_reads_env="""
        def test_reads_env(mattermost):
            assert mattermost.url == "http://mm.invalid:8065"
            assert mattermost.api == "http://mm.invalid:8065/api/v4"
            assert mattermost.admin_token == "t0ken"
            assert mattermost.expected_native_token_expiry == "absent"
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
