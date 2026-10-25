"""Story 3.9: the Dev Spec's VRTM (Appendix V) traces every requirement to a passing item."""

from __future__ import annotations

import re
from pathlib import Path

DEVSPEC = Path(__file__).resolve().parents[2] / "docs" / "mattermost-crew-devspec.md"
REQUIREMENTS = [f"R-{n:02d}" for n in range(1, 35)]
ITEM = re.compile(r"\b(?:IT-\d{2}|E2E-\d{2})\b")
STATUSES = ("Pass", "Fail")
VRTM_HEADING = "### Appendix V: Verification Requirements Traceability Matrix (VRTM)"


def section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n") + len(heading) + 1
    nxt = re.compile(rf"^#{{1,{level}}} ", re.MULTILINE).search(text, start)
    return text[start : nxt.start() if nxt else len(text)]


def table_rows(body: str) -> list[list[str]]:
    """The cells of each data row of the markdown tables in `body`."""
    rows = []
    for line in body.splitlines():
        if not line.startswith("|") or re.match(r"^\|[-\s|]+\|$", line):
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def vrtm() -> dict[str, dict[str, str]]:
    text = DEVSPEC.read_text(encoding="utf-8")
    rows = table_rows(section(text, VRTM_HEADING))
    header, data = rows[0], rows[1:]
    return {row[0]: dict(zip(header, row, strict=True)) for row in data}


def defined_items() -> set[str]:
    text = DEVSPEC.read_text(encoding="utf-8")
    body = section(text, "### 6.2 Integration Tests (Automated)") + section(
        text, "### 6.3 End-to-End Tests (Automated)"
    )
    return {row[0] for row in table_rows(body) if ITEM.fullmatch(row[0])}


def test_vrtm_covers_every_requirement() -> None:
    matrix = vrtm()
    assert sorted(matrix) == REQUIREMENTS, "Appendix V must have exactly one row per R-01 to R-34"
    items = defined_items()
    for req, row in matrix.items():
        named = ITEM.findall(row["Verification Item"])
        assert named, f"{req} names no IT or E2E verification item"
        assert set(named) <= items, f"{req} names items not in §6.2/§6.3: {set(named) - items}"
        assert row["Verification Method"], f"{req} has no verification method"
        assert row["Status"] in STATUSES, f"{req} status is {row['Status']!r}, not Pass or Fail"


def test_vrtm_every_requirement_passes() -> None:
    failing = {req: row["Status"] for req, row in vrtm().items() if row["Status"] != "Pass"}
    assert failing == {}, f"requirements without a Pass: {failing}"


def test_vrtm_matches_the_requirements_section() -> None:
    reqs = section(DEVSPEC.read_text(encoding="utf-8"), "## 3. Requirements (EARS Format)")
    declared = {row[0] for row in table_rows(reqs) if re.fullmatch(r"R-\d{2}", row[0])}
    assert declared == set(vrtm()), "§3 and Appendix V list different requirements"
