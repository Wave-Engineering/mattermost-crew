"""Story 1.4: registration (§4.2, §5.3; D-055; R-01, R-05, R-11)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from crewchat.registry import RegisterError, account_name
from crewchat.tokens import BrokerError, BrokerUnavailable


def register(daemon: Any, dev: str = "babel", crew: str | None = "oaw", session: str = "s1") -> Any:
    return asyncio.run(daemon.registry.register(dev, crew, session))


def test_register_returns_account_name(daemon: Any) -> None:
    reg = register(daemon)
    assert reg.account == "babel.oaw"
    assert reg.result()["account"] == "babel.oaw"
    assert daemon.broker().ops("mint") == [
        {"dev_name": "babel", "crew": "oaw", "current_token_id": None}
    ]
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
    assert result["backends"] == ["dev", "discord"]


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
    assert reg.backends == ["dev", "discord"]
    assert reg.unavailable == ["prod"]
    assert daemon.store.token_for("babel", "dev") is not None
    assert daemon.store.token_for("babel", "prod") is None
