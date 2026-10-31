"""`crewchat-send` (#146): one message from STDIN, as the checkout's registered agent."""

from __future__ import annotations

import io
import json
import socket
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from crewchat_clients.send import (
    DAEMON_MAX_FRAME_BYTES,
    EXIT_CANT_EDIT,
    EXIT_ERROR,
    EXIT_NO_DEFAULT,
    EXIT_NOT_REGISTERED,
    EXIT_SENT,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    parser,
    run,
)

DEV = "sender"
POST_ID = "p" * 26
MM = "mattermost:ci"

Responder = Callable[[dict[str, Any]], dict[str, Any]]


class FakeDaemon:
    """Answers each frame with `respond(frame)`, one frame per connection."""

    def __init__(self, path: Path, respond: Responder) -> None:
        self.respond = respond
        self.frames: list[dict[str, Any]] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                frame = json.loads(buf)
                self.frames.append(frame)
                conn.sendall(json.dumps(self.respond(frame)).encode() + b"\n")

    def close(self) -> None:
        self.sock.close()


def posted(frame: dict[str, Any]) -> dict[str, Any]:
    return {"id": frame["id"], "ok": True, "result": {"backend": MM, "post_id": POST_ID}}


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def short_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="cc-send-", dir="/tmp") as d:
        yield Path(d)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    write_identity(tmp_path, {"dev_name": DEV, "home_backend": MM})
    return tmp_path


def write_identity(root: Path, identity: dict[str, Any]) -> None:
    (root / ".claude" / "agent-identity.json").write_text(json.dumps(identity))


@pytest.fixture
def daemon(short_dir: Path) -> Iterator[tuple[Path, FakeDaemon]]:
    path = short_dir / "d.sock"
    fake = FakeDaemon(path, posted)
    yield path, fake
    fake.close()


def send(
    repo: Path, sock: Path, args: list[str], stdin: str | io.StringIO = "hello\n"
) -> tuple[int, str, str]:
    ns = parser().parse_args([*args, "--socket", str(sock), "--repo-root", str(repo)])
    out, err = io.StringIO(), io.StringIO()
    source = stdin if isinstance(stdin, io.StringIO) else io.StringIO(stdin)
    code = run(ns, source, out, err, defaults_file=repo / "no-defaults.json")
    return code, out.getvalue(), err.getvalue()


def test_send_reads_text_from_stdin_only(repo: Path, daemon: tuple[Path, FakeDaemon]) -> None:
    sock, fake = daemon
    code, _, err = send(repo, sock, ["precheck", "text", "in", "argv"])
    assert code == EXIT_USAGE
    assert "STDIN" in err
    assert fake.frames == []

    message = "run `make ci` and $(not this)\nsecond line\n"
    code, out, _ = send(repo, sock, ["precheck"], stdin=message)
    assert code == EXIT_SENT
    assert out.strip() == POST_ID
    (frame,) = fake.frames
    assert frame["op"] == "send"
    assert frame["args"] == {
        "dev_name": DEV,
        "backend": MM,
        "to": "precheck",
        "text": "run `make ci` and $(not this)\nsecond line",  # one trailing newline stripped
    }


def test_send_refuses_an_empty_message_and_a_terminal(
    repo: Path, daemon: tuple[Path, FakeDaemon]
) -> None:
    sock, fake = daemon
    assert send(repo, sock, ["precheck"], stdin="  \n")[0] == EXIT_USAGE
    assert send(repo, sock, ["precheck"], stdin=Tty("hello"))[0] == EXIT_USAGE
    assert fake.frames == []


def test_send_uses_default_backend_and_never_asks(
    repo: Path, daemon: tuple[Path, FakeDaemon]
) -> None:
    sock, fake = daemon
    write_identity(repo, {"dev_name": DEV})  # no home_backend, and no defaults file entry
    code, out, err = send(repo, sock, ["precheck"])
    assert code == EXIT_NO_DEFAULT
    assert out == ""
    assert str(repo.resolve()) in err  # the key the human would record a default under
    assert fake.frames == []

    (repo / "no-defaults.json").write_text(json.dumps({str(repo.resolve()): "discord"}))
    assert send(repo, sock, ["precheck"])[0] == EXIT_SENT
    assert fake.frames[-1]["args"]["backend"] == "discord"


def test_send_named_backend_is_marked_off_default(
    repo: Path, daemon: tuple[Path, FakeDaemon]
) -> None:
    sock, fake = daemon
    assert send(repo, sock, ["precheck", "--backend", "discord"])[0] == EXIT_SENT
    assert fake.frames[-1]["args"]["backend"] == "discord"
    assert fake.frames[-1]["args"]["off_default"] is True
    assert send(repo, sock, ["precheck", "--backend", MM])[0] == EXIT_SENT
    assert "off_default" not in fake.frames[-1]["args"]
    assert send(repo, sock, ["precheck", "--backend", "slack"])[0] == EXIT_USAGE


def test_send_malformed_default_spares_a_named_backend_and_a_thread(
    repo: Path, short_dir: Path
) -> None:
    def respond(frame: dict[str, Any]) -> dict[str, Any]:
        if frame["op"] == "thread_backend":
            return {"id": frame["id"], "ok": True, "result": {"backend": MM}}
        return posted(frame)

    write_identity(repo, {"dev_name": DEV, "home_backend": "slack"})  # DefaultsError
    sock = short_dir / "m.sock"
    fake = FakeDaemon(sock, respond)
    try:
        assert send(repo, sock, ["precheck", "--backend", "discord"])[0] == EXIT_SENT
        assert fake.frames[-1]["args"]["backend"] == "discord"
        assert fake.frames[-1]["args"]["off_default"] is True
        assert send(repo, sock, ["precheck", "--thread", POST_ID])[0] == EXIT_SENT
        assert fake.frames[-1]["args"]["backend"] == MM
        code, _, err = send(repo, sock, ["precheck"])  # only the default path needs it
    finally:
        fake.close()
    assert code == EXIT_USAGE
    assert "home_backend 'slack'" in err


def test_send_ok_without_post_id_is_an_error(repo: Path, short_dir: Path) -> None:
    sock = short_dir / "o.sock"
    fake = FakeDaemon(sock, lambda f: {"id": f["id"], "ok": True, "result": {}})
    try:
        code, out, err = send(repo, sock, ["precheck"])
    finally:
        fake.close()
    assert (code, out) == (EXIT_ERROR, "")
    assert "no post ID" in err


def test_send_mismatched_reply_is_unreachable(repo: Path, short_dir: Path) -> None:
    sock = short_dir / "i.sock"
    fake = FakeDaemon(sock, lambda f: {**posted(f), "id": "someone-else"})
    try:
        code, out, err = send(repo, sock, ["precheck"])
    finally:
        fake.close()
    assert (code, out) == (EXIT_UNREACHABLE, "")
    assert "mismatched" in err


def test_send_refuses_a_frame_over_the_daemon_limit(
    repo: Path, daemon: tuple[Path, FakeDaemon]
) -> None:
    sock, fake = daemon
    code, out, err = send(repo, sock, ["precheck"], stdin="x" * DAEMON_MAX_FRAME_BYTES)
    assert (code, out) == (EXIT_USAGE, "")
    assert "message too large" in err
    assert fake.frames == []  # nothing reached the socket
    assert send(repo, sock, ["precheck"], stdin="x" * 60_000)[0] == EXIT_SENT


def test_send_dm_flag_uses_dm_op(repo: Path, daemon: tuple[Path, FakeDaemon]) -> None:
    sock, fake = daemon
    assert send(repo, sock, ["bakerb", "--dm"])[0] == EXIT_SENT
    assert fake.frames[-1]["op"] == "dm"
    assert fake.frames[-1]["args"]["to"] == "bakerb"
    assert send(repo, sock, ["bakerb", "--dm", "--thread", POST_ID])[0] == EXIT_USAGE


def test_send_thread_follows_its_backend(repo: Path, short_dir: Path) -> None:
    def respond(frame: dict[str, Any]) -> dict[str, Any]:
        if frame["op"] == "thread_backend":
            return {"id": frame["id"], "ok": True, "result": {"backend": "discord"}}
        return posted(frame)

    sock = short_dir / "t.sock"
    fake = FakeDaemon(sock, respond)
    try:
        ref = "123456789012345678/223456789012345678"
        assert send(repo, sock, ["general", "--thread", ref])[0] == EXIT_SENT
    finally:
        fake.close()
    lookup, sent = fake.frames
    assert lookup["op"] == "thread_backend"
    assert lookup["args"] == {"dev_name": DEV, "post_id": "223456789012345678"}
    assert sent["args"]["backend"] == "discord"
    assert sent["args"]["thread"] == "123456789012345678"


def test_send_edit_flag(repo: Path, short_dir: Path) -> None:
    """#149: `--edit <post_id>` sends STDIN as the `edit` op, on the post's own backend; a
    post that is gone or another agent's exits 6; `<to>`, `--dm` and `--thread` are usage."""
    errors: list[str] = []

    def respond(frame: dict[str, Any]) -> dict[str, Any]:
        if frame["op"] == "thread_backend":
            return {"id": frame["id"], "ok": True, "result": {"backend": MM}}
        if errors:
            return {"id": frame["id"], "ok": False, "error": errors.pop()}
        return posted(frame)

    sock = short_dir / "e.sock"
    fake = FakeDaemon(sock, respond)
    try:
        assert send(repo, sock, ["--edit", POST_ID], stdin="fixed\n") == (
            EXIT_SENT,
            POST_ID + "\n",
            "",
        )
        lookup, edit = fake.frames
        assert lookup["op"] == "thread_backend"
        assert edit["op"] == "edit"
        assert edit["args"] == {"dev_name": DEV, "backend": MM, "post_id": POST_ID, "text": "fixed"}
        for error in ("forbidden: mattermost:ci: not your post", "not_found: post"):
            errors.append(error)
            assert send(repo, sock, ["--edit", POST_ID])[0] == EXIT_CANT_EDIT
        sent = len(fake.frames)
        for extra in (["precheck"], ["--dm"], ["--thread", POST_ID]):
            assert send(repo, sock, [*extra, "--edit", POST_ID])[0] == EXIT_USAGE
        assert len(fake.frames) == sent
    finally:
        fake.close()


def test_send_edit_of_an_unknown_post_exits_6(repo: Path, short_dir: Path) -> None:
    """Naming no backend, a post the daemon can't place is gone: exit 6, no `edit` sent."""
    sock = short_dir / "u.sock"
    fake = FakeDaemon(sock, lambda f: {"id": f["id"], "ok": False, "error": "not_found: p"})
    try:
        assert send(repo, sock, ["--edit", POST_ID])[0] == EXIT_CANT_EDIT
    finally:
        fake.close()
    assert [f["op"] for f in fake.frames] == ["thread_backend"]


def test_send_edit_reads_the_error_code_not_its_text(repo: Path, short_dir: Path) -> None:
    """Only the daemon's own `not_found` code is exit 6: a `bad_request` whose text quotes
    "not_found" (the caller's ID, say) stays exit 1."""
    sock = short_dir / "q.sock"
    error = "bad_request: post_id 'x_not_found' is not an ID"
    fake = FakeDaemon(sock, lambda f: {"id": f["id"], "ok": False, "error": error})
    try:
        assert send(repo, sock, ["--edit", POST_ID])[0] == EXIT_ERROR
    finally:
        fake.close()


def test_send_not_registered_exits_4_without_registering(repo: Path, short_dir: Path) -> None:
    sock = short_dir / "n.sock"
    fake = FakeDaemon(
        sock, lambda f: {"id": f["id"], "ok": False, "error": "not_registered: sender"}
    )
    try:
        code, out, err = send(repo, sock, ["precheck"])
    finally:
        fake.close()
    assert code == EXIT_NOT_REGISTERED
    assert out == ""
    assert f"no registered session for {DEV}" in err
    assert [f["op"] for f in fake.frames] == ["send"]  # never a `register`


def test_send_daemon_error_exits_1_without_the_text(repo: Path, short_dir: Path) -> None:
    sock = short_dir / "e.sock"
    fake = FakeDaemon(sock, lambda f: {"id": f["id"], "ok": False, "error": "not_found: precheck"})
    try:
        code, _, err = send(repo, sock, ["precheck"], stdin="the secret text\n")
    finally:
        fake.close()
    assert code == EXIT_ERROR
    assert "not_found" in err
    assert "secret" not in err


def test_send_unreachable_daemon_exits_5(repo: Path, short_dir: Path) -> None:
    code, _, err = send(repo, short_dir / "absent.sock", ["precheck"])
    assert code == EXIT_UNREACHABLE
    assert "absent.sock" in err


def test_send_without_identity_is_a_usage_error(tmp_path: Path, short_dir: Path) -> None:
    assert send(tmp_path, short_dir / "x.sock", ["precheck"])[0] == EXIT_USAGE


def test_send_prints_post_id(repo: Path, daemon: tuple[Path, FakeDaemon]) -> None:
    sock, _ = daemon
    code, out, err = send(repo, sock, ["precheck"])
    assert (code, out, err) == (EXIT_SENT, POST_ID + "\n", "")


def test_help_lists_the_exit_codes() -> None:
    text = parser().format_help()
    for line in ("0  sent", "3  no default backend", "4  not registered", "5  daemon unreachable"):
        assert line in text


def test_pyproject_installs_crewchat_send() -> None:
    pyproject = Path(__file__).parents[3] / "packages" / "clients" / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
    assert scripts["crewchat-send"] == "crewchat_clients.send:main"
