"""Spike 1.2 (#8), Dev Spec §5.N question 2: one upstream client per token, in one process.

Run it against the pinned upstream, not whatever resolves (D-090):

    source .mattermost.env   # MM_URL, MM_ADMIN_TOKEN from scripts/ci/mattermost-up.sh
    uv run --no-project --with mcp-server-mattermost==0.6.1 spikes/two_clients.py

Setup uses the admin token directly (two bot accounts, a token each, a team and a channel).
The experiment itself uses only the upstream library: two ``MattermostClient`` objects, each
built with a different bot token, post into the same channel, first one after the other on
their own connection pools, then concurrently while borrowing one shared ``httpx`` pool (how
the daemon would hold ~30 of them). Each post's ``user_id``, as the server reports it, must be
the bot whose client made it. Tokens are never printed.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import os
import secrets
import sys
from typing import Any

import httpx
from mcp_server_mattermost.client import MattermostClient
from mcp_server_mattermost.config import AuthMode, Settings
from mcp_server_mattermost.http_pool import create_http_client


def admin_call(admin: httpx.Client, method: str, path: str, body: Any = None) -> Any:
    r = admin.request(method, f"/api/v4{path}", json=body)
    r.raise_for_status()
    return r.json()


def setup(url: str, admin_token: str) -> dict[str, Any]:
    run = secrets.token_hex(3)
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {admin_token}"}) as admin:
        team = admin_call(
            admin,
            "POST",
            "/teams",
            {"name": f"two-{run}", "display_name": f"two {run}", "type": "O"},
        )
        chan = admin_call(
            admin,
            "POST",
            "/channels",
            {
                "team_id": team["id"],
                "name": f"two-{run}",
                "display_name": f"two {run}",
                "type": "O",
            },
        )
        bots = []
        for name in ("one", "two"):
            bot = admin_call(
                admin, "POST", "/bots", {"username": f"client-{name}-{run}", "display_name": name}
            )
            admin_call(
                admin,
                "POST",
                f"/teams/{team['id']}/members",
                {"team_id": team["id"], "user_id": bot["user_id"]},
            )
            admin_call(
                admin, "POST", f"/channels/{chan['id']}/members", {"user_id": bot["user_id"]}
            )
            tok = admin_call(
                admin,
                "POST",
                f"/users/{bot['user_id']}/tokens",
                {"description": "two_clients spike"},
            )
            bots.append({"name": name, "user_id": bot["user_id"], "token": tok["token"]})
    return {"channel_id": chan["id"], "bots": bots}


async def post_as(client: MattermostClient, channel_id: str, label: str) -> dict[str, Any]:
    me = await client.get_me()
    post = await client.create_post(
        channel_id, f"posted by client {label}", props={"crew_proto": 1}
    )
    return {
        "client": label,
        "get_me": me["id"],
        "post_user_id": post["user_id"],
        "post_id": post["id"],
    }


async def main() -> int:
    url = os.environ["MM_URL"]
    data = setup(url, os.environ["MM_ADMIN_TOKEN"])
    channel_id = data["channel_id"]
    bots = data["bots"]
    print(f"upstream: mcp-server-mattermost=={importlib.metadata.version('mcp-server-mattermost')}")
    print(f"bots: {[(b['name'], b['user_id']) for b in bots]}")

    # Settings carry no token (client_token mode: the token comes per client, as the daemon's
    # would); each client's token is the constructor override.
    settings = Settings(url=url, auth_mode=AuthMode.CLIENT_TOKEN)
    results: list[dict[str, Any]] = []

    print("\n# sequential, each client on its own connection pool")
    for b in bots:
        client = MattermostClient(settings, token=b["token"])
        async with client.lifespan() as c:
            results.append(
                {
                    **await post_as(c, channel_id, b["name"]),
                    "expected": b["user_id"],
                    "mode": "own pool",
                }
            )

    print("# concurrent, both clients borrowing one shared httpx pool, 10 posts each, interleaved")
    # Each client's lifespan is entered once and held open for all its posts, as the daemon
    # would hold one per token. (Entering and leaving one client object's lifespan from
    # several concurrent tasks is NOT safe: the first task to leave sets its _client to None
    # under the others, which then raise "Client not initialized".)
    shared = create_http_client(settings)
    try:
        async with contextlib.AsyncExitStack() as stack:
            clients = [
                (
                    b,
                    await stack.enter_async_context(
                        MattermostClient(settings, token=b["token"], http_client=shared).lifespan()
                    ),
                )
                for b in bots
            ]

            async def run(b: dict[str, Any], c: MattermostClient, i: int) -> dict[str, Any]:
                r = await post_as(c, channel_id, f"{b['name']}#{i}")
                return {**r, "expected": b["user_id"], "mode": "shared pool"}

            results += await asyncio.gather(*(run(b, c, i) for i in range(10) for b, c in clients))
    finally:
        await shared.aclose()

    bad = [r for r in results if not (r["post_user_id"] == r["get_me"] == r["expected"])]
    for r in results:
        ok = "ok " if r not in bad else "BAD"
        who = f"get_me={r['get_me']} post.user_id={r['post_user_id']}"
        print(f"{ok} {r['mode']:11} client {r['client']:8} {who}")
    print(f"\n{len(results)} posts, {len(bad)} with the wrong author")
    print(
        "Answer:",
        "PASS, one client per token works in one process" if not bad else "FAIL, author mix-up",
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
