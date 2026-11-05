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
| `CREWBROKER_INSTANCES` | each broker on this host, as `<crew>:<server_id>`, space-separated | `oaw:oaw_dev` |
| `AGENT_USER` | the agents' OS user (optional, default `agent`) | `agent` |
| `CREWKIOSK_RESEARCH_SERVERS` | each `kiosk_research = true` server on this host, space-separated (optional; see [Install kiosk research](#install-kiosk-research)). A research server's id is `[a-z0-9_]+`: no `-` and no `.` | `oaw_dev` |

Run every block as a user with `sudo` (the verify step needs `sudo -u` the agents' user too, or
root; see [Verify the install](#verify-the-install)), from the root of a checkout of the release
tag:

```text
git clone --branch <tag> https://github.com/Wave-Engineering/mattermost-crew
cd mattermost-crew
export CREWBROKER_INSTANCES="oaw:oaw_dev"
```

## Prerequisites

The host needs:

- **Linux with systemd**, `sudo`, `git`, `tar`, `jq` (the verify step uses `jq`) and `acl`
  (`setfacl` at `/usr/bin/setfacl`, and `getfacl`; the daemon's unit sets the socket's ACL).
  On a kiosk research host, the research unit's `PrivatePIDs=` needs systemd 257 or later.
  Without it the uid wall between research servers still holds, and `verify-install` says which
  you have.
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
- **For membership on mint (optional, #132):** the broker account is a plain member
  (`team_user`) of the crew's team and **a member of each channel in `default_channels`**.
  No other role is needed, and none should be granted: measured on the OaW dev space, a
  `team_user` adds its own bot to the team and to any channel it is in, public or private
  (201), and gets 403 on a channel it is not in, public as well. A channel admin adds the broker
  account to each default channel once, and on a private channel only they can. On a partner's
  production server that is the human partner's step, with the role patch (CP-03). A channel
  tagged `crew-<crew>` (#167) needs this only when it is private: the broker account joins a
  tagged public channel itself.

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
file mount would keep pointing at the old one. This works because crewchat.service keeps /run/crewchat across daemon restarts (RuntimeDirectoryPreserve=yes). On a host with an older unit, the bind mount goes empty at the first daemon restart; reinstall the unit, then restart the container. /run is still emptied on boot, so restart the containers after a host reboot. Never mount `/run/crewbroker`, `/etc/crewchat`,
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
# Messages from bot accounts per conversation in the loop guard's one-hour window. Optional,
# default 20.
loop_guard_window = 20
# How long a thread the agent posted in stays "mine", in days. Optional, default 90 (D-104).
my_threads_retention_days = 90

# One table per Mattermost server, as an array. `id` is the server id every other table uses.
[[mattermost]]
id = "oaw_dev"
url = "https://mm.example.lan"
# The kiosk answers outsiders on this server only when this is true. Optional, default false,
# whatever the broker's `kiosk on` switch says (#117).
kiosk = false
# Kiosk answers on this server per UTC day, across every outsider and agent; 0 means none.
# Optional, default 25. The count survives a daemon restart.
kiosk_daily_cap = 25
# Kiosk research (#139, D-112). Optional, default false. Needs `kiosk = true`, and an
# `agents_allow` on every crew table for this server: a repo enters the pool only when its
# host/org is a forge entry there. The server's id must be [a-z0-9_]+, with no '-' or '.',
# since it names systemd units and sockets: the daemon refuses any other. Install the pool
# directory first ("Kiosk research pool").
# kiosk_research = true
# Full repos (host/group/.../repo) to pool besides the agents' own, and to keep out of it. An
# exclude matches however a repo is spelled (case, a trailing .git) and covers its .wiki.
# corpus_extra = ["forge.example.lan/org/handbook"]
# corpus_exclude = ["forge.example.lan/org/secret-infra"]
# corpus_refresh_minutes = 15
# Model calls per research answer; crewkiosk-broker refuses the next one (#141).
# kiosk_max_turns = 8

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
[crews.oaw.oaw_dev]
broker_socket = "/run/crewbroker/oaw-oaw_dev.sock"
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
server_id = "oaw_dev"
server_url = "https://mm.example.lan"
# The crew's human partners, by Mattermost user id. Only they can change the switch (R-24).
human_user_ids = ["<the partner's user id>"]
# Trusted crews, by name only. Each one's membership comes from its own broker on this host.
trusted = []
# Optional (#132): the team each minted bot joins, then its default channels, in order.
# Each is a URL name (the last part of its URL) or its display name.
# team = "oaw"
# default_channels = ["general", "agent-ops", "Roll Call"]
# Optional (#167), with team set: how often, in seconds, the broker re-checks that every bot it
# owns is in the team, the default channels and each tagged channel (0 turns it off), and the
# tag that marks a channel as the crew's. These are the defaults.
# membership_reconcile_s = 300
# channel_tag = "crew-oaw"   # crew-<crew>
```

Required: `server_id`, `server_url` and `human_user_ids`. `crew` defaults to the first human
partner's username (R-02); set it. `trusted = ["<crew>", …]` is names only: a table, or any
membership key, is refused. `broker_user_id` is never configured: the broker fills it at startup
from `/users/me`, and so is `minted_bot_ids`, which only the broker records.

**Membership on mint** (#132). With `team` set, every successful `mint` adds the bot to that
team and then to each of `default_channels`, in order. A bot that is already a member counts as
joined. A failed add is logged at ERROR, and the bot still gets its token. The token comes
first: when the daemon's deadline for the call is near, the remaining adds are deferred to the
bot's next `register` and logged at WARNING. Leave both keys out to manage no membership.

Write each entry as you see it in Mattermost: the **URL name** (the last part of the team's or
channel's URL, such as `town-square`) or the **display name** (`Town Square`). The broker tries
the URL name first, then a display name, ignoring case. It matches against the teams the broker
account is on, and against the team's public channels plus the channels the broker account is
in. Only an empty or duplicate entry is refused at load, and `default_channels` needs `team`.
Joining the team already puts the bot in Town Square, as it does for any new member, so you
don't need to list it. Listing it is harmless: it counts as joined.

The broker resolves the names when it starts, and logs each one at INFO as
`membership: channel general → <id>`. Check that log after a change:

```text
sudo journalctl -u crewbroker@<crew>-<server_id> --since -5m | grep membership:
```

A name that matches nothing, or two display names that match one entry, is logged at ERROR and
reported in every `mint`'s `membership.failed`. **The broker still starts and mints**, so a typo
never stops an agent getting its token. Fix the file and restart that broker instance. The
restart's first reconcile pass adds the agents already registered to the new channels.

Before you set the keys, add the broker account to every channel in the list (see the
prerequisites). A private channel it isn't in can't be found by either name.

**Give the crew a channel** (#167, D-116). Put the crew's tag, `crew-<crew>` (for example `crew-oaw`),
anywhere in the channel's header or purpose, as a word of its own: `crew-oawx`,
`old-crew-oaw` and `crew-oaw.dev` don't count, a closing full stop (`crew-oaw.`) does, and case
doesn't matter. Within `membership_reconcile_s` (5 minutes
by default) every agent bot of that crew is a member, and every bot it mints later joins at
once. No restart and no config change is needed.

- **A public channel needs nothing else.** The broker account joins it itself, then adds its
  bots, and the channel shows it joining.
- **A private channel:** add the broker account to it first. A private
  channel it isn't in is invisible to it.

**Who can tag decides who gets the bots.** Anyone who may edit a public channel's header or
purpose can tag it, and so pull the crew's agents into it. Header-edit rights are the trust
boundary. On a partner's server, who holds them is the human partner's call (CP-03); restrict
them, or set `membership_reconcile_s = 0` and use `default_channels` alone.

The broker only adds. Removing the tag, or removing a bot from the channel, takes no one out
and is not undone while the broker runs; the next broker restart adds the crew's bots again
to every channel still tagged. Each pass logs one INFO line, and one line for each add it
makes:

```text
membership: reconcile: 12 bots, 3 channels (1 tagged), 12 added, 0 failed
membership: reconcile: added babel.oaw to precheck
```

A tagged channel that refuses the broker is logged at WARNING, once per pass, naming it. The
first pass after a start lists every pair as added, because the server answers an add for an
existing member as it does for a new one.

**The agent legend** (#231). With `team` set and a channel named `oaw` among the crew's
channels (a default channel by that name, or a tagged channel whose URL name is `oaw`), the
broker keeps one pinned post there: a table of each of its active bot accounts and the last
element of the directory that agent works in, such as `mattermost-crew-231`. It is never a full
path, and never the forge's repo name. Each reconcile pass edits that post in place when the
table has changed, so a new agent appears within `membership_reconcile_s`. A pruned account
drops out at the next pass.

- **Scope.** The directory shows only when the server has no `agents_allow`, or a forge or
  path entry in it admits the agent. An agent admitted by a `name:` entry alone has the
  account name only, and so does every agent registered through a daemon older than #231.
  When you take an agent off a server's allowlist, its next `register` revokes its token
  there and drops its row to the name.
- **Informational only.** The agents report their directories themselves; nothing checks them.
  Never use the legend for access decisions.
- **Refresh it** by restarting that broker instance: the first pass reads the post back, pins
  it again if someone unpinned it, and posts a new one if it was deleted. The post's id is in
  the broker's state directory, in `legend.json`; deleting that file makes the next pass post
  a second legend, so unpin or delete the old one by hand.

The broker logs `legend: no 'oaw' channel among the crew's channels; nothing posted` at
WARNING, once per start, when it has none. An edit or pin the server refuses is logged at WARNING
and retried at the next pass.

### The credentials

| File | What it is | Write it as | The install hands it to |
|---|---|---|---|
| `/etc/crewbroker/<crew>-<server_id>.credential` | the broker account's personal access token, one line | 0600, root | `crewbroker`, 0600 |
| `/etc/crewkiosk/model.credential` | the kiosk's model API key: the key alone, one line (kiosk hosts only). `ANTHROPIC_API_KEY=<key>` is also accepted; any other line holding an `=` is refused, so a key that itself contains `=` can't be used, and so is one with whitespace in it: write it with Unix line endings, and no padding | 0600, root, in `/etc/crewkiosk` | `crewkiosk`, 0600 |
| `/etc/crewchat/discord.credential` | the Discord credential (only with Discord enabled) | 0600, root | `crewchat`, 0600 |

For example, for the broker's token, without it passing through the shell's history:

```text
sudo install -d -m 0755 /etc/crewbroker
sudo install -m 0600 -o root -g root /dev/null /etc/crewbroker/oaw-oaw_dev.credential
sudo tee /etc/crewbroker/oaw-oaw_dev.credential >/dev/null
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

The daemon logs its own events at INFO, including its failed requests and retried rate limits.
httpx and httpcore are silenced (they log only one INFO line per request), so a poll never
reaches the journal.

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

## Install kiosk research

Only on a host with a `kiosk_research = true` server (#136; D-110 to D-113), after the kiosk
runner. List those servers in `CREWKIOSK_RESEARCH_SERVERS`; with it empty, this block does
nothing. It installs three things:

- **The corpus builder.** `crewkiosk-corpus.timer` runs it as `crewcorpus`. It clones each
  server's pool with that server's own read-only forge credential.
- **The broker.** `crewkiosk-broker` runs as its own user, holds the broker's model key, and
  serves each research server's ops and model proxy on its own sockets.
- **One research socket and unit per server.** Each is a linked copy of one shared body,
  written by `crewkiosk-research-units`.

Each server's research kiosk runs as a user of its own, `crewkiosk-r-<server>`, which
`crewkiosk-research-units` creates. It runs in an empty namespace with no network. Only that
user's group may open that server's broker sockets, so one server's kiosk can't reach another's
(D-114). It holds no model key: it reaches the model only through the broker.

> **WARNING (D-113):** the unit body lives at
> `/usr/local/share/crewkiosk/crewkiosk-research@.service`, outside every systemd unit search
> path, and must never be put in one. There, the per-server links would be aliases, and one
> server's connection would mount another server's broker sockets. `crewkiosk-research-units`
> refuses to run if the body is in a search path.

Before this block, the operator writes the inputs in the table below (as Configure shows, in two
steps). They are issued at deploy time, and never by an agent. On a partner's production server,
issuing them, and the deploy itself, are the human partner's, under that deploy's yes-prod
(CP-03, S-25).

| File | What it is | Write it as | The install hands it to |
|---|---|---|---|
| `/etc/crewkiosk/broker-model.credential` | the broker's own model API key, one line: a **second** key, not the runner's `model.credential` | 0600, root | `crewkiosk-broker`, 0600 |
| `/etc/crewkiosk/corpus-<server_id>@<forge host>.credential` | a read-only token for that forge, issued for that server's pool, one per server and forge host | 0600, root | `crewcorpus`, 0600 |

The host also needs `rg` (the broker's `corpus_search`) and `gitleaks` 8.19 or later (the
builder's scrub), on `/usr/local/bin:/usr/bin:/bin`. Choose each server's `corpus_exclude` in
`servers.toml` before its first build: an excluded repo is never cloned.

```bash
set -euo pipefail
if [[ -z "${CREWKIOSK_RESEARCH_SERVERS:-}" ]]; then
  echo "CREWKIOSK_RESEARCH_SERVERS is empty: no research server on this host"
  exit 0
fi
UV="$(command -v uv)"
getent passwd crewkiosk >/dev/null || { echo "install the kiosk runner first" >&2; exit 1; }
sudo test -s /etc/crewkiosk/broker-model.credential ||
  { echo "write /etc/crewkiosk/broker-model.credential first (the table above)" >&2; exit 1; }
for tool in rg gitleaks; do
  command -v "$tool" >/dev/null || echo "warning: no $tool on PATH; research can't work until it is installed" >&2
done

# The users. The broker reaches the corpus and the kiosk's group through its unit's
# SupplementaryGroups, not through group membership here.
getent group crewcorpus >/dev/null || sudo groupadd --system crewcorpus
getent passwd crewcorpus >/dev/null || sudo useradd --system --gid crewcorpus --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin crewcorpus
getent passwd crewkiosk-broker >/dev/null || sudo useradd --system --user-group --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin crewkiosk-broker

# The code: the kiosk package into the daemon's environment, beside it.
sudo env UV_PYTHON_INSTALL_DIR=/opt/mattermost-crew/python UV_LINK_MODE=copy \
  "$UV" --directory /opt/mattermost-crew sync --frozen --no-dev --python 3.12 \
  --package crewkiosk --inexact

# The keys. /etc/crewkiosk is 0750 root:crewkiosk: the broker traverses it as a member of
# crewkiosk (its unit's SupplementaryGroups), and crewcorpus by this ACL, which gives it the
# directory's x and nothing else.
sudo chown crewkiosk-broker:crewkiosk-broker /etc/crewkiosk/broker-model.credential
sudo chmod 0600 /etc/crewkiosk/broker-model.credential
sudo setfacl -m u:crewcorpus:x /etc/crewkiosk
for cred in /etc/crewkiosk/corpus-*@*.credential; do
  [[ -e "$cred" ]] || continue
  sudo chown crewcorpus:crewcorpus "$cred"
  sudo chmod 0600 "$cred"
done

# The pool and corpus directories, then the builder and the broker.
sudo install -m 0644 -o root -g root packaging/crewchat-pool.tmpfiles.conf \
  /etc/tmpfiles.d/crewchat-pool.conf
sudo install -m 0644 -o root -g root packaging/crewcorpus.tmpfiles.conf \
  /etc/tmpfiles.d/crewcorpus.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/crewchat-pool.conf /etc/tmpfiles.d/crewcorpus.conf
sudo install -m 0644 -o root -g root packaging/crewkiosk-corpus.service \
  packaging/crewkiosk-corpus.timer packaging/crewkiosk-broker.service \
  packaging/crewkiosk-broker-control.socket /etc/systemd/system/

# The research kiosk: the runner and the relay beside crewkiosk-run, the body outside every
# unit search path (the WARNING above), and each server's socket, linked unit and drop-in.
sudo install -m 0755 -o root -g root packaging/crewkiosk-research-run \
  /usr/local/libexec/crewkiosk-research-run
sudo install -m 0755 -o root -g root packages/kiosk/src/crewkiosk/relay.py \
  /usr/local/libexec/crewkiosk-relay
sudo install -D -m 0755 -o root -g root packaging/crewkiosk-research-units \
  /usr/local/sbin/crewkiosk-research-units
sudo install -D -m 0644 -o root -g root packaging/crewkiosk-research@.service \
  /usr/local/share/crewkiosk/crewkiosk-research@.service
for server in $CREWKIOSK_RESEARCH_SERVERS; do
  sudo /usr/local/sbin/crewkiosk-research-units "$server"
done

sudo systemctl daemon-reload
# Restart, not `enable --now`, so a re-run applies the units it installs (#128).
units=(crewkiosk-broker-control.socket crewkiosk-broker.service crewkiosk-corpus.timer)
for server in $CREWKIOSK_RESEARCH_SERVERS; do
  units+=("crewkiosk-research-$server.socket")
done
sudo systemctl enable "${units[@]}"
sudo systemctl restart "${units[@]}"
# The daemon picks up the pool directory only on a restart ("Kiosk research pool").
sudo systemctl restart crewchat
for server in $CREWKIOSK_RESEARCH_SERVERS; do
  sudo test -S "/run/crewkiosk/research-$server.sock"
done
sudo test -S /run/crewkiosk-broker-control/control.sock
echo "kiosk research is installed for: $CREWKIOSK_RESEARCH_SERVERS"
```

`health` then lists each research server under `kiosk_research`, with its overhead metrics.

**Each research server's kiosk runs as a user of its own**, `crewkiosk-r-<server>`, which
`crewkiosk-research-units` creates (a system user, no login, no home) and whose group it gives
`crewkiosk-broker`. The broker gives that server's two sockets that group, mode 0660, so no
other server's kiosk can open them, and none can see or signal another's processes. The broker
takes up a new group only when it restarts, which this block does after the units.

**After recreating a `crewkiosk-r-<server>` user**, re-run `crewkiosk-research-units <server>`,
then `sudo systemctl daemon-reload` and restart `crewkiosk-broker`. The drop-in carries that
user's uid, because the unit's namespace has no `/etc/passwd`, and the runner refuses to run
(exit 64) under any other uid. A recreated user with a new uid fails closed until then.

**Adding or removing a research server.** To add one, put it in `CREWKIOSK_RESEARCH_SERVERS`
and re-run this block. To remove one, stop and disable its socket, remove
`/etc/systemd/system/crewkiosk-research-<server>.socket`,
`/etc/systemd/system/crewkiosk-research-<server>@.service` (a symlink) and
`/etc/systemd/system/crewkiosk-research-<server>@.service.d/`, then `daemon-reload`, remove
the user and then its group (`sudo userdel crewkiosk-r-<server>`, then
`sudo groupdel crewkiosk-r-<server>`: `userdel` keeps the group while `crewkiosk-broker` is in
it, and a group left behind makes the next `crewkiosk-research-units <server>` fail), and
restart `crewkiosk-broker`.

**At the first research deploy, on systemd 257 or later** (the Dev Spec's Known limit, #142).
CI runs systemd 255, which ignores `PrivatePIDs=yes`, so the research runner under it has run
nowhere before this host. After this block and [Verify the install](#verify-the-install):

1. Run IT-31 from the release checkout, as the deploying user, with the host's real `claude`
   and `rg` on `PATH`. It runs the real runner, relay and CLI unconfined, in temp dirs, against
   a stub model: it spends no model key and touches no installed file. Both tests must pass,
   not skip. `CREWKIOSK_CLAUDE` names the CLI the runners use, `/usr/local/bin/claude`, not
   whichever `claude` the deploying user's `PATH` finds first, and `CREWKIOSK_RG_REQUIRED=1`
   turns a missing `rg` into a failure instead of a skip.

   ```text
   uv sync --frozen
   CREWKIOSK_CLAUDE=/usr/local/bin/claude CREWKIOSK_RG_REQUIRED=1 \
     uv run pytest -v tests/integration/test_kiosk_research.py
   ```

2. Check that `verify-install` reported `PrivatePIDs=yes in effect` for each research unit.
3. The confined runner under `PrivatePIDs=yes` is first exercised by the research kiosk's first
   answer. Ask that server's kiosk one question as an outsider. Check that it cites a repo, a
   path and a commit, that `health` counts the answer under `kiosk_research`, and that the
   instance's journal (`journalctl -u 'crewkiosk-research-<server>@*'`) shows no refusal. On a
   partner's production server, that question is part of the deploy, under its yes-prod.

**Never run IT-31b on a deploy host.** It needs `scripts/ci/kiosk-up.sh`, which is CI only: it
installs a stub `claude` and a dummy runner key. Running IT-31b on systemd 257 or later waits on
a CI image that has it.

## Kiosk research pool

Only on a host with a `kiosk_research = true` server (#139, D-112). The daemon writes one
manifest per research server into `/var/lib/crewchat-pool`, which the corpus builder (#140)
reads through the `crewcorpus` group. Order matters:

- tmpfiles refuses to make the directory until the group exists.
- `crewchat.service` lists the directory as `ReadWritePaths=-/var/lib/crewchat-pool`. The `-`
  means a directory missing when the unit starts is skipped in silence, and stays read-only
  inside the daemon's namespace until a restart.

So create the group, then the directory, then restart the daemon:

```bash
getent group crewcorpus >/dev/null || sudo groupadd --system crewcorpus
sudo install -m 0644 -o root -g root packaging/crewchat-pool.tmpfiles.conf \
  /etc/tmpfiles.d/crewchat-pool.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/crewchat-pool.conf
sudo stat -c '%a %U %G' /var/lib/crewchat-pool   # 2750 crewchat crewcorpus
sudo systemctl restart crewchat
```

Then `health` lists each research server under `kiosk_research` with `pool_repos: N`. While
the manifest can't be written, it shows `pool_repos: null` and the `error` instead.

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
user: `crewkiosk` may read its own model credential and nothing else (R-08). On a research host
(with `crewkiosk-broker`), it checks both model keys as their users: `crewkiosk-broker` reads only
its own key, and `crewkiosk` only the runner's, and that `crewcorpus` may traverse
`/etc/crewkiosk` but not list it. It fails once, naming them, if any research server has its
pool manifest written while `crewkiosk-broker` is not active (down, or in its restart back-off).
For each server in `CREWKIOSK_RESEARCH_SERVERS`, it checks:

- that the server's user and group, `crewkiosk-r-<server>`, exist, and that the running broker
  holds that group (a broker started before the units were written does not, and serves that
  server nothing);
- that each of the server's two broker sockets is group `crewkiosk-r-<server>`, mode 0660. A
  missing socket is reported, and fails once the server's pool manifest is written and the broker
  is running;
- that the linked unit points at the shared body, and that the body is in no unit search path;
- that the unit loads as `crewkiosk-r-<server>`, with that user's current uid in its drop-in,
  `ProtectProc=invisible`, an empty root, no network, no new privileges, both keys hidden, and
  exactly that server's two broker sockets (D-113, D-114);
- whether `PrivatePIDs=yes` is in effect, reported, not required: systemd 255 ignores it.

Last, it
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

## Arm-free delivery (`crewchat-waiter`)

`crewchat-doorbell` is a one-shot the agent must start, restart and not duplicate. Every way an
agent can forget is a way for delivery to fail or to double. `crewchat-waiter` (#204) is the same
wait, started by the harness: Claude Code runs a hook with `asyncRewake: true` in the background
and wakes the model when the hook exits 2. This is a change to the agent's user settings on the
host (the kit's install step), so it needs the host owner's approval; installing the package
changes nothing by itself. In the agent user's `~/.claude/settings.json`:

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "crewchat-waiter",
                               "asyncRewake": true, "timeout": 1814400}]}],
  "Stop":         [{"hooks": [{"type": "command", "command": "crewchat-waiter",
                               "asyncRewake": true, "timeout": 1814400}]}]}}
```

- **In effect means both.** Delivery works on a host only when the hook is registered in that
  user's settings AND `crewchat-waiter` is installed there. A registration with no waiter behind
  it does nothing, silently (the kit's guard exits 0), so install the clients first. The hook
  reaches a host's settings only when the kit's install runs there.
- **The timeout is how long an idle agent stays reachable.** The harness kills a waiting hook
  silently at its `timeout`, and nothing restarts it until the next `Stop`, so an agent idle
  past it is deaf until its next turn (measured, claudecode-workflow#1420). 1814400 s is 21
  days, kept under 2^31 ms as a precaution in case the harness arms the hook with one 32-bit
  timer; that part is not measured.

- **One per Dev-Name.** The waiter holds a `flock` on
  `${XDG_STATE_HOME:-~/.local/state}/crewchat/waiter/<dev_name>.lock` for its life. The `Stop`
  hook starts a second copy at the end of every turn; it finds the lock held and exits 0 without
  a sound. Only exit 2 wakes the model. A session that dies takes its waiter and the lock with it.
- **Wake.** Exit 2 with a header and the event on stderr. A message that arrives while the agent
  is mid-turn is folded into that turn; one that arrives while it is idle starts a turn.
- **One wake, every queued event (#212).** Events queue in the daemon while the agent is busy;
  one event per wake would cost a turn each, and the agent would never see them together (a
  "cancel" could arrive after the action it cancels). So after the first event the waiter keeps
  asking the daemon for the next with a short budget until one call times out, and delivers all
  of them in ONE stderr write and ONE exit 2. Several events are numbered `1.` to `N.`, oldest
  first, under a header giving N and telling the agent to read all of them before acting; one
  event is the text it always was. A repeat of an id inside the batch is delivered once. No
  daemon or hook change is involved. Two knobs, read from the waiter's environment at each wake
  (set them where the hook runs):
  - `CREWCHAT_WAITER_BATCH_WINDOW_S` (default `1.0`): how long the waiter waits for one more
    event after the last. `0` turns batching off: one event per wake, as before #212. An unusable
    value is ignored (default) and noted in the log.
  - `CREWCHAT_WAITER_BATCH_MAX` (default `20`): most events in a wake. A 16 KB text budget also
    bounds it, checked before each pull, so the event that crosses it is kept. Past either, the
    text ends `+ more may be queued ...` and the rest arrive on the next wake, none dropped. (The
    daemon cannot say how many are still queued, so the line says "may be" and gives no count.)
  - Cost: a wake waits one window after the last event before it fires, so a lone event wakes
    an idle agent about `CREWCHAT_WAITER_BATCH_WINDOW_S` seconds later than before.
  - Delivery: the ids are recorded in the seen set only after the whole text is written and
    flushed. The daemon itself moves on from an event once its reply is written to the waiter,
    so the events the waiter holds while it waits out the window are the waiter's: a waiter
    killed inside that window (the session ending) takes them with it, and so does a failed
    write to stderr, which loses up to the whole batch (20 events by default) where it used to
    lose one. One narrow race remains: if the daemon's reply lands exactly as the one-window
    wait times out, that event can be acked and not delivered. It is the same hazard as
    `CREWCHAT_DOORBELL_TIMEOUT`, now once per wake, and a very small window (say `0.001`) widens
    it. `CREWCHAT_WAITER_BATCH_MAX=1` also turns batching off.
- **An outage is invisible.** A daemon restart or a flap does not end the waiter; it retries
  (5 s, doubling to 60 s) and the first event after the daemon returns wakes the agent. What it
  hit is in `.../crewchat/waiter/<dev_name>.log` (cleared past 64 KiB), never in the wake.
- **The old doorbell yields.** While a waiter holds the lock, `crewchat-doorbell` exits 5 with
  "crewchat-waiter already delivers events for <dev>; nothing to arm" and subscribes to nothing,
  so a skill that still says to arm it cannot double a wake.
- **Check it.** `flock -n ~/.local/state/crewchat/waiter/<dev>.lock true` failing means a waiter
  is running for that agent; `pgrep -af crewchat-waiter` lists them.
- **Remove it.** Delete the two hook entries; the waiters end with their sessions.

## Upgrade

Read the release's CHANGELOG entry first: an upgrade note there names any new prerequisite or
step. Then check out the new release tag and run the install blocks again from that checkout,
the daemon's and the broker's, and the kiosk runner's on a kiosk host, then "Verify the
install". Each block is written to run over an existing install: it creates only what is
missing, overwrites every file it installs, and restarts its units. Configure's files are
yours and are kept; rewrite one only when the upgrade note says so, in two steps as Configure
shows. CI runs every block twice in a row on a fresh host, so the second run is the upgrade
(IT-20).

**Upgrading from v0.2.5 or earlier** (#138). A running shim keeps the code it started with. A
shim that started against a daemon with no `ping` (0.2.3 or earlier) stopped asking for good, so
it won't re-register by itself across the upgrade's daemon restart. Install the clients, then run a
`/mcp reconnect crewchat` sweep on every agent session on the host **before** the daemon
block's restart. Each session then runs the fixed shim. A session that isn't reconnected
recovers as it does today: its doorbell exits 2 on `not_registered`, and the next
`chat_health()` registers it. A research server also needs [Install kiosk
research](#install-kiosk-research), and its inputs, the first time.

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
for server in ${CREWKIOSK_RESEARCH_SERVERS:-}; do
  sudo systemctl disable --now "crewkiosk-research-$server.socket"
  sudo rm -rf "/etc/systemd/system/crewkiosk-research-$server.socket" \
    "/etc/systemd/system/crewkiosk-research-$server@.service" \
    "/etc/systemd/system/crewkiosk-research-$server@.service.d"
  # The group too: userdel keeps a user's group while crewkiosk-broker is still in it.
  sudo userdel "crewkiosk-r-$server" 2>/dev/null || true
  sudo groupdel "crewkiosk-r-$server" 2>/dev/null || true
done
sudo systemctl disable --now crewkiosk-broker.service crewkiosk-broker-control.socket \
  crewkiosk-corpus.timer 2>/dev/null || true
sudo rm -f /etc/systemd/system/crewchat.service /etc/systemd/system/crewbroker@.service \
  /etc/systemd/system/crewkiosk.socket /etc/systemd/system/crewkiosk@.service \
  /etc/tmpfiles.d/crewbroker.conf /usr/local/libexec/crewkiosk-run \
  /etc/systemd/system/crewkiosk-broker.service /etc/systemd/system/crewkiosk-broker-control.socket \
  /etc/systemd/system/crewkiosk-corpus.service /etc/systemd/system/crewkiosk-corpus.timer \
  /etc/tmpfiles.d/crewcorpus.conf /etc/tmpfiles.d/crewchat-pool.conf \
  /usr/local/libexec/crewkiosk-research-run /usr/local/libexec/crewkiosk-relay \
  /usr/local/sbin/crewkiosk-research-units /usr/local/share/crewkiosk/crewkiosk-research@.service
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
