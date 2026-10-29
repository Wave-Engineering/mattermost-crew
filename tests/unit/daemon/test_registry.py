"""Story 1.4: registration (§4.2, §5.3; D-055; R-01, R-05, R-11)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.discord import DiscordBackend
from crewchat.registry import RegisterError, Registry, account_name
from crewchat.tokens import BrokerError, BrokerUnavailable


def register(daemon: Any, dev: str = "babel", crew: str | None = "oaw", session: str = "s1") -> Any:
    return asyncio.run(daemon.registry.register(dev, crew, session))


def test_register_returns_account_name(daemon: Any) -> None:
    reg = register(daemon)
    assert reg.account == "babel.oaw"
    assert reg.result()["account"] == "babel.oaw"
    (args,) = daemon.broker().ops("mint")
    # The broker refuses the mint once this call's 2 s timeout has passed (#41).
    assert args.pop("deadline") == pytest.approx(time.time() + 2.0, abs=1.0)
    assert args == {"dev_name": "babel", "crew": "oaw", "current_token_id": None}
    # One registration per Dev-Name, taken at its word (D-055).
    assert daemon.registry.get("babel") is reg
    assert reg.session_id == "s1"


def test_register_defaults_crew(daemon: Any) -> None:
    """§5.3: with no crew in the identity file, the host's default_crew is used."""
    assert register(daemon, crew=None).account == "babel.oaw"
    assert register(daemon, dev="kessel", crew="").account == "kessel.oaw"


def test_register_returns_needs_list(daemon: Any, servers_toml: dict[str, Any]) -> None:
    result = register(daemon).result()
    assert result["needs_list"] == servers_toml["crews"]["oaw"]["needs_list"] == "bj:needs"
    # Story 2.4c: the one backend-name form, mattermost:<server_id> (not the bare id).
    assert result["backends"] == ["mattermost:dev", "discord"]


def test_reregister_revokes_previous_token(daemon: Any) -> None:
    """The shim's daily re-register: mint, swap, reconnect, revoke the previous token."""
    register(daemon)
    first = daemon.store.agent("babel").servers["dev"]
    daemon.events.clear()
    seen: list[str | None] = []
    broker = daemon.broker()
    broker.on_revoke = lambda _tid: seen.append(daemon.store.token_for("babel", "dev"))

    register(daemon, session="s2")
    second = daemon.store.agent("babel").servers["dev"]
    assert second.token_id != first.token_id
    steps = [(e[0], e[2] if e[0] == "broker" else e[1]) for e in daemon.events]
    assert steps == [
        ("broker", "mint"),
        ("hook", "babel"),
        ("broker", "revoke"),
        ("broker", "crew_info"),
        ("broker", "switch_state"),
    ]
    # The mint names the token held, so the broker keeps it live until the swap (#41).
    assert broker.ops("mint")[-1]["current_token_id"] == first.token_id
    assert broker.ops("revoke") == [{"token_id": first.token_id}]
    # The swap came first: at revoke time the store already held the new token.
    assert seen == [second.token]
    assert first.token_id not in broker.live and second.token_id in broker.live
    assert daemon.registry.get("babel").session_id == "s2"


def test_register_rejects_bad_names(daemon: Any) -> None:
    for dev, crew in (("", "oaw"), (".hidden", "oaw"), ("Babel", "oaw"), ("a/b", "oaw")):
        with pytest.raises(RegisterError, match="bad_request"):
            register(daemon, dev=dev, crew=crew)
    with pytest.raises(RegisterError, match="bad_request"):
        register(daemon, crew="otter")  # no [crews.otter] table
    with pytest.raises(RegisterError, match="bad_request"):
        register(daemon, session="")
    assert daemon.broker().ops("mint") == []


def test_account_name_fits_ct05() -> None:
    assert account_name("babel", "oaw") == "babel.oaw"
    assert len(account_name("a" * 60, "oaw")) == 64
    with pytest.raises(ValueError, match="longer than 64"):
        account_name("a" * 61, "oaw")
    with pytest.raises(ValueError):
        account_name("babel", "OaW")


def test_register_fails_loudly_when_no_broker_answers(daemon: Any) -> None:
    daemon.broker().fail["mint"] = BrokerUnavailable("timeout")
    with pytest.raises(RegisterError, match="broker_unavailable"):
        register(daemon)
    assert daemon.store.agent("babel") is None


def _two_servers(raw: dict[str, Any]) -> dict[str, Any]:
    raw["mattermost"].append({"id": "prod", "url": "http://mm2.invalid"})
    raw["crews"]["oaw"]["prod"] = {
        "broker_socket": "/run/crewbroker/oaw-prod.sock",
        "broker_user_id": "b-oaw-prod",
    }
    return raw


def test_register_all_squatted_returns_squatted(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]
) -> None:
    daemon = make_daemon(_two_servers(servers_toml))
    for sid in ("dev", "prod"):
        daemon.broker("oaw", sid).fail["mint"] = BrokerError("mint", "squatted: held by x")
    with pytest.raises(RegisterError) as exc:
        register(daemon)
    assert exc.value.code == "squatted"

    # A squatted name on one server and a timeout on the other: a retry may still work.
    daemon.broker("oaw", "prod").fail["mint"] = BrokerUnavailable("timeout")
    with pytest.raises(RegisterError) as exc:
        register(daemon)
    assert exc.value.code == "broker_unavailable"


def test_register_keeps_the_servers_that_answered(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]
) -> None:
    raw = servers_toml
    raw["mattermost"].append({"id": "prod", "url": "http://mm2.invalid"})
    raw["crews"]["oaw"]["prod"] = {
        "broker_socket": "/run/crewbroker/oaw-prod.sock",
        "broker_user_id": "b-oaw-prod",
    }
    daemon = make_daemon(raw)
    daemon.broker("oaw", "prod").fail["mint"] = BrokerUnavailable("timeout")
    reg = register(daemon)
    assert reg.backends == ["mattermost:dev", "discord"]
    assert reg.unavailable == ["prod"]  # the server ids whose broker didn't mint (log only)
    assert daemon.store.token_for("babel", "dev") is not None
    assert daemon.store.token_for("babel", "prod") is None


def test_register_skips_disallowed_server(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]
) -> None:
    """#117: with an allowlist on one server, `register` mints there only for an agent it
    admits, and a skipped server is neither minted nor `unavailable`."""
    raw = _two_servers(servers_toml)
    raw["crews"]["oaw"]["prod"]["agents_allow"] = ["forge.example.lan/partner-org", "name:helper"]
    daemon = make_daemon(raw)

    reg = asyncio.run(
        daemon.registry.register("babel", "oaw", "s1", project_key="forge.example.lan/other")
    )
    assert reg.backends == ["mattermost:dev", "discord"]
    assert reg.unavailable == []
    assert daemon.broker("oaw", "prod").ops("mint") == []
    assert daemon.store.token_for("babel", "prod") is None

    reg = asyncio.run(
        daemon.registry.register("kessel", "oaw", "s1", project_key="forge.example.lan/partner-org")
    )
    assert reg.backends == ["mattermost:dev", "mattermost:prod", "discord"]
    reg = asyncio.run(daemon.registry.register("helper", "oaw", "s1"))  # by name, no key
    assert "mattermost:prod" in reg.backends

    # Deny wins over allow.
    raw["crews"]["oaw"]["prod"]["agents_deny"] = ["name:kessel"]
    daemon = make_daemon(raw)
    reg = asyncio.run(
        daemon.registry.register("kessel", "oaw", "s1", project_key="forge.example.lan/partner-org")
    )
    assert reg.backends == ["mattermost:dev", "discord"]


def test_register_retires_token_on_newly_disallowed_server(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]
) -> None:
    """#117: an agent taken off a server's allowlist has its token there revoked by its next
    `register`, and that server leaves its rotation; its other servers are untouched."""
    raw = _two_servers(servers_toml)
    daemon = make_daemon(raw)
    register(daemon)
    held = daemon.store.held_token_id("babel", "oaw", "prod")
    assert held is not None

    raw["crews"]["oaw"]["prod"]["agents_allow"] = ["name:helper"]
    restarted = make_daemon(raw)  # the same token directory, as after a daemon restart
    restarted.store.load()
    prod = restarted.broker("oaw", "prod")
    prod.live[held] = "babel.oaw"  # the real broker still holds it
    reg = register(restarted)
    assert reg.backends == ["mattermost:dev", "discord"]
    assert restarted.store.token_for("babel", "prod") is None
    assert restarted.store.token_for("babel", "dev") is not None
    assert [a["token_id"] for a in prod.ops("revoke")] == [held]
    assert held not in prod.live and prod.ops("mint") == []
    on_disk = json.loads(restarted.store.path_for("babel").read_text(encoding="utf-8"))
    assert "prod" not in on_disk["servers"] and not on_disk["pending_revoke"]


def test_register_disallowed_everywhere_keeps_discord(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]
) -> None:
    """No Mattermost server admits the agent: it still registers, on Discord. An old shim's
    empty `project_key` fails closed under an allowlist."""
    raw = _two_servers(servers_toml)
    for sid in ("dev", "prod"):
        raw["crews"]["oaw"][sid]["agents_allow"] = ["forge.example.lan/partner-org"]
    daemon = make_daemon(raw)
    reg = register(daemon)  # sends no project_key
    assert reg.backends == ["discord"] and reg.unavailable == []
    assert daemon.store.agent("babel") is None


def test_register_project_key_over_the_socket(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]
) -> None:
    """The socket's `register` passes `project_key` through; a non-string is `bad_request`."""
    from crewchat.server import DaemonServer, build_handlers

    raw = _two_servers(servers_toml)
    raw["crews"]["oaw"]["prod"]["agents_allow"] = ["forge.example.lan/partner-org"]
    daemon = make_daemon(raw)
    server = DaemonServer(build_handlers(daemon.config, daemon.registry))

    def call(**args: Any) -> dict[str, Any]:
        frame = {"proto": 1, "id": "x", "op": "register", "args": args}
        return asyncio.run(server.handle_line(json.dumps(frame).encode()))

    got = call(dev_name="babel", session_id="s1", project_key="forge.example.lan/partner-org")
    assert got["result"]["backends"] == ["mattermost:dev", "mattermost:prod", "discord"]
    bad = call(dev_name="babel", session_id="s1", project_key=["forge.example.lan/partner-org"])
    assert bad["ok"] is False and bad["error"].startswith("bad_request")


def test_register_sets_discord_identity(daemon: Any, tmp_path: Path) -> None:
    """D-103: register hands the avatar and team to the Discord adapter's set_identity."""
    discord = DiscordBackend("http://screamhole.invalid", tmp_path / "discord.credential")
    registry = Registry(
        daemon.config, daemon.brokers, daemon.store, daemon.cache, identity_sink=discord
    )
    reg = asyncio.run(registry.register("babel", "oaw", "s1", dev_team="oaw", dev_avatar="📡"))
    assert (reg.dev_team, reg.dev_avatar) == ("oaw", "📡")
    assert discord.signature("babel") == "— **babel** 📡 (oaw) ·p1"
    assert discord.signed("babel", "hello")[-1].endswith("— **babel** 📡 (oaw) ·p1")
    # A re-register overwrites both, on the registration and in the signature.
    reg = asyncio.run(registry.register("babel", "oaw", "s2", dev_team="wave", dev_avatar="🦊"))
    assert (reg.dev_team, reg.dev_avatar) == ("wave", "🦊")
    assert discord.signature("babel") == "— **babel** 🦊 (wave) ·p1"
    # result() is unchanged: no team, no avatar.
    assert set(reg.result()) == {"account", "backends", "needs_list"}


def test_register_without_discord_skips_identity(
    make_daemon: Any, servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    servers_toml["discord"] = {"enabled": False}
    d = make_daemon(servers_toml)
    discord = DiscordBackend("http://screamhole.invalid", tmp_path / "discord.credential")
    registry = Registry(d.config, d.brokers, d.store, d.cache, identity_sink=discord)
    asyncio.run(registry.register("babel", "oaw", "s1", dev_team="oaw", dev_avatar="📡"))
    assert discord.identity("babel") is None
