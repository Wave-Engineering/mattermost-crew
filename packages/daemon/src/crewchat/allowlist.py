"""Which agents `register` mints an account for on a server (#117).

`[crews.<crew>.<server_id>]` may carry `agents_allow` and `agents_deny`. Each entry is a forge
key `host/org`, compared exactly with the agent's `project_key` (`repo_key()` in the shim, so
nested groups share their top-level key); an absolute path, matching a `project_key` that is
that path or lies under it, component by component; or `name:<dev_name>`.

This is an **accident guard, not a security boundary**: the `project_key` and the Dev-Name are
the agent's own word, taken inside the host's trust boundary (D-055).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from crewchat.config import NAME_ENTRY_PREFIX, BrokerEntry


def matches(entry: str, project_key: str, dev_name: str) -> bool:
    """One allowlist entry against an agent. An empty `project_key` matches no forge or path
    entry, so an old shim that sends none fails closed under an allowlist."""
    if entry.startswith(NAME_ENTRY_PREFIX):
        return entry[len(NAME_ENTRY_PREFIX) :] == dev_name
    if not project_key:
        return False
    if entry.startswith("/"):
        if not project_key.startswith("/"):
            return False
        # By component, never as a string prefix: `/a/b` doesn't cover `/a/bc`.
        root, key = PurePosixPath(entry).parts, PurePosixPath(project_key).parts
        return key[: len(root)] == root
    return entry == project_key


def allowed(entry: BrokerEntry, project_key: str, dev_name: str) -> bool:
    """Deny wins; with no `agents_allow` every other agent is allowed (today's behaviour);
    otherwise only an agent an `agents_allow` entry matches."""
    if any(matches(e, project_key, dev_name) for e in entry.agents_deny):
        return False
    if entry.agents_allow is None:
        return True
    return any(matches(e, project_key, dev_name) for e in entry.agents_allow)
