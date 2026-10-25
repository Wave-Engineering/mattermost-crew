"""Story 2.4: the kiosk brief is only the seed's `## Kiosk` section (R-32, D-072)."""

from __future__ import annotations

from pathlib import Path

from crewchat_clients.brief import extract_brief, read_brief

SEED = """# Seed

## Plan
secret plan: ship on Friday

## Kiosk
I maintain the chat daemon.

### Ask me about
- the socket protocol

```md
## Not a heading, inside a fence
```

## Volatile decisions
D-999: private
"""


def test_extract_brief_only_kiosk_section() -> None:
    brief = extract_brief(SEED)
    assert brief.startswith("I maintain the chat daemon.")
    assert "### Ask me about" in brief and "the socket protocol" in brief
    assert "## Not a heading, inside a fence" in brief  # a fenced line is text
    assert "secret plan" not in brief and "D-999" not in brief and "# Seed" not in brief
    assert "## Kiosk" not in brief


def test_missing_or_empty_section_is_empty() -> None:
    assert extract_brief("# Seed\n\n## Plan\nthings\n") == ""
    assert extract_brief("## Kiosk\n\n   \n## Next\nmore\n") == ""
    assert extract_brief("## Kiosk\nlast section, to the end") == "last section, to the end"
    assert extract_brief("## Kiosks\nnot ours\n") == ""


def test_read_brief_missing_file(tmp_path: Path) -> None:
    assert read_brief(tmp_path / "nope.md") == ""
    seed = tmp_path / "seed.md"
    seed.write_text(SEED)
    assert read_brief(seed) == extract_brief(SEED)
