"""The crew's social switch and kiosk flag, held by the broker (Dev Spec §4.6, §5.6).

`/var/lib/crewbroker/<crew>-<server_id>/switch.json` holds one `SwitchState`. The default is
crew only with the kiosk on (R-20, R-34). Only the crew's human partners change it, by DMing
the broker (R-24). Social always carries an expiry; past it, the switch reverts to the
setting social was engaged from, or to crew only if that can't be read (R-23).

Invariant: `revert_to` is never `social`, so extending or re-engaging social always lapses
back to a non-social mode. Expiry is checked whenever the state is read, never by a timer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from crewbroker.config import CrewConfig
from crewbroker.state import read_json, write_private_json

Mode = Literal["crew", "trusted", "social"]
MODES: tuple[Mode, ...] = ("crew", "trusted", "social")
SAFE_MODES: tuple[Mode, ...] = ("crew", "trusted")
DEFAULT_MODE: Mode = "crew"
# A guard against a typo'd duration, and against overflowing a datetime.
MAX_SOCIAL = timedelta(days=30)

_DURATION_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")

USAGE = (
    "commands: `social <duration>` (e.g. `social 2h`, `social 1h30m`), `trusted`, `crew`, "
    "`kiosk off`, `kiosk on`"
)
REFUSAL = "refused: only this crew's human partner can change the switch"


@dataclass(frozen=True)
class SwitchState:
    mode: str = DEFAULT_MODE
    expires_at: datetime | None = None
    revert_to: str = DEFAULT_MODE
    kiosk_on: bool = True


def safe_revert(revert_to: str) -> Mode:
    """The mode a lapse lands on: `revert_to`, or crew only if it is unreadable or social."""
    return revert_to if revert_to in SAFE_MODES else DEFAULT_MODE


def effective_mode(state: SwitchState, now: datetime) -> tuple[Mode, bool]:
    """The mode in force at `now`, and whether a social expiry has passed (R-23).

    Social with no expiry is unreadable, and lapses: the switch fails closed.
    """
    if state.mode == "social":
        if state.expires_at is not None and now < state.expires_at:
            return "social", False
        return safe_revert(state.revert_to), True
    if state.mode in SAFE_MODES:
        return state.mode, False
    return DEFAULT_MODE, False


def settle(state: SwitchState, now: datetime) -> SwitchState:
    """`state` with any lapse applied: `mode = revert_to` and no expiry.

    The caller persists the result, before every command and on every `switch_state`, so a
    lapsed social never lingers in storage.
    """
    mode, lapsed = effective_mode(state, now)
    if not lapsed and mode == state.mode:
        return state
    return replace(state, mode=mode, expires_at=None, revert_to=mode)


def parse_duration(text: str) -> timedelta | None:
    """`2h`, `90m`, `1h30m`, `1d`, `45s`: a positive duration, or None."""
    match = _DURATION_RE.fullmatch(text)
    if match is None or not any(match.groups()):
        return None
    try:
        days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        dur = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except (OverflowError, ValueError):
        return None
    return dur if dur > timedelta(0) else None


def _describe(state: SwitchState) -> str:
    kiosk = "on" if state.kiosk_on else "off"
    if state.mode == "social" and state.expires_at is not None:
        return (
            f"switch: social until {state.expires_at.isoformat(timespec='seconds')}, "
            f"then {safe_revert(state.revert_to)}; kiosk {kiosk}"
        )
    label = "crew only" if state.mode == "crew" else "trusted crews"
    return f"switch: {label}; kiosk {kiosk}"


def apply_command(
    state: SwitchState, sender_user_id: str, text: str, cfg: CrewConfig, now: datetime
) -> tuple[SwitchState, str]:
    """One DM to the broker: the new state and the reply to post (§4.6).

    A sender who is not one of the crew's human partners, by user ID, is refused and the
    state is returned unchanged (R-24).
    """
    if sender_user_id not in cfg.human_user_ids:
        return state, REFUSAL
    state = settle(state, now)
    words = text.strip().lower().split()
    if words == ["crew"]:
        state = replace(state, mode="crew", expires_at=None, revert_to="crew")
    elif words == ["trusted"]:
        state = replace(state, mode="trusted", expires_at=None, revert_to="trusted")
    elif words == ["kiosk", "off"]:
        state = replace(state, kiosk_on=False)
    elif words == ["kiosk", "on"]:
        state = replace(state, kiosk_on=True)
    elif len(words) == 2 and words[0] == "social":
        dur = parse_duration(words[1])
        if dur is None or dur > MAX_SOCIAL:
            return state, f"not a duration (up to {MAX_SOCIAL.days}d): {words[1]!r}. {USAGE}"
        mode, _ = effective_mode(state, now)
        # Already social: a new expiry, `dur` from now (shorter or longer, #154), keeping where
        # it reverts to. Otherwise remember the mode social was engaged from, which by the
        # invariant is crew or trusted.
        revert_to = safe_revert(state.revert_to) if mode == "social" else safe_revert(mode)
        state = replace(state, mode="social", expires_at=now + dur, revert_to=revert_to)
    else:
        return state, f"{_describe(state)}\n{USAGE}"
    return state, _describe(state)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def to_json(state: SwitchState) -> dict[str, Any]:
    return {
        "mode": state.mode,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
        "revert_to": state.revert_to,
        "kiosk_on": state.kiosk_on,
    }


def from_json(data: dict[str, Any]) -> SwitchState:
    """Read a stored state, failing closed: an unknown mode is crew only, an unreadable
    expiry lapses social, and a `revert_to` that is unreadable or social reverts to crew."""
    mode = data.get("mode")
    revert_to = data.get("revert_to")
    kiosk_on = data.get("kiosk_on")
    return SwitchState(
        mode=mode if isinstance(mode, str) and mode in MODES else DEFAULT_MODE,
        expires_at=_parse_ts(data.get("expires_at")),
        revert_to=revert_to if isinstance(revert_to, str) else DEFAULT_MODE,
        kiosk_on=kiosk_on if isinstance(kiosk_on, bool) else True,
    )


class SwitchStore:
    """`switch.json` in the instance's state directory, written whole and mode 0600."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "switch.json"

    def load(self) -> SwitchState:
        try:
            data = read_json(self.path, {})
        except ValueError:
            return SwitchState()  # unreadable: the defaults, which fail closed
        return from_json(data)

    def save(self, state: SwitchState) -> None:
        write_private_json(self.path, to_json(state))
