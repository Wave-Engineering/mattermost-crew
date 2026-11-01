"""IT-27 (#146): `crewchat-send` posts from a script as the checkout's registered agent.

The daemon is wired as in IT-15 (`test_chat_contract.Host`: the registry, the Discord adapter
over the scream-hole and Discord REST fake, `build_handlers` plus `build_chat_handlers`) on a
real unix socket. The agent's session is the real `Shim`, which registers it. `crewchat-send`
runs as a real subprocess in that agent's checkout, with the message on STDIN, as a kit page
script runs it. A second checkout, with no session, gets exit 4 and sends nothing.

It needs no Mattermost and always runs. The Mattermost path is the same daemon `send` op the
shim already drives in IT-05 and IT-16.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from crewchat_clients.send import EXIT_NOT_REGISTERED, EXIT_SENT

from tests.fakes.screamhole import FakeDiscord
from tests.integration.test_chat_contract import (  # noqa: F401 (`sock_dir` is a fixture)
    Host,
    make_repo,
    make_shim,
    sock_dir,
)


async def crewchat_send(repo: Path, sock: Path, text: str, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "crewchat_clients.send",
        *args,
        "--socket",
        str(sock),
        cwd=repo,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(text.encode())
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


def test_it27_crewchat_send_posts_as_the_registered_agent(
    tmp_path: Path,
    sock_dir: Path,  # noqa: F811 (the imported fixture)
) -> None:
    """IT-27 (#146)."""
    discord = FakeDiscord()
    chan = discord.add_channel("call-4-bj")
    sock = sock_dir / "crewchat.sock"
    host = Host(tmp_path, discord)

    async def scenario() -> None:
        async with host.serving(sock):
            repo = make_repo(tmp_path, home_backend="discord")
            shim, _ = make_shim(repo, sock, tmp_path)
            await shim.start()  # the session registers the agent; the script never does
            assert shim.backends() == ["discord"]

            page = "babelfish: pane %3 never revived; see `reseed-kick.log`\n"
            code, out, err = await crewchat_send(repo, sock, page, "#call-4-bj")
            assert (code, err) == (EXIT_SENT, "")
            stored = discord.messages[chan][-1]
            assert stored["id"] == out.strip()
            assert stored["content"].startswith(
                "babelfish: pane %3 never revived; see `reseed-kick.log`\n\n— **shimmy**"
            )

            stray = make_repo(tmp_path / "other", dev="nobody-here", home_backend="discord")
            before = len(discord.messages[chan])
            code, out, err = await crewchat_send(stray, sock, "should not post\n", "call-4-bj")
            assert code == EXIT_NOT_REGISTERED
            assert out == ""
            assert "no registered session for nobody-here" in err
            assert "should not post" not in err
            assert len(discord.messages[chan]) == before

    asyncio.run(scenario())
