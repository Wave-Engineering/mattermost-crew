"""#115: what the runbook's install blocks do about access, checked without a host.

IT-20 (`tests/integration/test_runbook.py`) runs these blocks for real on a fresh CI host. These
tests pin, in the text IT-20 extracts (`tests/runbook_md.py`), the two fixes from the first real
install: the daemon's ACL drop-in for live agent sessions, and `/etc/crewbroker`'s mode.
"""

from __future__ import annotations

from tests.runbook_md import RUNBOOK, runbook_blocks, sections

DROPIN_DIR = "/etc/systemd/system/crewchat.service.d"
DROPIN = f"{DROPIN_DIR}/agent-acl.conf"


def block(heading: str) -> str:
    (script,) = [s for h, s in runbook_blocks(RUNBOOK.read_text(encoding="utf-8")) if h == heading]
    return script


def line_of(script: str, needle: str) -> int:
    """The index of the first line of `script` that contains `needle`; fails when none does."""
    for n, line in enumerate(script.splitlines()):
        if needle in line:
            return n
    raise AssertionError(f"no line contains {needle!r}")


def test_daemon_block_writes_acl_dropin() -> None:
    script = block("Install the daemon")
    # The drop-in sets the default ACL with the agents' user substituted, and g::rw so the
    # group's access isn't capped at the directory's r-x.
    assert (
        "printf '[Service]\\nExecStartPre=+/usr/bin/setfacl -d -m u:%s:rw,g::rw /run/crewchat\\n'"
        ' "$AGENT_USER"'
    ) in script
    assert f"/dev/stdin {DROPIN}" in script
    assert f"install -d -m 0755 -o root -g root {DROPIN_DIR}" in script
    # After the unit, before daemon-reload, so the first start already has it.
    unit = line_of(script, "packaging/crewchat.service /etc/systemd/system/crewchat.service")
    assert unit < line_of(script, DROPIN_DIR) < line_of(script, "systemctl daemon-reload")
    assert line_of(script, DROPIN) < line_of(script, "systemctl daemon-reload")
    # The group stays: it is the containers' and new logins' path.
    assert 'usermod -aG crewchat-clients "$AGENT_USER"' in script
    # setfacl is checked up front, not discovered by a failed ExecStartPre.
    assert line_of(script, "[[ -x /usr/bin/setfacl ]]") < line_of(script, "systemctl restart")


def test_broker_block_sets_crewbroker_mode() -> None:
    script = block("Install the broker")
    command = "sudo install -d -m 0755 -o root -g root /etc/crewbroker"
    fix = line_of(script, command)
    assert script.splitlines()[fix].strip() == command
    assert fix < line_of(script, "for entry in $CREWBROKER_INSTANCES")


def test_prerequisites_and_rollback_cover_the_dropin() -> None:
    body = dict(sections(RUNBOOK.read_text(encoding="utf-8")))
    assert "`acl`" in body["Prerequisites"]
    assert "`sg crewchat-clients`" in body["Prerequisites"]
    assert f"sudo rm -rf {DROPIN_DIR}" in body["Rollback"]
