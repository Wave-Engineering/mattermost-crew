"""Story 3.9: `scripts/smoke-devspace.sh` refuses any target it was not told is the dev space,
and (#157) `--cleanup <run>` deactivates only what that run recorded creating."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke-devspace.sh"


def run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in ("MM_URL", "MM_ADMIN_TOKEN")}
    base.pop("SMOKE_TARGET", None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=30,
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


# -- `--cleanup <run> [--yes]` (#157) ---------------------------------------------------------
RUN = "1a2b"


def mid(tag: str) -> str:
    """A Mattermost-shaped id: 26 lowercase alphanumerics."""
    return (tag + "0" * 26)[:26]


class FakeMattermost(ThreadingHTTPServer):
    """Users, bots and teams by id, and every request seen, as (method, path)."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.users: dict[str, dict[str, Any]] = {}
        self.teams: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.token = "t"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def user(self, tag: str, username: str, *, bot: bool = False) -> str:
        self.users[mid(tag)] = {"username": username, "is_bot": bot, "delete_at": 0}
        return mid(tag)

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]


class Handler(BaseHTTPRequestHandler):
    server: FakeMattermost

    def log_message(self, *args: Any) -> None:
        pass

    def _reply(self, status: int, body: dict[str, Any] | None = None) -> None:
        data = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        self.server.requests.append((self.command, self.path))
        if self.headers.get("Authorization") != f"Bearer {self.server.token}":
            return self._reply(401)
        m = re.fullmatch(r"/api/v4/(users|teams|bots)/([a-z0-9]{26})(/disable)?", self.path)
        if m is None:
            return self._reply(404)
        kind, id_, disable = m.groups()
        table = self.server.teams if kind == "teams" else self.server.users
        if id_ not in table:
            return self._reply(404)
        entry = table[id_]
        if self.command == "GET" and not disable:
            key = "name" if kind == "teams" else "username"
            return self._reply(200, {"id": id_, key: entry["username"], **entry})
        if (self.command, kind, disable) in (
            ("POST", "bots", "/disable"),
            ("DELETE", "users", None),
            ("DELETE", "teams", None),
        ):
            entry["delete_at"] = 1
            return self._reply(200, {"status": "OK"})
        return self._reply(405)

    do_GET = do_POST = do_DELETE = _handle


@pytest.fixture
def mm() -> Iterator[FakeMattermost]:
    server = FakeMattermost()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def cleanup(mm: FakeMattermost, reports: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(
        "--cleanup",
        *args,
        MM_URL=mm.url,
        MM_ADMIN_TOKEN="t",
        SMOKE_TARGET="local",
        SMOKE_REPORTS=str(reports),
    )


@pytest.fixture
def world(mm: FakeMattermost, tmp_path: Path) -> dict[str, str]:
    """Run 1a2b's accounts and team on the server and in its record, beside the traps: an
    account that matches the suffix but is not in the record, another run's, a crew agent's,
    and record entries that are not this run's name, renamed since, or gone."""
    ids = {
        "team": "t1",
        "partner-oaw1a2b": mm.user("u1", "partner-oaw1a2b"),
        "partner-synth1a2b": mm.user("u2", "partner-synth1a2b"),
        "outsider-e2e1a2b": mm.user("u3", "outsider-e2e1a2b"),
        "crewbroker-oaw1a2b-e2e": mm.user("u4", "crewbroker-oaw1a2b-e2e"),
        "x.oaw1a2b": mm.user("b1", "x.oaw1a2b", bot=True),
        "babel.oaw1a2b": mm.user("b2", "babel.oaw1a2b", bot=True),
        "kessel.oaw": mm.user("k1", "kessel.oaw", bot=True),
        "zed.synth1a2b": mm.user("r1", "renamed.oaw", bot=True),  # renamed since
        "quill.synth1a2b": mid("g1"),  # gone from the server
        "extra.oaw1a2b": mm.user("e1", "extra.oaw1a2b", bot=True),  # not in the record
        "partner-oaw9f9f": mm.user("o1", "partner-oaw9f9f"),  # another run's
    }
    mm.teams[mid("t1")] = {"username": "crew-e2e1a2b", "delete_at": 0}
    mm.teams[mid("t2")] = {"username": "crew-e2e9f9f", "delete_at": 0}  # another run's
    ids["team"] = mid("t1")
    ids["crew-e2e9f9f"] = mid("t2")
    created = [
        {"kind": "team", "id": ids["team"], "username": "crew-e2e1a2b"},
        {"kind": "team", "id": ids["crew-e2e9f9f"], "username": "crew-e2e9f9f"},
    ]
    for name in ("partner-oaw1a2b", "partner-synth1a2b", "outsider-e2e1a2b"):
        created.append({"kind": "user", "id": ids[name], "username": name})
    created.append(
        {"kind": "user", "id": ids["crewbroker-oaw1a2b-e2e"], "username": "crewbroker-oaw1a2b-e2e"}
    )
    for name in ("x.oaw1a2b", "babel.oaw1a2b", "kessel.oaw", "zed.synth1a2b", "quill.synth1a2b"):
        created.append({"kind": "bot", "id": ids[name], "username": name})
    record = {"run": RUN, "created": created}
    (tmp_path / f"smoke-{RUN}-created.json").write_text(json.dumps(record))
    return ids


ACTED = {
    "team": "archive",
    "partner-oaw1a2b": "deactivate",
    "partner-synth1a2b": "deactivate",
    "outsider-e2e1a2b": "deactivate",
    "crewbroker-oaw1a2b-e2e": "deactivate",
    "x.oaw1a2b": "disable",
    "babel.oaw1a2b": "disable",
}
SKIPPED = {
    "crew-e2e9f9f": "not this run's name",
    "kessel.oaw": "not this run's name",
    "zed.synth1a2b": "renamed",
    "quill.synth1a2b": "gone",
}


@pytest.mark.parametrize("bad", ["abc", "ABCD", "1a2b3"])
def test_cleanup_refuses_a_bad_run(mm: FakeMattermost, tmp_path: Path, bad: str) -> None:
    result = cleanup(mm, tmp_path, bad)
    assert result.returncode == 2 and "4 lowercase hex digits" in result.stderr
    assert mm.requests == []


def test_cleanup_yes_needs_cleanup() -> None:
    result = run("--yes", MM_URL="http://127.0.0.1:1", MM_ADMIN_TOKEN="t", SMOKE_TARGET="local")
    assert result.returncode == 2 and "--yes goes only with --cleanup" in result.stderr


def test_cleanup_keeps_the_target_guard(tmp_path: Path) -> None:
    """No target, or the dev-space target on another server: exit 2 before any request."""
    marker = tmp_path / "requested"
    shim = tmp_path / "curl"
    shim.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    shim.chmod(0o755)
    path = f"{tmp_path}:{os.environ['PATH']}"
    for env in (
        {"MM_URL": "http://127.0.0.1:1"},
        {"MM_URL": "https://mm.invalid", "SMOKE_TARGET": "oaw-devspace"},
    ):
        result = run("--cleanup", RUN, MM_ADMIN_TOKEN="t", PATH=path, **env)
        assert result.returncode == 2, (env, result.stderr)
    assert not marker.exists()


@pytest.mark.parametrize("record", [None, "{", json.dumps({"run": "9f9f", "created": []})])
def test_cleanup_refuses_without_a_record(
    mm: FakeMattermost, tmp_path: Path, record: str | None
) -> None:
    if record is not None:
        (tmp_path / f"smoke-{RUN}-created.json").write_text(record)
    result = cleanup(mm, tmp_path, RUN)
    assert result.returncode == 2
    if record is None:
        assert "no creation record" in result.stderr
    assert mm.requests == []


def test_cleanup_dry_run_writes_nothing(
    mm: FakeMattermost, tmp_path: Path, world: dict[str, str]
) -> None:
    result = cleanup(mm, tmp_path, RUN)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    would = {line.split()[3] for line in lines if line.startswith("would")}
    assert would == {"crew-e2e1a2b" if k == "team" else k for k in ACTED}
    for name, reason in SKIPPED.items():
        assert any(line.startswith("skip") and name in line and reason in line for line in lines)
    assert "extra.oaw1a2b" not in result.stdout and "partner-oaw9f9f" not in result.stdout
    assert mm.writes() == []
    assert "dry run" in lines[-1]


def test_cleanup_yes_deactivates_only_the_run(
    mm: FakeMattermost, tmp_path: Path, world: dict[str, str]
) -> None:
    result = cleanup(mm, tmp_path, RUN, "--yes")
    assert result.returncode == 0, result.stderr
    expected = {
        ("DELETE", f"/api/v4/teams/{world['team']}"),
        *(
            ("DELETE", f"/api/v4/users/{world[n]}")
            for n, verb in ACTED.items()
            if verb == "deactivate"
        ),
        *(("POST", f"/api/v4/bots/{world[n]}/disable") for n, v in ACTED.items() if v == "disable"),
    }
    assert set(mm.writes()) == expected and len(mm.writes()) == len(expected)
    assert mm.users[world["extra.oaw1a2b"]]["delete_at"] == 0
    assert mm.users[world["kessel.oaw"]]["delete_at"] == 0
    assert mm.users[world["zed.synth1a2b"]]["delete_at"] == 0
    assert mm.users[world["partner-oaw9f9f"]]["delete_at"] == 0
    assert mm.teams[world["crew-e2e9f9f"]]["delete_at"] == 0
    assert "7 acted on, 4 skipped, 0 failed" in result.stdout


def test_cleanup_keeps_the_token_off_curls_command_line(
    mm: FakeMattermost, tmp_path: Path, world: dict[str, str]
) -> None:
    """An argv is any local user's to read in /proc: the admin token goes to curl on stdin."""
    mm.token = "s3cr3tadmintoken"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv"
    real_curl = shutil.which("curl")
    shim = bin_dir / "curl"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{argv_log}"\nexec "{real_curl}" "$@"\n')
    shim.chmod(0o755)
    result = run(
        "--cleanup",
        RUN,
        "--yes",
        MM_URL=mm.url,
        MM_ADMIN_TOKEN=mm.token,
        SMOKE_TARGET="local",
        SMOKE_REPORTS=str(tmp_path),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
    )
    assert "7 acted on, 4 skipped, 0 failed" in result.stdout, result.stderr
    calls = argv_log.read_text()
    assert calls.count("\n") == len(mm.requests)  # every request went through the shim
    assert mm.token not in calls


def test_cleanup_skips_what_is_already_deactivated_or_the_wrong_kind(
    mm: FakeMattermost, tmp_path: Path, world: dict[str, str]
) -> None:
    mm.users[world["partner-oaw1a2b"]]["delete_at"] = 5
    mm.users[world["babel.oaw1a2b"]]["is_bot"] = False  # recorded as a bot
    result = cleanup(mm, tmp_path, RUN, "--yes")
    assert "partner-oaw1a2b" in result.stdout and "already deactivated" in result.stdout
    assert "kind mismatch" in result.stdout
    assert ("POST", f"/api/v4/bots/{world['babel.oaw1a2b']}/disable") not in mm.writes()
    assert ("DELETE", f"/api/v4/users/{world['partner-oaw1a2b']}") not in mm.writes()


def test_cleanup_reports_a_failed_write(
    mm: FakeMattermost, tmp_path: Path, world: dict[str, str]
) -> None:
    """A write the server refuses is counted, and the run exits 1."""
    original = Handler._handle

    def refuse_team_delete(self: Handler) -> None:
        if self.command == "DELETE" and "/teams/" in self.path:
            self.server.requests.append((self.command, self.path))
            return self._reply(500)
        return original(self)

    Handler.do_DELETE = refuse_team_delete  # type: ignore[method-assign]
    try:
        result = cleanup(mm, tmp_path, RUN, "--yes")
    finally:
        Handler.do_DELETE = original  # type: ignore[method-assign]
    assert result.returncode == 1
    assert "FAILED  archive team crew-e2e1a2b" in result.stdout and "1 failed" in result.stdout


def test_smoke_exports_its_run_suffix() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    pytest_at = text.index('uv run pytest "${FLOWS[@]}"')
    assert text.index('export E2E_RUN="$run" E2E_CREATED="$created"') < pytest_at
    refuse = text.index('[[ ! -e "$created" ]] || die')
    assert refuse < text.index("export E2E_RUN=")
    assert "**Run suffix:**" in text and "**Creation record:**" in text
    assert '"$run" "$created" <<' in text
