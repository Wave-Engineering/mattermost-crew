from importlib.metadata import version

import crewchat


def test_crewchat_version() -> None:
    assert crewchat.__version__ == version("crewchat")
