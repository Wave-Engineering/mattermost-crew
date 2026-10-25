"""The doorbell's filter hook (Dev Spec §5.9; Stories 3.2, 3.3).

`admit(dev_name, event)` decides whether an event the doorbell took from a backend goes on the
agent's queue. In Story 3.2 it is a pass-through: every event is admitted. Story 3.3 fills it
in with membership, addressing, the loop guard and the switch (R-20 to R-22, R-25).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crewchat.doorbell import DoorbellEvent


def admit(dev_name: str, event: DoorbellEvent) -> bool:
    """Whether `event` is queued for `dev_name`. A pass-through until Story 3.3."""
    return True
