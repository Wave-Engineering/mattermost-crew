from importlib.metadata import version

import crewchat_clients


def test_crewchat_clients_version() -> None:
    assert crewchat_clients.__version__ == version("crewchat-clients")
