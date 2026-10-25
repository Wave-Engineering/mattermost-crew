"""The kiosk brief: the `## Kiosk` section of the agent's reseed seed (Dev Spec §5.11; R-32).

`extract_brief(text)` returns the body of the seed's `## Kiosk` section, from the line after
that heading up to the next `## ` heading or the end of the file, stripped. Nothing else from
the seed is ever returned, so nothing else crosses the socket (D-072). A missing or empty
section gives "", and the kiosk then says it has nothing to share yet.

Headings inside fenced code blocks are text, not headings. Deeper headings (`### `) inside the
section are part of it.
"""

from __future__ import annotations

import re
from pathlib import Path

KIOSK_HEADING_RE = re.compile(r"^##\s+Kiosk\s*#*\s*$")
SECTION_RE = re.compile(r"^##(?!#)\s")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def extract_brief(text: str) -> str:
    """The `## Kiosk` section's body, or "" when the seed has none."""
    out: list[str] = []
    inside = False
    fence: str | None = None
    for line in text.splitlines():
        marker = FENCE_RE.match(line)
        if fence is None and not marker:
            if SECTION_RE.match(line):
                if inside:
                    break
                inside = bool(KIOSK_HEADING_RE.match(line))
                continue
        elif marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1) == fence:
                fence = None
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def read_brief(path: Path) -> str:
    """`extract_brief` of the file at `path`; "" when it is missing or unreadable."""
    try:
        return extract_brief(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""
