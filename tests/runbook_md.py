"""Reading `docs/runbook.md` and the other docs: `## ` sections and their fenced blocks.

IT-20 (`tests/integration/test_runbook.py`) runs the blocks `runbook_blocks` returns, and the
unit tests (`tests/unit/test_docs.py`) check the same extraction, so both read the file one way.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
RUNBOOK = DOCS / "runbook.md"

FENCE_RE = re.compile(r"^(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
SECTION_RE = re.compile(r"^## (?!#)(.+?)\s*$")
VERIFY_HEADING = "Verify the install"


def sections(text: str) -> list[tuple[str, str]]:
    """Each `## ` section as (heading, body), in order. A `## ` inside a fence is text."""
    out: list[tuple[str, list[str]]] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = FENCE_RE.match(line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1) == fence and not marker.group(2):
                fence = None
        elif fence is None and (head := SECTION_RE.match(line)):
            out.append((head.group(1), []))
            continue
        if out:
            out[-1][1].append(line)
    return [(heading, "\n".join(body)) for heading, body in out]


def fenced_blocks(body: str, lang: str) -> list[str]:
    """The contents of every fenced block in `body` whose info string is exactly `lang`."""
    blocks: list[str] = []
    current: list[str] | None = None
    fence: str | None = None
    for line in body.splitlines():
        marker = FENCE_RE.match(line)
        if fence is None and marker:
            fence = marker.group(1)
            current = [] if marker.group(2) == lang else None
            continue
        if fence is not None and marker and marker.group(1) == fence and not marker.group(2):
            if current is not None:
                blocks.append("\n".join(current) + "\n")
            fence, current = None, None
            continue
        if current is not None:
            current.append(line)
    return blocks


def is_run_section(heading: str) -> bool:
    """IT-20 runs the blocks under "Install …" and under "Verify the install"."""
    return heading.startswith("Install ") or heading == VERIFY_HEADING


def runbook_blocks(text: str) -> list[tuple[str, str]]:
    """Every `bash` block IT-20 runs, as (section heading, script), in document order."""
    return [
        (heading, block)
        for heading, body in sections(text)
        if is_run_section(heading)
        for block in fenced_blocks(body, "bash")
    ]
