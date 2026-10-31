"""#157: the daemon's journal carries its own events, not one httpx line per poll."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from crewchat.__main__ import QUIET_LOGGERS, configure_logging


@contextmanager
def bare_root() -> Iterator[None]:
    """A root logger with no handlers, as `crewchat serve` starts with. pytest adds its own
    when the test body starts, which would make `basicConfig` a no-op, so this runs inside
    the body. Everything touched is put back."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quiet = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    root.handlers = []
    try:
        yield
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = handlers
        root.setLevel(level)
        for name, quiet_level in quiet.items():
            logging.getLogger(name).setLevel(quiet_level)


def test_configure_logging_quiets_httpx(capsys: pytest.CaptureFixture[str]) -> None:
    with bare_root():
        configure_logging()
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("crewchat").getEffectiveLevel() == logging.INFO

        logging.getLogger("httpx").info('HTTP Request: GET http://mm/api/v4/users/me "200 OK"')
        logging.getLogger("crewchat.tokens").info("rotated one token")
        logging.getLogger("httpx").warning("a failed request")
    err = capsys.readouterr().err
    assert "HTTP Request:" not in err
    assert "crewchat.tokens INFO rotated one token" in err
    assert "httpx WARNING a failed request" in err
