"""Story 1.3 (moved from 1.2, D-090): every §5.N question has an answer in the spike doc."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEVSPEC = ROOT / "docs" / "mattermost-crew-devspec.md"
SPIKE_DOC = ROOT / "docs" / "spikes" / "2026-10-spike-1.2.md"


def open_questions() -> list[tuple[int, str]]:
    """The numbered items of the Dev Spec's §5.N."""
    text = DEVSPEC.read_text(encoding="utf-8")
    start = text.index("### 5.N Open Questions")
    end = text.index("\n## ", start)
    return [
        (int(n), title)
        for n, title in re.findall(r"^(\d+)\. \*\*(.+?)\*\*", text[start:end], flags=re.M)
    ]


def spike_sections() -> dict[int, str]:
    """The spike doc's `## N. ...` sections, each up to the next numbered one."""
    text = SPIKE_DOC.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^## (\d+)\. .*$", text, flags=re.M))
    sections: dict[int, str] = {}
    for i, head in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sections[int(head.group(1))] = text[head.start() : stop]
    return sections


def test_spike_doc_answers_every_question() -> None:
    questions = open_questions()
    assert [n for n, _ in questions] == list(range(1, len(questions) + 1))
    assert len(questions) >= 9
    assert any(n == 9 and "11.8" in title for n, title in questions), "the 11.8 item is missing"

    sections = spike_sections()
    answer = re.compile(r"^(\*\*)?Answer:", flags=re.M)
    missing = [
        f"{n}. {title}"
        for n, title in questions
        if n not in sections or not answer.search(sections[n])
    ]
    assert missing == [], f"no 'Answer:' line in {SPIKE_DOC.name} for: {missing}"
