from importlib.metadata import version

import crewbroker


def test_crewbroker_version() -> None:
    assert crewbroker.__version__ == version("crewbroker")
