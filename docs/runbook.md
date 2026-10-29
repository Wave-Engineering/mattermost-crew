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

Run every block as a user with `sudo` (the verify step needs `sudo -u` the agents' user too, or
root; see [Verify the install](#verify-the-install)), from the root of a checkout of the release
tag:

```text
git clone --branch <tag> https://github.com/Wave-Engineering/mattermost-crew
cd mattermost-crew
export CREWBROKER_INSTANCES="oaw:oaw-dev"
```

## Prerequisites

The host needs:

- **Linux with systemd**, `sudo`, `git`, `tar`, `jq` (the verify step uses `jq`) and `acl`
  (`setfacl` at `/usr/bin/setfacl`, and `getfacl`; the daemon's unit sets the socket's ACL).
- **Python 3.12** and **uv**. The install uses the system `python3.12` when there is one;
  otherwise uv downloads 3.12 into `/opt/mattermost-crew/python`, which every service user can
  reach. `uv` must be on the installing user's `PATH`.
- **The agents' OS user** (`AGENT_USER`, default `agent`). The install gives it two ways to
  reach the daemon, described below.
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

**Two ways onto the daemon socket.** The install adds `AGENT_USER` to `crewchat-clients`, but a
process keeps the groups it started with. On a workstation with lingering on (`Linger=yes`), whose
desktop starts apps as systemd scopes, even a new login keeps the boot-time group list, so the
agents' live sessions would not get the group until a reboot. So the daemon's unit also sets a
default ACL on `/run/crewchat` (the drop-in
`/etc/systemd/system/crewchat.service.d/agent-acl.conf`), and the socket comes out with
`user:<AGENT_USER>:rw-`: the ACL serves `AGENT_USER`'s sessions at once, without a re-login, and
the group serves containers and new logins. The same ACL sets `group::rw-`; without it the socket
inherits the directory's `r-x` for its group, and every client that has only the group, such as a
container on `--group-add`, is refused. Don't use `sg crewchat-clients` as a workaround: it makes
the group the primary gid, and an ssh `KnownHostsCommand` that calls `setresgid` then fails, so
git over SSH breaks.

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
  daily rotation; a revoke that fails is retried at each later `register`. Its bot account
  stays, inactive once the broker's 30-day idle sweep reaches it.

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
| `/etc/crewkiosk/model.credential` | the kiosk's model API key: the key alone, one line (kiosk hosts only). `ANTHROPIC_API_KEY=<key>` is also accepted; any other line holding an `=` is refused, so a key that itself contains `=` can't be used, and so is one with whitespace in it: write it with Unix line endings, and no padding | 0600, root, in `/etc/crewkiosk` | `crewkiosk`, 0600 |
| `/etc/crewchat/discord.credential` | the Discord credential (only with Discord enabled) | 0600, root | `crewchat`, 0600 |

For example, for the broker's token, without it passing through the shell's history:

```text
sudo install -d -m 0755 /etc/crewbroker
sudo install -m 0600 -o root -g root /dev/null /etc/crewbroker/oaw-oaw-dev.credential
sudo tee /etc/crewbroker/oaw-oaw-dev.credential >/dev/null
<paste the token, then Enter and Ctrl-D>
```

Write a file in two steps like this, never with `install … /dev/stdin`: `install` sets the mode
and owner (and empties the file), then `tee` writes the content. Ubuntu's Rust coreutils
`install` (uutils) can't install from a pipe over a file that already exists (`install: No such
file or directory`), so a rewrite, or any upgrade, would fail (#123).

### `/etc/crewkiosk/model.conf`

Optional, kiosk hosts only, and not a secret. It names the model the kiosk runs on and,
optionally, a model gateway that serves the Anthropic Messages API at `<base_url>/v1/messages`
and takes the key as `x-api-key` (a LiteLLM proxy, for example). Write it as 0600, root, in
`/etc/crewkiosk`; the kiosk install makes it 0640, `root:crewkiosk`.

```text
# The model gateway. Optional: without it the kiosk calls the Anthropic API directly.
base_url=https://llm.example.lan
# The pinned model. Optional, default claude-haiku-5-5: a full model name, or with base_url
# set, the gateway's name for one.
model=haiku-latest
```

Only `KEY=value` lines, blank lines and `#` comments, with no spaces around the `=`. The runner
reads the file, never sources it, and refuses to run on an unknown key (it reports the line's
number, never its text), a `base_url` that is not `https://` or holds a space, or a `model`
outside `A-Z a-z 0-9 . _ : -`. With no file the model is `claude-haiku-5-5` and there is no
gateway. The runner always pins the model: it passes `--model <model>` and sets every model
variable Claude Code reads to it, so the CLI never asks the gateway for a model it doesn't allow.
Claude Code sends those variables' values as model IDs, so with no `base_url` the runner refuses
one of Claude Code's own aliases (`haiku`, `sonnet`, `opus`, `fable`, `opusplan`, `default`,
`best`) as `model`; behind a gateway the value is the gateway's name, whatever it is. The runner
also turns off Claude Code's model switch on a flagged request (`switchModelsOnFlag`), so a
request the safety classifiers flag ends as an error, not a re-run on another model.

## Install the daemon

Creates `crewchat-clients` and `crewchat`, puts the agents' user in `crewchat-clients`, installs
the code into `/opt/mattermost-crew`, then installs `crewchat.service` with the drop-in that
gives the agents' user its ACL on the socket (Prerequisites), and starts it.

```bash
set -euo pipefail
AGENT_USER="${AGENT_USER:-agent}"
UV="$(command -v uv)"
sudo test -r /etc/crewchat/servers.toml || { echo "write /etc/crewchat/servers.toml first (Configure)" >&2; exit 1; }
id "$AGENT_USER" >/dev/null || { echo "no user $AGENT_USER (Prerequisites)" >&2; exit 1; }
[[ -x /usr/bin/setfacl ]] || { echo "no /usr/bin/setfacl: install acl (Prerequisites)" >&2; exit 1; }

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
# The agents' live sessions reach the socket by ACL, without a re-login; g::rw keeps the group's
# access, which the default ACL would otherwise cap at the directory's r-x (Prerequisites).
sudo install -d -m 0755 -o root -g root /etc/systemd/system/crewchat.service.d
# Two steps, never `install /dev/stdin`, which fails over an existing file on uutils (#123).
sudo install -m 0644 -o root -g root /dev/null /etc/systemd/system/crewchat.service.d/agent-acl.conf
printf '[Service]\nExecStartPre=+/usr/bin/setfacl -d -m u:%s:rw,g::rw /run/crewchat\n' "$AGENT_USER" |
  sudo tee /etc/systemd/system/crewchat.service.d/agent-acl.conf >/dev/null
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
# crewbroker must traverse /etc/crewbroker to reach its credential; this also fixes a directory
# that already exists at 0700.
sudo install -d -m 0755 -o root -g root /etc/crewbroker

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
credential to `crewkiosk`, makes `/etc/crewkiosk/model.conf` (when there is one) readable by its
group, and restarts `crewkiosk.socket`, so a re-run applies the unit it installs. The daemon
connects to `/run/crewkiosk/kiosk.sock` and systemd runs one answer per connection as
`crewkiosk`, so the daemon never changes user and keeps `NoNewPrivileges=yes`.

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
if sudo test -e /etc/crewkiosk/model.conf; then
  sudo chown root:crewkiosk /etc/crewkiosk/model.conf
  sudo chmod 0640 /etc/crewkiosk/model.conf
fi
sudo install -D -m 0755 -o root -g root packaging/crewkiosk-run /usr/local/libexec/crewkiosk-run
sudo install -m 0644 -o root -g root packaging/crewkiosk.socket packaging/crewkiosk@.service /etc/systemd/system/
sudo systemctl daemon-reload
# Restart, not `enable --now`: that leaves an active socket on its old unit. With Accept=yes an
# answer in flight runs in its own crewkiosk@ instance, so the restart doesn't cut it off.
sudo systemctl enable crewkiosk.socket
sudo systemctl restart crewkiosk.socket
sudo test -S /run/crewkiosk/kiosk.sock
echo "crewkiosk.socket is listening on /run/crewkiosk/kiosk.sock"
```

## Verify the install

`make verify-install` runs `scripts/verify-install.sh`. It checks, per broker instance, the
socket's mode and group, a `mint` for a `verify-canary` bot account as the daemon's user (its
token is revoked straight away and never printed), and that the agents' user cannot read the
broker's credential or token ledger. It checks the daemon's socket, `health` and a `register` as
the agents' user, whose reply must carry no token. It checks the daemon socket's ACL
(`user:<AGENT_USER>:rw-`, `group::rw-` and `mask::rw-`, from the `agent-acl.conf` drop-in), and
then three callers through `setpriv`, each with exactly the ids named. The agents' user without
`crewchat-clients` must get `health` (the ACL), and so must a uid with only that group's gid (a
container). A uid with neither must be refused. Every known credential file is checked by name,
never by glob, with `test -e <file> && ! sudo -u agent test -r <file>`: each broker's
`/etc/crewbroker/<crew>-<server_id>.credential` and `/var/lib/crewbroker/<crew>-<server_id>/tokens.json`,
the daemon's `/var/lib/crewchat/tokens/verify-canary.json`, `/etc/crewchat/discord.credential`
with Discord enabled, a configured squawkie credential, and the kiosk's
`/etc/crewkiosk/model.credential`. On a host with `crewkiosk` it also checks the kiosk socket,
that the daemon runs with no new privileges, that `/etc/crewkiosk/model.conf`, when there is
one, is 0640 `root:crewkiosk`, and the same files with `crewkiosk` in place of the agents'
user: `crewkiosk` may read its own model credential and nothing else (R-08). Last, it
deactivates the `verify-canary` bot on every broker instance with the broker's `deactivate`, and
checks the server's `delete_at` came back above 0, so a verify leaves no active bot on the
server. The next verify reactivates it.

```bash
set -euo pipefail
: "${CREWBROKER_INSTANCES:?list each broker as <crew>:<server_id>}"
make verify-install
```

It prints one `PASS` or `FAIL` line per check and exits non-zero on any `FAIL`.

Run it as root, or as an operator who may `sudo -u` both the agents' user and root. On a host
whose sudoers grants the operator `(root)` only, typically an operator who is the agents' user,
verify prints the line to re-run with and exits 1 before any check. Run that line as the same
operator:

```text
sudo env AGENT_USER=<agents' user> CREWBROKER_INSTANCES="<crew>:<server_id> ..." make verify-install
```

## Upgrade

Read the release's CHANGELOG entry first: an upgrade note there names any new prerequisite or
step. Then check out the new release tag and run the install blocks again from that checkout,
the daemon's and the broker's, and the kiosk runner's on a kiosk host, then "Verify the
install". Each block is written to run over an existing install: it creates only what is
missing, overwrites every file it installs, and restarts its units. Configure's files are
yours and are kept; rewrite one only when the upgrade note says so, in two steps as Configure
shows. CI runs every block twice in a row on a fresh host, so the second run is the upgrade
(IT-20).

## Rollback

**To the previous release.** Check out the previous release tag and run the three install blocks
again from that checkout. The configuration and the state are kept: `/etc/crewchat`,
`/etc/crewbroker`, `/etc/crewkiosk`, `/var/lib/crewchat` and `/var/lib/crewbroker` are never
touched by an install. Then run "Verify the install". If a release changed a unit, the older
release's unit replaces it, because each block installs its unit afresh.

Rolling back to v0.2.3 or earlier on a host whose `install` is uutils fails over the existing
files: those releases' blocks write with `install … /dev/stdin`, which uutils refuses when the
destination exists (`install: No such file or directory`). Move the file the block names aside
(`sudo mv <file> <file>.v0.2.4`) and re-run the block, once per file it stops on. Only the
blocks' own files are involved, never Configure's.

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
sudo rm -rf /etc/systemd/system/crewchat.service.d
sudo systemctl daemon-reload
sudo rm -rf /opt/mattermost-crew
```

Removing the code does not revoke anything on the Mattermost server. Before deleting
`/var/lib/crewbroker`, revoke the bot accounts' tokens (each broker's `tokens.json` lists their
ids) or deactivate the bot accounts; until then a token stays live until its native expiry, or
indefinitely on a server without native expiry (R-07). Reverting the broker account's role patch
on a partner's production server is that human partner's call (CP-03). Then remove the users, the
configuration and the state if the host is being retired.
