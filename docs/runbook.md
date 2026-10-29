# Operator runbook: installing the chat daemon, the broker and the kiosk

This runbook installs one host: one chat daemon (`crewchat`), one broker per crew and Mattermost
server (`crewbroker@<crew>-<server_id>`) and, where the kiosk's CLI runner is used, the kiosk
socket (`crewkiosk.socket`). It is Dev Spec §5.B written out as commands. It also covers the
environment prerequisites and the install verification (DM-09a).

**CI runs this file.** IT-20 (`tests/integration/test_runbook.py`) extracts every fenced `bash`
block under the "Install …" headings and under "Verify the install", and runs them in document
order on a fresh host. Every block must exit 0, so a command here that drifts from the code fails
the build. Keep each block self-contained: it starts from the root of the release checkout, in a
new shell, with only the variables below set. A block never relies on a `cd` or a variable from
an earlier block.

**Production.** Installing onto a host that serves a partner organization's production
Mattermost needs that human partner's yes-prod first, and so does the broker account's role
patch (CP-03, S-25). The OaW dev space needs neither (CP-06).

The variables every block reads:

| Variable | What it is | Example |
|---|---|---|
| `CREWBROKER_INSTANCES` | each broker on this host, as `<crew>:<server_id>`, space-separated | `oaw:oaw-dev` |
| `AGENT_USER` | the agents' OS user (optional, default `agent`) | `agent` |

Run every block as a user with `sudo`, from the root of a checkout of the release tag:

```text
git clone --branch <tag> https://github.com/Wave-Engineering/mattermost-crew
cd mattermost-crew
export CREWBROKER_INSTANCES="oaw:oaw-dev"
```

## Prerequisites

The host needs:

- **Linux with systemd**, `sudo`, `git`, `tar` and `jq` (the verify step uses `jq`).
- **Python 3.12** and **uv**. The install uses the system `python3.12` when there is one;
  otherwise uv downloads 3.12 into `/opt/mattermost-crew/python`, which every service user can
  reach. `uv` must be on the installing user's `PATH`.
- **The agents' OS user** (`AGENT_USER`, default `agent`). The install adds it to
  `crewchat-clients`, which is the only thing that lets it reach the daemon.
- **Claude Code at `/usr/local/bin/claude`**, only on a host that runs the kiosk's CLI runner,
  with **no managed Claude Code settings** on the host, local or org-pushed: they still apply
  under `--restricted` (§5.11, spike 1.2).
- **The Mattermost side:** a broker account per (crew, server), with the patched
  `system_user_access_token` role and no admin role (R-06). On a partner's production server the
  role patch is the human partner's yes-prod (CP-03).

The install creates these OS users and groups. They are listed here so you can check a host
before and after:

| User or group | What it is | Rules |
|---|---|---|
| `crewchat` (user and group) | the chat daemon | also in `crewchat-clients`, so it can give its socket that group |
| `crewchat-clients` (group) | who may connect to the daemon socket | `crewchat` and the agents' user |
| `crewbroker` (user and group) | every broker instance | **never in `crewchat`**. The broker sockets get the `crewchat` group from the setgid directory `/run/crewbroker`, made by `packaging/crewbroker.tmpfiles.conf` |
| `crewkiosk` (user and group) | the kiosk's CLI runner, only if it is used | an **empty HOME** (`/home/crewkiosk`, mode 0700); in no other group; holds only the kiosk's model credential |

**The socket mount into containers.** A host has one daemon socket,
`/run/crewchat/crewchat.sock` (mode 0660, `crewchat:crewchat-clients`). Agents in containers on
this host reach it through a bind mount of its directory, plus the group:

```text
docker run ... \
  -v /run/crewchat:/run/crewchat \
  --group-add "$(getent group crewchat-clients | cut -d: -f3)" \
  ...
```

Mount the directory, not the socket file: the daemon recreates the socket when it restarts, and a
file mount would keep pointing at the old one. Never mount `/run/crewbroker`, `/etc/crewchat`,
`/etc/crewbroker`, `/var/lib/crewchat` or `/var/lib/crewbroker` into an agent's container.

## Configure

Write the configuration **before** the install steps: each service starts as soon as it is
installed. Every file here is owned by root. The credential files are written mode 0600, owned by
root, and each install step hands its own credential to its service user.

### `/etc/crewchat/servers.toml`

Mode 0644, owned by root. The daemon reads it at start.

```toml
# The host's default crew: an agent whose identity file names no crew registers with it.
default_crew = "oaw"
# Every daemon->broker call's timeout, in seconds. Optional, default 2.
broker_timeout_s = 2
# Kiosk routings per outsider, per expert, per server, per hour. Optional, default 10.
kiosk_rate_per_hour = 10
# Messages from bot accounts per conversation in the loop guard's window. Optional, default 20.
loop_guard_window = 20
# How long a thread the agent posted in stays "mine", in days. Optional, default 90 (D-104).
my_threads_retention_days = 90

# One table per Mattermost server, as an array. `id` is the server id every other table uses.
[[mattermost]]
id = "oaw-dev"
url = "https://mm.example.lan"
# The kiosk answers outsiders on this server only when this is true. Optional, default false,
# whatever the broker's `kiosk on` switch says (#117).
kiosk = false
# Kiosk answers on this server per UTC day, across every outsider and agent; 0 means none.
# Optional, default 25. The count survives a daemon restart.
kiosk_daily_cap = 25

# Discord. With `enabled = true`, also set `screamhole_url`, and install the Discord credential
# at /etc/crewchat/discord.credential (mode 0600).
[discord]
enabled = false

# One table per crew on this host: the sqtk list `register` hands back (R-11).
[crews.oaw]
needs_list = "bj:needs"

# One table per crew and Mattermost server, for EVERY broker on this host, trusted crews'
# brokers included. `broker_user_id` is that broker account's Mattermost user id. These entries
# are the configured broker list: the only accounts that may hold the patched role (R-06).
[crews.oaw.oaw-dev]
broker_socket = "/run/crewbroker/oaw-oaw-dev.sock"
broker_user_id = "<the broker account's user id>"
```

Required: `default_crew` (naming a `[crews.<crew>]` table), each `[[mattermost]]` entry's `id`
and `url`, `[discord] enabled`, each `[crews.<crew>] needs_list`, and each
`[crews.<crew>.<server_id>]` table's `broker_socket` and `broker_user_id`. A trusted crew is
reached only through its own `[crews.<crew>.<server_id>]` table here; a trusted crew with no
table on this host is skipped, and its senders are outsiders.

**Per-server controls** (#117). Before the daemon joins a second server, such as a partner's,
decide on it three things: whether the kiosk answers there, its daily kiosk cap, and which
agents get an account there. For example, a second server where only one forge org's repos and
one named agent appear, with no kiosk:

```text
[[mattermost]]
id = "partner"
url = "https://chat.example.lan"
kiosk = false

[crews.oaw.partner]
broker_socket = "/run/crewbroker/oaw-partner.sock"
broker_user_id = "<the broker account's user id on that server>"
agents_allow = ["forge.example.lan/partner-org", "name:helper"]
agents_deny = ["name:scratch"]
```

- `agents_allow` (optional) lists who `register` mints an account for on that server. Without
  it, every agent on the host gets one, as before. Each entry is a forge key `host/org` (the
  agent's repo origin, reduced to its host and top-level group, compared exactly), an absolute
  path (an agent whose repo is that directory or under it), or `name:<dev_name>`. An agent whose
  shim sends no project key (one older than v0.2.3) matches only a `name:` entry.
- `agents_deny` (optional) takes the same forms and always wins over `agents_allow`. A forge key
  or path in it never matches an older shim, so a deny-only list lets those agents through; to
  keep one out, deny it by `name:` too.
- The allowlist is an accident guard, not a security boundary: an agent's project key and
  Dev-Name are its own word, taken inside the host's trust boundary.
- Taking an agent off a list later (and restarting the daemon) stops new mints, and the agent's
  next `register` revokes the token it holds on that server and takes that server out of its
  daily rotation. Its bot account stays, inactive once the broker's 30-day idle sweep reaches
  it.

### `/etc/crewbroker/<crew>-<server_id>.toml`

Mode 0644, owned by root. One file per broker instance.

```toml
crew = "oaw"
server_id = "oaw-dev"
server_url = "https://mm.example.lan"
# The crew's human partners, by Mattermost user id. Only they can change the switch (R-24).
human_user_ids = ["<the partner's user id>"]
# Trusted crews, by name only. Each one's membership comes from its own broker on this host.
trusted = []
```

Required: `server_id`, `server_url` and `human_user_ids`. `crew` defaults to the first human
partner's username (R-02); set it. `trusted = ["<crew>", …]` is names only: a table, or any
membership key, is refused. `broker_user_id` is never configured: the broker fills it at startup
from `/users/me`, and so is `minted_bot_ids`, which only the broker records.

### The credentials

| File | What it is | Write it as | The install hands it to |
|---|---|---|---|
| `/etc/crewbroker/<crew>-<server_id>.credential` | the broker account's personal access token, one line | 0600, root | `crewbroker`, 0600 |
| `/etc/crewkiosk/model.credential` | the kiosk's model API key, one line (kiosk hosts only) | 0600, root, in `/etc/crewkiosk` | `crewkiosk`, 0600 |
| `/etc/crewchat/discord.credential` | the Discord credential (only with Discord enabled) | 0600, root | `crewchat`, 0600 |

For example, for the broker's token, without it passing through the shell's history:

```text
sudo install -d -m 0755 /etc/crewbroker
sudo install -m 0600 -o root -g root /dev/stdin /etc/crewbroker/oaw-oaw-dev.credential
<paste the token, then Enter and Ctrl-D>
```

## Install the daemon

Creates `crewchat-clients` and `crewchat`, puts the agents' user in `crewchat-clients`, installs
the code into `/opt/mattermost-crew`, then installs and starts `crewchat.service`.

```bash
set -euo pipefail
AGENT_USER="${AGENT_USER:-agent}"
UV="$(command -v uv)"
sudo test -r /etc/crewchat/servers.toml || { echo "write /etc/crewchat/servers.toml first (Configure)" >&2; exit 1; }
id "$AGENT_USER" >/dev/null || { echo "no user $AGENT_USER (Prerequisites)" >&2; exit 1; }

getent group crewchat-clients >/dev/null || sudo groupadd --system crewchat-clients
getent passwd crewchat >/dev/null || sudo useradd --system --user-group --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin crewchat
sudo usermod -aG crewchat-clients crewchat
sudo usermod -aG crewchat-clients "$AGENT_USER"
if sudo test -e /etc/crewchat/discord.credential; then
  sudo chown crewchat:crewchat /etc/crewchat/discord.credential
  sudo chmod 0600 /etc/crewchat/discord.credential
fi

# The code: this checkout's commit, root-owned, in /opt/mattermost-crew.
sudo install -d -m 0755 -o root -g root /opt/mattermost-crew
git archive --format=tar HEAD | sudo tar -x -C /opt/mattermost-crew --no-same-owner
sudo env UV_PYTHON_INSTALL_DIR=/opt/mattermost-crew/python UV_LINK_MODE=copy \
  "$UV" --directory /opt/mattermost-crew sync --frozen --no-dev --python 3.12 \
  --package crewchat --inexact

sudo install -m 0644 -o root -g root packaging/crewchat.service /etc/systemd/system/crewchat.service
sudo systemctl daemon-reload
sudo systemctl enable crewchat
sudo systemctl restart crewchat

for _ in $(seq 1 30); do
  sudo test -S /run/crewchat/crewchat.sock && break
  sleep 1
done
sudo test -S /run/crewchat/crewchat.sock || { sudo journalctl -u crewchat -n 50 --no-pager >&2; exit 1; }
echo "crewchat is up on /run/crewchat/crewchat.sock"
```

## Install the broker

Creates `crewbroker`, installs the broker into the same `/opt/mattermost-crew` environment, the
template unit and the socket directory, then starts one `crewbroker@<crew>-<server_id>` instance
per entry in `CREWBROKER_INSTANCES`: one per (crew, Mattermost server). The daemon picks each
broker up on its next refresh, within 30 s.

```bash
set -euo pipefail
: "${CREWBROKER_INSTANCES:?list each broker as <crew>:<server_id>}"
UV="$(command -v uv)"

getent passwd crewbroker >/dev/null || sudo useradd --system --user-group --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin crewbroker
# The broker is never in the daemon's group: its sockets get crewchat from the setgid directory.
if id -nG crewbroker | tr ' ' '\n' | grep -qx crewchat; then
  echo "crewbroker must not be in group crewchat" >&2
  exit 1
fi

sudo install -d -m 0755 -o root -g root /opt/mattermost-crew
git archive --format=tar HEAD | sudo tar -x -C /opt/mattermost-crew --no-same-owner
sudo env UV_PYTHON_INSTALL_DIR=/opt/mattermost-crew/python UV_LINK_MODE=copy \
  "$UV" --directory /opt/mattermost-crew sync --frozen --no-dev --python 3.12 \
  --package crewbroker --inexact

sudo install -m 0644 -o root -g root packaging/crewbroker@.service /etc/systemd/system/crewbroker@.service
sudo install -m 0644 -o root -g root packaging/crewbroker.tmpfiles.conf /etc/tmpfiles.d/crewbroker.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/crewbroker.conf
sudo systemctl daemon-reload

for entry in $CREWBROKER_INSTANCES; do
  instance="${entry%%:*}-${entry#*:}"
  sudo test -r "/etc/crewbroker/${instance}.toml" || { echo "write /etc/crewbroker/${instance}.toml first (Configure)" >&2; exit 1; }
  sudo test -s "/etc/crewbroker/${instance}.credential" || { echo "write /etc/crewbroker/${instance}.credential first (Configure)" >&2; exit 1; }
  sudo chown crewbroker:crewbroker "/etc/crewbroker/${instance}.credential"
  sudo chmod 0600 "/etc/crewbroker/${instance}.credential"
  sudo systemctl enable "crewbroker@${instance}"
  sudo systemctl restart "crewbroker@${instance}"
  for _ in $(seq 1 30); do
    sudo test -S "/run/crewbroker/${instance}.sock" && break
    sleep 1
  done
  sudo test -S "/run/crewbroker/${instance}.sock" || {
    sudo journalctl -u "crewbroker@${instance}" -n 50 --no-pager >&2
    exit 1
  }
  echo "crewbroker@${instance} is up on /run/crewbroker/${instance}.sock"
done
```

## Install the kiosk runner

Only on a host that runs the kiosk's CLI runner (D-096, D-107). Creates `crewkiosk` with an empty
HOME, installs `packaging/crewkiosk-run` at `/usr/local/libexec/crewkiosk-run`, hands the model
credential to `crewkiosk`, and starts `crewkiosk.socket`. The daemon connects to
`/run/crewkiosk/kiosk.sock` and systemd runs one answer per connection as `crewkiosk`, so the
daemon never changes user and keeps `NoNewPrivileges=yes`.

```bash
set -euo pipefail
sudo test -s /etc/crewkiosk/model.credential || { echo "write /etc/crewkiosk/model.credential first (Configure)" >&2; exit 1; }

getent passwd crewkiosk >/dev/null || sudo useradd --system --user-group --no-create-home \
  --home-dir /home/crewkiosk --shell /usr/sbin/nologin crewkiosk
# An empty HOME: no dotfiles, no settings, no CLAUDE.md.
sudo install -d -m 0700 -o crewkiosk -g crewkiosk /home/crewkiosk
if [[ -n "$(sudo ls -A /home/crewkiosk)" ]]; then
  echo "/home/crewkiosk is not empty" >&2
  exit 1
fi
[[ -x /usr/local/bin/claude ]] || echo "warning: no /usr/local/bin/claude; the kiosk can't answer until Claude Code is installed" >&2

sudo install -d -m 0750 -o root -g crewkiosk /etc/crewkiosk
sudo chown crewkiosk:crewkiosk /etc/crewkiosk/model.credential
sudo chmod 0600 /etc/crewkiosk/model.credential
sudo install -D -m 0755 -o root -g root packaging/crewkiosk-run /usr/local/libexec/crewkiosk-run
sudo install -m 0644 -o root -g root packaging/crewkiosk.socket packaging/crewkiosk@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crewkiosk.socket
sudo test -S /run/crewkiosk/kiosk.sock
echo "crewkiosk.socket is listening on /run/crewkiosk/kiosk.sock"
```

## Verify the install

`make verify-install` runs `scripts/verify-install.sh`. It checks, per broker instance, the
socket's mode and group, a `mint` for a `verify-canary` bot account as the daemon's user (its
token is revoked straight away and never printed), and that the agents' user cannot read the
broker's credential or token ledger. It checks the daemon's socket, `health` and a `register` as
the agents' user, whose reply must carry no token. Every known credential file is checked by name,
never by glob, with `test -e <file> && ! sudo -u agent test -r <file>`: each broker's
`/etc/crewbroker/<crew>-<server_id>.credential` and `/var/lib/crewbroker/<crew>-<server_id>/tokens.json`,
the daemon's `/var/lib/crewchat/tokens/verify-canary.json`, `/etc/crewchat/discord.credential`
with Discord enabled, a configured squawkie credential, and the kiosk's
`/etc/crewkiosk/model.credential`. On a host with `crewkiosk` it also checks the kiosk socket,
that the daemon runs with no new privileges, and the same files with `crewkiosk` in place of the
agents' user: `crewkiosk` may read its own model credential and nothing else (R-08).

```bash
set -euo pipefail
: "${CREWBROKER_INSTANCES:?list each broker as <crew>:<server_id>}"
make verify-install
```

It prints one `PASS` or `FAIL` line per check and exits non-zero on any `FAIL`.

## Rollback

**To the previous release.** Check out the previous release tag and run the three install blocks
again from that checkout. The configuration and the state are kept: `/etc/crewchat`,
`/etc/crewbroker`, `/etc/crewkiosk`, `/var/lib/crewchat` and `/var/lib/crewbroker` are never
touched by an install. Then run "Verify the install". If a release changed a unit, the older
release's unit replaces it, because each block installs its unit afresh.

**A single broker instance.** Stop it with `sudo systemctl disable --now crewbroker@<crew>-<server_id>`.
The daemon then treats that crew's Mattermost traffic as unresolvable and drops it (R-22), and
Discord and squawkie keep flowing. Its agents keep their current tokens until they expire or are
revoked.

**Remove the install entirely**, in this order:

```text
sudo systemctl disable --now crewchat crewkiosk.socket
for entry in $CREWBROKER_INSTANCES; do
  sudo systemctl disable --now "crewbroker@${entry%%:*}-${entry#*:}"
done
sudo rm -f /etc/systemd/system/crewchat.service /etc/systemd/system/crewbroker@.service \
  /etc/systemd/system/crewkiosk.socket /etc/systemd/system/crewkiosk@.service \
  /etc/tmpfiles.d/crewbroker.conf /usr/local/libexec/crewkiosk-run
sudo systemctl daemon-reload
sudo rm -rf /opt/mattermost-crew
```

Removing the code does not revoke anything on the Mattermost server. Before deleting
`/var/lib/crewbroker`, revoke the bot accounts' tokens (each broker's `tokens.json` lists their
ids) or deactivate the bot accounts; until then a token stays live until its native expiry, or
indefinitely on a server without native expiry (R-07). Reverting the broker account's role patch
on a partner's production server is that human partner's call (CP-03). Then remove the users, the
configuration and the state if the host is being retired.
