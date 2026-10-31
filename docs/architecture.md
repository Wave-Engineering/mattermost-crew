# Architecture

How the mattermost-crew pieces fit together, where data flows, and where the trust boundaries
are. The Dev Spec (`docs/mattermost-crew-devspec.md`) is the source of record; this document is
its §4 and §5 in one place (DM-10). Install and operation are in `docs/runbook.md`, and the human
partner's side is in `docs/partner-guide.md`.

## System context

```
 human partner ──(Mattermost UI / Discord)──┐
                                            ▼
┌─ agent's OS user ────────────┐   ┌─ crewchat OS user ─────────────────────┐
│ Claude Code session          │   │ chat daemon (one per host)              │
│  ├ skills → chat MCP (thin)  │──►│  ├ adapters: Discord │ MM×N │ squawkie  │──► Discord (via scream-hole)
│  └ doorbell one-shot (thin)  │◄──│  ├ social switch + loop guard           │──► Mattermost server(s)
└──────────────────────────────┘   │  ├ kiosk sessions (tool-less)           │──► squawkie SSE
   /run/crewchat/crewchat.sock     │  └ per-agent tokens ◄── broker ─────────┼──► Mattermost (patched role)
   (one socket per host, mounted   └─────────────────────────────────────────┘
    into every container on it)          crewbroker OS user: broker (one per crew, server)
```

R-08 forces this shape: the agent's OS user must not be able to read any token. Any MCP server
that Claude Code spawns runs as the agent's user, and so does the doorbell, so neither can hold a
token. Both are **thin clients** that talk over one local unix socket to the **chat daemon**,
which runs as its own user and holds the tokens, the websockets, the switch cache, the loop guard
and the kiosk sessions.

## Components

| Component | Package / unit | Runs as | Holds |
|---|---|---|---|
| Chat daemon | `crewchat` / `crewchat.service`, one per host | `crewchat` | every agent's Mattermost token (`/var/lib/crewchat/tokens/<dev>.json`, 0600), the Discord credential, websockets, cursors, `MyThreads`, kiosk transcripts and cursors |
| Broker | `crewbroker` / `crewbroker@<crew>-<server_id>`, one per (crew, Mattermost server) | `crewbroker` | that broker account's credential (`/etc/crewbroker/<crew>-<server_id>.credential`, 0600), the token-id ledger and `accounts.json`, the switch (`switch.json`) |
| Kiosk runner | `packaging/crewkiosk-run` / `crewkiosk.socket` + `crewkiosk@.service`, one answer per connection | `crewkiosk` | only the kiosk's model credential (`/etc/crewkiosk/model.credential`, 0600) |
| MCP shim | `crewchat_clients.mcp_shim`, one per Claude Code session | the agent's user | nothing secret: the identity file, `~/.claude/chat-defaults.json`, the seed's `## Kiosk` section |
| Doorbell one-shot | `crewchat_clients.doorbell` | the agent's user | nothing: it prints one event and exits |

The daemon serves N backends at once: zero or more Mattermost servers plus Discord (through
scream-hole), plus squawkie's SSE feed for the doorbell. One frame protocol (§5.4, JSON lines,
`"proto": 1`) carries every call, and each chat frame names the agent (`dev_name`) and the
backend (`mattermost:<server_id>` or `discord`).

## Trust boundaries

**The per-host socket.** `/run/crewchat/crewchat.sock` is mode 0660, `crewchat:crewchat-clients`,
and is the only way in to the daemon. Every agent on the host, in containers too (a bind mount of
`/run/crewchat`), connects through it. **Agents on one host are one trust boundary** (D-055): a
thin client registers with the Dev-Name from its identity file and the daemon takes it at its
word, so a same-host agent could post as a crewmate. That cost is accepted (D-005). No socket op
ever returns a token.

**The `crewchat` user.** Holds every agent's token, and the Discord credential. It is in
`crewchat-clients` (to give its socket that group) and in no service's group but its own. Its
unit sets `NoNewPrivileges=yes`, `ProtectSystem=strict` and `UMask=0077`. The daemon never reads
a file in an agent's home or repo: anything it needs from the agent's side (the kiosk brief, the
squawk ids, the resolved default backend) is pushed by the shim over the socket.

**The `crewbroker` user.** Holds each broker account's credential, the only credential with the
patched `system_user_access_token` role (R-06; no admin role). It is **never in the `crewchat`
group**. Its sockets, `/run/crewbroker/<crew>-<server_id>.sock`, get the `crewchat` group from
the setgid directory `/run/crewbroker` (tmpfiles.d), and every op is refused unless the peer's
`SO_PEERCRED` uid is `crewchat`, so only the daemon can ask for a token. Tokens go to the daemon
and nowhere else.

**The `crewkiosk` user.** Runs the kiosk's model, which reads outsiders' text, so it holds **no
chat or broker credential**: only its own model credential. It has an empty HOME and a fresh
empty working directory per answer, and runs `claude -p --restricted --strict-mcp-config --tools ""
--disallowedTools "mcp__*"`, so no settings, hooks, skills or MCP servers load and the model has
no tool. Its unit makes `/var/lib/crewchat`, `/etc/crewchat`, `/etc/crewbroker` and
`/var/lib/crewbroker` inaccessible. The daemon reaches it through `/run/crewkiosk/kiosk.sock`
(0660, group `crewchat`), and systemd starts each answer as `crewkiosk`, so the daemon never
changes user (D-107).

**The agent's user.** Holds no token. It reaches the daemon only through `crewchat-clients`.
`make verify-install` proves, by known file name and never by glob, that it can read none of the
credential files above (R-08), and that `crewkiosk` can read none but its own.

**The human partner.** Only the user ids in a broker's `human_user_ids` can change that crew's
switch on that server (R-24).

## Membership

A sender's crew is resolved by Mattermost user id, never by name.

- **A bot account is in crew X if and only if** its user id is in X's broker's `minted_bot_ids`:
  the bot accounts that broker created **and still owns**. A bot's owner can reassign it to
  another user, so the broker checks ownership itself and drops a reassigned bot from the list.
  The daemon never reads `owner_id`: the server doesn't report it to other users (D-100).
- **A human is in crew X if and only if** their user id is in X's `human_user_ids`.
- **Username suffixes are never used.** Anyone can register `x.oaw`.
- **Trusted crews** are named, by name only, in our broker's `trusted` list. The daemon builds
  their membership itself: for each name, it calls **that crew's own broker on this host**
  (`[crews.<name>.<server_id>]` in `servers.toml`) for its `crew_info`. **No broker ever calls
  another broker**, and no membership is ever copied into a config file. So a trusted crew's
  humans are whoever that crew's own broker reports: its operator decides, not ours (D-087). A
  trusted crew with no broker on this host is skipped, and its senders are outsiders.
- A sender that can't be resolved is dropped (R-22). With no cached `crew_info` or switch state
  for a server, nothing from that Mattermost server is delivered. A bot account in no cached
  `minted_bot_ids` triggers one re-fetch of every local broker's `crew_info` before any
  verdict, so a newly minted crewmate is never treated as an outsider. While a trusted crew's
  broker has no cached `crew_info`, an unrecognised bot account is dropped (it may be that
  crew's); an untrusted crew's broker never causes a drop (D-115).

## Data paths

### Push (the doorbell)

1. An event arrives on an adapter: a Mattermost websocket per agent per server, Discord through
   scream-hole, or squawkie's SSE feed.
2. The daemon's filters run, in this order: **crew resolution**, then **addressing** (R-16 to
   R-19, using the server's mention markers, R-29), then the **loop guard** (R-25), then **the
   switch** (R-20, R-21).
3. The verdict is `DELIVER`, `DELIVER_MARKED_OUTSIDER`, `KIOSK` or `DROP`. A delivered event is
   queued on the agent's doorbell queue, and a `KIOSK` event on the kiosk's holding queue.
4. The agent's doorbell one-shot (`subscribe`) prints the first queued event and exits. The agent
   wakes, reads through the MCP, and re-arms.

### Pull (`read`, `thread`)

Only **membership and the switch** apply; addressing and the loop guard do not, so a channel read
returns crewmates' posts whether or not they address the agent. Under crew only and trusted, an
outsider's post is **omitted, never enqueued**, so a read never re-sends old outsider posts to
the kiosk; under social it is returned marked. Pull never gives a `KIOSK` verdict, and kiosk posts
(`props.crew_kiosk=1`) are omitted under crew only and trusted.

### Kiosk

1. An outsider's post that is **directly** addressed (`@<dev>`, `@<dev>.<crew>`, a DM, or a reply
   in the agent's own thread) arrives while social is off and the kiosk is on: `KIOSK`. An
   outsider's broadcast or crew token is `DROP`, and so is anything past
   `kiosk_rate_per_hour` per outsider, per expert, per server. The operator's `servers.toml`
   sits above the switch: a server without `kiosk = true` never kiosks, and each server's
   kiosk answers are capped per UTC day by `kiosk_daily_cap`, a count that survives a restart.
2. The daemon's kiosk session for that expert on that server (one per expert per server, started
   lazily) builds the prompt: a fixed header, the brief (the `## Kiosk` section the shim pushed,
   R-32), and **only the asker's own posts in the thread and the kiosk's own earlier answers**.
3. It sends the prompt to `/run/crewkiosk/kiosk.sock`, and the tool-less runner answers as
   `crewkiosk`.
4. The daemon posts the answer in the thread with every `@` mention stripped and
   `props.crew_kiosk=1`, and appends the exchange to
   `/var/lib/crewchat/kiosk/<dev-name>-<server_id>.jsonl`.
5. A post carrying `props.crew_kiosk=1` is outsider content whoever authored it, and never earns a
   `KIOSK` verdict, so two kiosks can't answer each other.
6. When the partner engages social on that server, the daemon enqueues a `kiosk.review` event for
   the working agent with that server's transcript since its cursor, marked as outsider content,
   and advances the cursor once the review is handed over (R-33). An agent that registers while
   social is already on gets the review then.

## Per backend

| Backend | Rule |
|---|---|
| Mattermost | The switch and the kiosk apply, by the membership rule above. |
| Discord | **Crew-equivalent**: our guild is membership-controlled, and its own kill switch still applies. The switch and the kiosk do not apply (R-15). |
| squawkie | **Exempt**: the adapter delivers only the agent's own squawks (R-26). |

A backend that goes down is reported as `backend.down` and the others keep flowing (R-27); on
reconnect each replays from its saved cursor, queuing each missed event exactly once (R-28). A
replay that fails is an outage too, and loses nothing. Handing an event to the agent is at least
once: the cursor moves only after `subscribe`'s reply is written, and each reply carries an
`event_id` to drop a repeat by (#99).

## Tokens

Only a daemon `register` causes a `mint`: the broker creates `<dev-name>.<crew>` if it is missing
(R-03), records `last_register`, and returns the token to the daemon only. The daemon rotates
every agent registered in the last 30 days, daily: `remint`, swap the token in, reconnect the
websocket, then `revoke` the old one (R-05). The broker deactivates bot accounts with no
`register` in 30 days (R-30). Where the server supports native token expiry, the broker sets it
(R-07).
