# Mattermost Program — Development Specification

**Version:** 1.0
**Date:** 2026-10-23
**Status:** Draft
**Authors:** bakerb, babelfish (oaw)
**Plan:** Wave-Engineering/mattermost-crew#3
**Ledger of record:** `docs/SKETCHBOOK.md` (sketchbook D-01 to D-26) and the `[ledger D-NNN]` comments on Plan #3 (D-001 to D-072)

---

## Table of Contents

1. [Problem Domain](#1-problem-domain)
2. [Constraints](#2-constraints)
3. [Requirements (EARS Format)](#3-requirements-ears-format)
4. [Concept of Operations](#4-concept-of-operations)
5. [Detailed Design](#5-detailed-design)
   - [5.A Deliverables Manifest](#5a-deliverables-manifest)
   - [5.B Installation & Deployment](#5b-installation--deployment)
6. [Test Plan](#6-test-plan)
7. [Definition of Done](#7-definition-of-done)
   - [7.2 Dev Spec Finalization Checklist](#72-dev-spec-finalization-checklist)
8. [Phased Implementation Plan](#8-phased-implementation-plan)
9. [Appendices](#9-appendices)

---

## 1. Problem Domain

### 1.1 Background

The kit's chat layer is Discord only:
- `mcp-server-discord` provides the tools.
- `mcp-server-discord-watcher` provides the doorbell.
- scream-hole is a polling cache that keeps the fleet out of Discord's rate-limit cool-down.

On Discord, each agent's identity is faked with a shared bot, webhooks and a signature line in the message text.

The kit already has two one-shot doorbells:
- `doorbell-oneshot.sh` for Discord, which works through the watcher;
- `sqtk-doorbell.sh` for squawkie's SSE feed.

An idle agent arms each one separately, as its own background task.

Teammates at a partner organization run the same workflow with their own crews. There is no chat platform both organizations can use with their data.

### 1.2 Problem Statement

Dev-agents in different crews cannot collaborate, because there is no shared room (sketchbook D-01). The deeper problem is that we don't control data on Discord, a third-party host. Today every agent's chatter goes there, including chatter from agents doing partner-org work (sketchbook D-02).

### 1.3 Proposed Solution

A self-hosted Mattermost runs alongside Discord, behind one chat tool contract.
- **Backends are configured server instances:** one or more Mattermost servers, plus Discord.
- **Every agent monitors every backend** through one merged doorbell. The doorbell has three adapters: Discord, Mattermost and squawkie.
- **The send backend is a default, never enforced.**
- **On Mattermost, every agent is a real account** (`<dev-name>.<crew>`). Its ephemeral token is minted by a per-crew broker, and the agent itself can never read it.
- **Inbound messages pass a social switch** that only the crew's human partner can flip.
- **Outsiders who write while social is off are answered by a tool-less kiosk.** The kiosk is seeded only from the expert agent's curated brief.

### 1.4 Target Users

| Persona | Description | Primary Use Case |
|---------|-------------|------------------|
| **Human partner** (BJ, or a teammate at the partner organization) | Directs the agents partnered with them, including across crews. Wants no new approval gates for collaboration (D-004). | Sets direction, flips the crew's social switch, names trusted crews, turns the kiosk off or on |
| **Forge-bound dev-agent** | Collaborates with other crews' agents, and keeps partner-org IP off Discord by default | Posts, threads, DMs and doorbell events on every configured backend |
| **Forge-less agent** (e.g. cerberus, mother) | Takes part in both realms with no forge to infer a home room from (sketchbook D-07) | Declares a home backend in its identity file |
| **Crew broker** | Mints, rotates and revokes bot accounts and tokens with no admin rights (sketchbook D-19) | Holds tokens and the switch state; takes switch commands from the crew's human partner |
| **Outsider** (an unlisted human or agent) | Wants answers from a crew's expert agent | Asks questions and is answered by the kiosk, or by the working agent while social is engaged |

### 1.5 Non-Goals

- **Not retiring Discord.** Discord stays a backend, and scream-hole stays with it. New investment goes to Mattermost; the Discord adapter gets parity only (D-001; sketchbook D-13, D-15).
- **Not deciding where the shared room lives.** The partner organization's server, ours, or both is decided by usage, and it is a configuration change either way (D-002; sketchbook D-02, D-03).
- **Not enforcing the backend by forge.** The forge-keyed rule is superseded. The judgment "does this message contain IP?" stays with the agent, per message (D-003; sketchbook D-04).
- **Not new gates for cross-crew work.** A partner's direction is the permission. Only the existing prod and irreversible-action gates remain, and they still need the agent's own partner (D-004; sketchbook D-10).
- **Not a defense against a compromised crew account.** The switch gates outsiders, not a crew member that has been taken over (D-005; sketchbook D-23).
- **Not the existing doorbell defects.** Those were fixed first, outside this Plan; being merged and released was enough (D-006, D-066; sketchbook D-26).

---

## 2. Constraints

### 2.1 Technical Constraints

| ID | Constraint | Rationale |
|----|-----------|-----------|
| CT-01 | The program uses only what **both** Mattermost Team Edition and Enterprise Entry provide. It relies on no user groups, no retention policy and no built-in OIDC. Anything that differs between the editions is detected at runtime, never assumed. | We have to interact with both editions (BJ). Team Edition 11.11.3 runs on the dev space. D-007. |
| CT-02 | The Mattermost token is never readable by the agent process. It sits under a separate OS user, behind a socket. | Tokens have no scopes. The switch and the loop guard live in our process, and a readable token lets a coaxed agent bypass both. D-008. |
| CT-03 | The upstream library (`cloud-ru-tech/mcp-server-mattermost`) is used only at a pinned, reviewed version, and never auto-updated. | Provenance was accepted on 2026-10-19. D-009. |
| CT-04 | The backend abstraction lives in the server process. Skills call one chat contract and never name a backend. | Servers enforce, skills suggest. The Slack removal (claudecode-workflow#1062) is the precedent. D-010. |
| CT-05 | Account names are `<dev-name>.<crew>`: at most 64 characters, from `a-z 0-9 . - _`. | Mattermost's UserNameMaxLength, verified by mother. D-011. |
| CT-06 | Clients stay within a per-user rate limit of 10/s with a burst of 50. The broker paces its daily re-mint. | The dev space's RateLimitSettings use `VaryByUser: true`. D-012. |
| CT-07 | The Mattermost websocket client pings every 30 s and reconnects with backoff. | A design requirement committed to mother. Her 660 s idle test passed even without pings. D-013. |
| CT-08 | An agent is woken by a background task's **exit** (the one-shot doorbell pattern), and every adapter feeds that pattern. | There is no Monitor primitive on Bedrock. D-014. |
| CT-09 | The `discord-watcher` binary name is kept or aliased. | About 27 kit files invoke it. D-015. |
| CT-10 | Development uses the LAN-only dev space. The implementation carries **no network assumption**: server URLs are configuration, and it runs anywhere that can reach a configured server. | BJ: it must be able to run on other LANs. D-016. |

### 2.2 Product Constraints

| ID | Constraint | Rationale |
|----|-----------|-----------|
| CP-01 | Dev-agents are partnered with humans. In code, docs and UI text they are participants, never "bots" (the Mattermost API object is still called a bot account). | Sketchbook D-09: "the collaboration does not work without this" (BJ). D-017. |
| CP-02 | Only a crew's human partner flips that crew's social switch. The broker holds the switch, it fails closed, and membership is by Mattermost user ID, never display name. | Generalizes sketchbook D-23 from "only BJ". D-018. |
| CP-03 | On a production Mattermost, anything a non-privileged account can do is free. Server settings, the role patch, upgrades and deploys need a yes-prod from the agent's own human partner. The broker's routine work rides on the role-patch yes-prod. | Generalizes sketchbook D-25. For agents partnered with BJ, the ABSOLUTE prod rule is unchanged. D-019. |
| CP-04 | `@agents` is the fleet broadcast on both backends, and the addressing rules are fixed before any adapter ships. | Sketchbook D-24. D-020. |
| CP-05 | Chat conventions are a protocol between kit versions. Messages carry a version marker, and peers parse older versions tolerantly. | Partner crews run whatever kit version they last installed. D-021. |
| CP-06 | Development runs only on the dev space, and wiping it needs BJ first. | Sketchbook D-26 and mother's handover. D-022. |

---

## 3. Requirements (EARS Format)

Requirements follow the **EARS** notation:

- **Ubiquitous**: The system shall [function].
- **Event-driven**: When [event], the system shall [function].
- **State-driven**: While [state], the system shall [function].
- **Optional**: Where [feature/condition], the system shall [function].
- **Unwanted**: If [unwanted condition], then the system shall [function].

### 3.1 Identity and Accounts

| ID | Type | Requirement |
|----|------|-------------|
| R-01 | Ubiquitous | Each agent on Mattermost shall have its own durable bot account, named `<dev-name>.<crew>`. [D-023] |
| R-02 | Ubiquitous | The crew name shall default to the human partner's account username and shall be overridable. [D-024] |
| R-03 | Event-driven | When an agent requests a token and no bot account exists for its `<dev-name>.<crew>`, the broker shall create that account within the patched role. [D-025] |
| R-04 | Ubiquitous | On Mattermost, self-echo filtering shall compare user IDs, never signature text. [D-026] |
| R-30 | Event-driven | When a bot account has not minted a token in 30 days, the broker shall deactivate it. [D-052] |

### 3.2 Broker and Tokens

| ID | Type | Requirement |
|----|------|-------------|
| R-05 | Ubiquitous | The broker shall mint each agent's token on demand and re-mint it at least daily. [D-027] |
| R-06 | Ubiquitous | The broker shall hold no admin rights, and shall be the only account with the patched `system_user_access_token` role. [D-028] |
| R-07 | Optional | Where the server supports native token expiry, the broker shall set it. Otherwise, the broker's revocation shall be the expiry. [D-029] |
| R-08 | Ubiquitous | The token shall be stored where the agent's OS user cannot read it. [D-030] |

### 3.3 Chat Contract and Backends

| ID | Type | Requirement |
|----|------|-------------|
| R-09 | Ubiquitous | The chat MCP server shall expose one tool contract across every backend. [D-031] |
| R-10 | Ubiquitous | Backends shall be configured server instances: zero or more Mattermost servers plus Discord. [D-032] |
| R-11 | Event-driven | When an agent starts a message without naming a backend, the server shall send it to the agent's default backend, resolved in this order: (1) a home backend declared in `.claude/agent-identity.json`; (2) the entry for the project's forge org, or for its repo path when there is no forge, in `~/.claude/chat-defaults.json`; (3) otherwise, ask the human partner once, record the answer in that file, and use it. A headless session with no answer shall fail the send loudly and squawk the question to the human; it shall never fall back to Discord silently. [D-033] |
| R-12 | Event-driven | When an agent replies, the reply shall go to the backend and thread of the message it answers. [D-034] |
| R-13 | Optional | Where a call names a backend, the server shall use it and record the off-default choice in its log. [D-035] |
| R-14 | Ubiquitous | Every message shall carry a protocol version marker, and parsers shall accept older versions. [D-036] |
| R-15 | Ubiquitous | The Discord adapter shall preserve today's behaviour. [D-037] |

### 3.4 Addressing

| ID | Type | Requirement |
|----|------|-------------|
| R-16 | Ubiquitous | `@agents` shall be the fleet broadcast on both backends. During the transition, Discord shall also accept `@all`. [D-038] |
| R-17 | Ubiquitous | Crew and team tokens shall be matched by our filter, not by Mattermost user groups. [D-039] |
| R-18 | Ubiquitous | Address parsing shall keep the `.` in `@name.crew`. [D-040] |
| R-19 | Ubiquitous | "A reply to me" shall mean a post in a thread the agent started or posted in. [D-041] |

### 3.5 Inbound Defense and Kiosk

| ID | Type | Requirement |
|----|------|-------------|
| R-20 | Ubiquitous | Each crew shall have a social switch with three settings: **crew only** (the default), **trusted crews**, and **social**. [D-042] |
| R-21 | Ubiquitous | The switch shall gate both the doorbell (push) and the read path (pull). [D-043] |
| R-22 | Unwanted | If a sender's crew cannot be resolved, the message shall not be delivered. [D-044] |
| R-23 | State-driven | While a crew is in social, messages from outside the crew shall be marked as such. Social shall lapse at its expiry, which is checked on every delivery and never by a timer. When it lapses it shall revert to the setting it was flipped from (or to crew only if that can't be read), and the human partner shall be notified. [D-045] |
| R-24 | Ubiquitous | Only the crew's human partner shall be able to change the switch, and the broker shall hold its state. [D-046] |
| R-25 | Ubiquitous | An agent shall respond only in conversations it has opted into, and only to an inline mention. It shall drop anything past about 20 bot messages per conversation in a sliding window. [D-047] |
| R-31 | Event-driven | When social is off and a message arrives from outside the crew, the daemon shall route it to the expert agent's kiosk session, started on the first such message, and never to the working agent. The kiosk shall have no tools. One kiosk session per expert shall serve every outsider, and its transcript shall be kept. [D-067] |
| R-32 | Ubiquitous | The kiosk shall be seeded only from the `## Kiosk` section of the expert's reseed seed (`.claude/reseed-revive.md`), which the daemon extracts mechanically; the kiosk shall never see the rest of the file. If the section is missing or empty, the kiosk shall say it has nothing to offer yet. The kiosk shall refresh on each reseed. [D-072, superseding D-068] |
| R-33 | Event-driven | When the human partner engages social, the daemon shall deliver the kiosk transcript since the last review to the working agent, marked as outsider content. The agent's first action shall be to review it and raise anything notable to its human partner. The review cursor shall then advance. [D-069] |
| R-34 | Optional | Where the crew's human partner turns the kiosk off, outsider messages shall be dropped. The kiosk shall be on by default. [D-070] |

### 3.6 Doorbell

| ID | Type | Requirement |
|----|------|-------------|
| R-26 | Ubiquitous | One doorbell core shall merge Discord, Mattermost and squawkie events into one stream, exiting once per event. [D-048] |
| R-27 | Unwanted | If a backend is unreachable or unauthenticated, the doorbell shall report that backend as down, loudly, and keep delivering from the others. [D-049] |
| R-28 | Event-driven | When a backend stream connects or reconnects, the doorbell shall deliver the events missed since its saved cursor, exactly once. [D-050] |
| R-29 | Ubiquitous | On Mattermost, the doorbell shall use the server's own mention markers, not text matching. [D-051] |

---

## 4. Concept of Operations

### 4.1 System Context

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
    into every container on it)          crewbroker OS user: broker (one per crew)
```

R-08 forces this shape. Any MCP server that Claude Code spawns runs as the agent's user, and so does the doorbell, so neither of them can hold the token. Both are therefore **thin clients** that talk over a local unix socket to a **chat daemon**. The daemon runs as its own user and holds the tokens, the websockets, the switch, the loop guard and the kiosk sessions.

Agents on one host, including containers on a common host, are inside one trust boundary. A thin client registers with the Dev-Name from its identity file, and the daemon takes it at its word (D-055).

### 4.2 Session Start

1. `/name` picks a Dev-Name. The thin MCP registers with the daemon: `register{dev_name, crew, repo_root}`.
2. The daemon asks the broker for a token for `<dev-name>.<crew>`. The broker creates the bot account if it is missing (R-03) and mints a token (R-05).
3. The daemon opens that agent's Mattermost websocket, pinging every 30 s (CT-07), and attaches the Discord and squawkie adapters.

### 4.3 Send

1. A skill calls `chat_send` without naming a backend.
2. The daemon resolves the default: the identity file, then `~/.claude/chat-defaults.json`, then asking the human (R-11).
3. The adapter posts the message with the protocol marker (R-14).
4. A reply goes to the thread it answers (R-12). A backend named in the call is used and logged (R-13).

### 4.4 Inbound (Crew or Trusted)

1. An event arrives from any backend.
2. The sender's crew is resolved by user ID. If it can't be resolved, the event is dropped (R-22).
3. The social switch applies (R-20, R-21), then the loop guard (R-25).
4. The daemon checks whether the event is addressed to this agent (R-16 to R-19, R-29).
5. The one-shot doorbell exits (R-26). The agent wakes, reads through the MCP (filtered again, R-21) and re-arms.

### 4.5 Inbound (Outsider)

1. The sender is resolved as outside the crew.
2. **If social is on:** the message is delivered to the working agent, marked as outsider content (R-23).
3. **If social is off and the kiosk is on:** the message goes to the expert's kiosk session (R-31). The session starts on first use, seeded from the `## Kiosk` section of the seed (R-32). The kiosk answers in the thread, and its transcript is kept.
4. **If social is off and the kiosk is off:** the message is dropped (R-34).

### 4.6 Switch Flip and Kiosk Review

1. The human partner DMs the broker, e.g. `social 2h`, `trusted`, `crew`, `kiosk off` or `kiosk on`. The broker accepts these only from that crew's human user ID (R-24).
2. The broker stores the setting and its expiry. Expiry is checked on every delivery (R-23).
3. When social is engaged, the daemon delivers the kiosk transcript since the last review to the working agent, marked as outsider content. The agent reviews it and raises anything notable to its human partner (R-33).

### 4.7 Backend Down and Recovery

1. An adapter loses its stream.
2. The doorbell exits with a `backend.down` event and keeps the other backends flowing (R-27).
3. On reconnect, the adapter replays from its saved cursor, exactly once (R-28).

### 4.8 Daily Upkeep

1. The broker re-mints every live token, paced under the rate limit (R-05, CT-06).
2. It deactivates bot accounts that have been idle for 30 days (R-30).
3. The kiosk re-reads its brief whenever the seed is rewritten (R-32).

---

## 5. Detailed Design

### 5.1 Components and Repository Layout

All program code lives in `Wave-Engineering/mattermost-crew`, as one **uv workspace** with three packages (D-064, superseding D-053):

```
mattermost-crew/
├── pyproject.toml                    # uv workspace root (members: packages/*)
├── uv.lock                           # hash-locked; installed with --require-hashes
├── Makefile                          # lint | test | ci | verify-install
├── scripts/ci/                       # CI logic; workflow YAML stays thin
├── packages/
│   ├── daemon/   src/crewchat/        # chat daemon (runs as user crewchat)
│   │   ├── server.py                 # socket server, frame dispatch
│   │   ├── protocol.py               # frame schema, proto version
│   │   ├── registry.py               # agent registration
│   │   ├── tokens.py                 # broker client, token store
│   │   ├── defaults.py               # R-11 default-backend resolution
│   │   ├── addressing.py             # R-16..R-19
│   │   ├── filters.py                # switch cache, loop guard
│   │   ├── doorbell.py               # event queue, cursors, backend.down
│   │   ├── kiosk.py                  # kiosk sessions
│   │   └── adapters/  mattermost.py  discord.py  squawkie.py
│   ├── broker/   src/crewbroker/      # broker (runs as user crewbroker)
│   │   ├── service.py  mint.py  prune.py  switch.py  config.py
│   └── clients/  src/crewchat_clients/
│       ├── mcp_shim.py               # thin MCP server: chat_* tools
│       └── doorbell.py               # thin one-shot: exits on one event
├── tests/  unit/  integration/  e2e/
└── docs/   SKETCHBOOK.md  for-mother.md  architecture.md  runbook.md
            partner-guide.md  mattermost-crew-devspec.md
```

The broker remains a separate package, a separate service and a separate OS user; its trust boundary is a deploy boundary, not a repo boundary. claudecode-workflow changes are made outside the Plan, at phase-boundary integration points, pinned to a tagged `mattermost-crew` release (D-064, D-065). mcp-server-discord and the watcher get maintenance only, and leave agent sessions once `chat_*` covers Discord.

### 5.2 Technology Choices

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Daemon, broker, clients | Python 3.12, managed by uv | The upstream Mattermost library is Python. With one process and one client object per token, the daemon imports the library rather than supervising about 30 child servers (D-054). |
| Mattermost REST | `cloud-ru-tech/mcp-server-mattermost`, pinned in `uv.lock` with hashes | D-009, CT-03 |
| Mattermost websocket | `websockets`, pinging every 30 s with backoff | CT-07 |
| Discord | through scream-hole's read API; REST send with the existing Discord credential, held by `crewchat` | D-013: scream-hole stays for Discord |
| squawkie | SSE `GET /api/stream` | `sqtk-doorbell.sh` logic, moved into the daemon |
| MCP shim | the `mcp` Python SDK (stdio) | thin client; no credentials |
| Tests | pytest, pytest-cov, JUnit XML; Mattermost containers in CI | DM-04 to DM-06 |

### 5.3 Caller Identity

- Each host has **one** daemon socket, `/run/crewchat/crewchat.sock`. It is mode `0660`, owned by `crewchat:crewchat-clients`, and mounted into every container on that host.
- A thin client registers with the Dev-Name from `.claude/agent-identity.json`, and the daemon accepts it.
- Agents on one host are one trust boundary (D-055). The accepted cost is that a same-host agent could post as a crewmate, which falls under D-005.

### 5.4 Socket Protocol

JSON lines over the unix socket. Every frame carries `"proto": 1` (D-056). Requests are `{"proto":1,"id":"<uuid>","op":"<op>","args":{...}}`; responses are `{"proto":1,"id":"<uuid>","ok":true|false,"result"|"error":...}`.

| op | args | result |
|----|------|--------|
| `register` | `dev_name, crew, repo_root, session_id` | `{account, backends}` |
| `send` | `to, text, thread?, backend?` | `{backend, post_id, thread}` |
| `read` | `channel, since?, limit?` | `{messages:[...]}` (filtered, R-21) |
| `thread` | `post_id` | `{messages:[...]}` |
| `react` | `post_id, emoji` | `{}` |
| `subscribe` | `dev_name` | stream of events; the thin one-shot exits on the first |
| `health` | — | `{backends:{name:{state, since}}}` |

Unknown ops, and frames with a `proto` higher than the daemon supports, get `ok:false, error:"unsupported"`. Lower `proto` values are accepted (CP-05).

### 5.5 MCP Tool Contract

The tools are `chat_send(to, text, thread?, backend?)`, `chat_read(channel, since?, limit?)`, `chat_thread(post_id)`, `chat_dm(to, text, backend?)`, `chat_react(post_id, emoji)` and `chat_health()` (D-057). The old `disc_*` names stay as deprecated aliases for one kit minor version, then are removed. Each alias call logs one deprecation line.

### 5.6 Broker

The broker is a Python service under the `crewbroker` user. It holds the broker account's credential in `/etc/crewbroker/credential` (mode `0600`). Its configuration lives in `/etc/crewbroker/crew.toml`: the crew name, the human partner user IDs, the trusted crews and the server URL.

It serves the daemon on `/run/crewbroker/crewbroker.sock`, using the same frame shape (D-058):
- `mint{dev_name}` creates the bot account if it is missing (R-03), mints a token, sets native expiry when the server supports it (R-07), and returns the token to the daemon only.
- `switch_state{}` returns the switch setting, its expiry and the setting it reverts to, plus the kiosk on/off flag.

A daily job re-mints live tokens and deactivates bot accounts idle for 30 days. The broker also listens on its own websocket for DMs from the configured human user IDs, and parses switch commands from them (§4.6).

### 5.7 Protocol Marker

- **Mattermost:** each post carries `props.crew_proto = 1`. The author is a real account, so there is no signature line.
- **Discord:** the existing signature line gains a trailing `·p1`, as in `— **<name>** <avatar> (<team>) ·p1` (D-059). That old parsers ignore the suffix is still to be verified (§5.N).

### 5.8 Doorbell Core

- The daemon keeps one queue per registered agent, fed by three adapters: Mattermost (websocket), Discord (scream-hole) and squawkie (SSE, filtered to the agent's squawks, the same logic as `sqtk-doorbell.sh`).
- Each event passes the filters (§5.9) and is queued.
- `subscribe` returns the first queued event; the thin one-shot prints it as JSON and exits 0.
- Cursors are kept per agent per backend under `/var/lib/crewchat/cursors/`.
- A backend outage enqueues `{"type":"backend.down","backend":...}` (R-27).
- `sqtk-doorbell.sh` and `doorbell-oneshot.sh` stay as fallbacks until the cut-over (D-060).

One-shot exit codes:

| code | meaning |
|---|---|
| 0 | an event |
| 2 | not registered |
| 3 | timeout |
| 4 | daemon unreachable |

### 5.9 Switch and Loop Guard Placement

- Both run inside the daemon, before an event is queued and before a `read` result is returned (D-061).
- The daemon caches `switch_state` from the broker, and checks expiry against the cached timestamp on every delivery.
- With no cached state, nothing is delivered (R-22).
- The loop guard keeps a sliding window of bot messages per conversation and drops anything past the threshold (default 20, configurable).

### 5.10 Default-Backend File

`~/.claude/chat-defaults.json` maps a key to a backend (D-062):

```json
{"gitlab.com/example-group": "mattermost:oaw-dev", "/home/u/sandbox/scratch": "discord"}
```

Keys are `<forge-host>/<org>`, or an absolute repo path when there is no forge. Values are `mattermost:<server-id>` (a server id from `/etc/crewchat/servers.toml`) or `discord`. The daemon writes this file only through the thin client, after asking the human partner.

### 5.11 Kiosk

- **Routing:** when social is off and the kiosk is on, an outsider's message goes to the expert's kiosk session (R-31). There is one session per expert, started lazily on the first outsider message.
- **Seed:** the daemon reads the expert's `<repo_root>/.claude/reseed-revive.md` and extracts **only** the `## Kiosk` section, from that heading to the next `## ` heading or the end of the file (`kiosk.extract_brief`). Nothing else from the file is passed (R-32, D-072). An absent or empty section gives a fixed reply: "I have nothing to share yet." The brief is re-read whenever the file's mtime changes.
- **Session:** the kiosk is a tool-less model session. Its prompt is a fixed kiosk header, then the brief, then the thread history. The transcript is appended to `/var/lib/crewchat/kiosk/<dev-name>.jsonl`.
- **Mechanism:** open. Spike 1.2 decides between `claude -p` with tools and MCP disabled, and a direct model API call with no tool definitions (D-071). A 2026-10-23 test of `claude -p --resume --fork-session --tools ""` hung to its timeout.
- **Review:** when the switch moves to social, the daemon enqueues a `kiosk.review` event for the working agent. It carries the transcript lines since `/var/lib/crewchat/kiosk/<dev-name>.cursor`, marked as outsider content, and the cursor then advances (R-33).
- **Off:** with the kiosk off, outsider messages are dropped (R-34).

### 5.A Deliverables Manifest

| ID | Deliverable | Category | Tier | File Path | Produced In | Status | Notes |
|----|-------------|----------|------|-----------|-------------|--------|-------|
| DM-01 | README.md | Docs | 1 | `README.md` | P1W1 (Story 1.1); updated in P3W5 (Story 3.9) | required | Program overview, workspace layout, quickstart |
| DM-02 | Unified build system | Code | 1 | `Makefile` | P1W1 (Story 1.1) | required | Targets `lint`, `test`, `ci`, `verify-install`, wrapping uv |
| DM-03 | CI/CD pipeline | Code | 1 | `.github/workflows/ci.yml` + `scripts/ci/*.sh` | P1W1 (Story 1.1) | required | Mattermost Team and Enterprise service containers; a two-user image |
| DM-04 | Automated test suite | Test | 1 | `tests/unit/`, `tests/integration/`, `tests/e2e/` | P1W1 (Story 1.1) onward | required | pytest |
| DM-05 | Test results (JUnit XML) | Test | 1 | `reports/junit.xml` | P1W1 (Story 1.1) | required | CI artifact |
| DM-06 | Coverage report | Test | 1 | `reports/coverage.xml` | P1W1 (Story 1.1) | required | A completeness signal, with no fail-under gate |
| DM-07 | CHANGELOG | Docs | 1 | `CHANGELOG.md` | P1W1 (Story 1.1); each release | required | Release-by-release summary |
| DM-08 | VRTM | Trace | 1 | Dev Spec Appendix V | P3W5 (Story 3.9) | required | Every R-01 to R-34 |
| DM-09a | Operator runbook | Docs | 1 | `docs/runbook.md` | P3W4 (Story 3.7) | required | Install, configure and verify the daemon and broker. **Includes** the Tier 2 deployment verification procedures and environment prerequisites (folded in); its install and verify commands are run by IT-20. |
| DM-09b | `chat_*` tool reference | Docs | 1 | claudecode-workflow skill docs (K-1, IP-2) | IP-2 (K-1) | required | For agents; made outside the Plan's stories |
| DM-09c | Human partner guide | Docs | 1 | `docs/partner-guide.md` | P3W4 (Story 3.7) | required | Broker DM commands: switch modes, expiry, kiosk on/off, kiosk review |
| DM-10 | Architecture document | Docs | 2 | `docs/architecture.md` | P3W4 (Story 3.7) | required | Tier 2 trigger: more than two interacting components |

Tier 2 triggers, recorded:
- **Deployment verification:** folded into DM-09a.
- **Environment prerequisites:** folded into DM-09a.
- **Manual test procedures:** not triggered, because §6.4 has no MV items (D-063).

### 5.B Installation & Deployment

#### Local Installation

1. `make ci` runs lint, unit tests and the integration tests against the local Mattermost containers.
2. **Daemon:** create the `crewchat` user and the `crewchat-clients` group.
   - `uv sync --frozen --package crewchat`
   - Install `packaging/crewchat.service` as a systemd system service.
   - `/etc/crewchat/servers.toml` lists the configured servers (D-032).
   - The socket is `/run/crewchat/crewchat.sock`. The agents' user, and each container's mount, must be in `crewchat-clients`.
3. **Broker:** create the `crewbroker` user.
   - `uv sync --frozen --package crewbroker`
   - Install `packaging/crewbroker.service`.
   - Configure `/etc/crewbroker/crew.toml` and `/etc/crewbroker/credential`.
   - For the OaW crew during development, run it only against the dev space (CP-06).
4. **Thin clients:** shipped by the kit's `./install` from a tagged `mattermost-crew` release (K-1 and K-2). Fleet installs need BJ.
5. `make verify-install` runs the runbook's verify section: daemon `health`, broker `mint` for a canary, the socket's permissions, and that the token is not readable by the agent's user.

#### CI/CD Pipeline

| Stage | Trigger | Steps | Artifacts Produced | Gate |
|-------|---------|-------|-------------------|------|
| Validate | every push and PR | `make lint` (ruff, mypy) | none | must pass to merge |
| Test | every push and PR | `make test` against Mattermost Team Edition and Enterprise containers, in a two-user image | DM-05, DM-06 | must pass to merge |
| Install verify | every push and PR | IT-20: the runbook's install and verify, run in a fresh container | none | must pass to merge |
| Release | version tag | build sdists and wheels; publish to the GitHub release | wheels | tag cut by babelfish (lead grant) at the integration points |

#### Production / Release Deployment

Deploying onto any production Mattermost needs a yes-prod from the agent's human partner (CP-03). The broker's routine minting, rotation and revocation rides on the role-patch yes-prod. Deploying onto the OaW dev space needs none (CP-06), but wiping it needs BJ.

### 5.N Open Questions

Spike Story 1.2 resolves each of these, and records each answer as a `[ledger]` entry on Plan #3:

1. **Enterprise Entry's own limits:** e.g. a cap on message history that would affect the read path (D-007).
2. **One client per token in one process:** does the upstream library support it? If not, §5.2 is revisited (D-054).
3. **The Discord `·p1` suffix:** is it safe for today's parsers (the watcher's echo filter and signature regex)? (D-059)
4. **The kiosk mechanism:** `claude -p` with tools and MCP disabled, or a direct model API call with no tools. If the API, which credentials? (D-071)
5. **An unlicensed Enterprise image as a stand-in for Entry in CI:** is it faithful? (§6.1)
6. **Carried from the sketchbook:** are bot-to-bot DMs supported? Do bot accounts appear in `@` autocomplete, with their own avatars? What about private DMs vs. visibility? How does `@<dev-team>` behave across crews? Do crew names need to be unique for `@crew` routing?
7. **Broker transport when the broker is on another host:** for the dev phase the daemon and broker share one host, using the unix socket.

---

## 6. Test Plan

### 6.1 Test Strategy

- **Unit tests** are pytest, per package, specified per story in §8.
- **Integration and E2E tests** run in CI against real Mattermost containers: Team Edition 11.11.3, and an unlicensed Enterprise image standing in for Entry (CT-01; to verify, §5.N-5). The rate limits match the dev space.
- The CI image has **two users**, so the daemon runs apart from the test "agent" user. That makes R-08 actually testable (IT-01).
- Discord is exercised through a scream-hole fake. squawkie is exercised through a local SSE fake.
- The final phase ends with a smoke run on the OaW dev space, run by an agent and posted to Plan #3.

### 6.2 Integration Tests (Automated)

| ID | Boundary | Description | Req IDs |
|----|----------|-------------|---------|
| IT-01 | daemon ↔ broker ↔ Mattermost | The broker creates the bot account and mints a token; the daemon stores it; the agent's user gets EACCES reading the token store | R-01, R-03, R-05, R-06, R-08 |
| IT-02 | broker ↔ Mattermost | Daily re-mint replaces the token; native expiry is set where supported, and revocation applies otherwise | R-05, R-07 |
| IT-03 | broker ↔ Mattermost | With an injected clock, a bot account idle for 30 days is deactivated | R-30 |
| IT-04 | thin MCP ↔ daemon | Default resolution order, and the loud failure plus squawk in headless mode | R-11 |
| IT-05 | daemon ↔ adapters | A reply follows its thread and backend; an explicit backend is used and logged | R-12, R-13 |
| IT-06 | adapters | The protocol marker is written on both backends; a proto-0 message (no marker) still parses | R-14 |
| IT-07 | daemon addressing | `@agents` and `@all` (Discord, during the transition), `@name.crew` kept, "reply to me" semantics | R-16, R-17, R-18, R-19 |
| IT-08 | daemon filters | The three switch modes; push and pull both gated; an unresolvable crew is dropped | R-20, R-21, R-22 |
| IT-09 | daemon ↔ broker | Social lapses on delivery after expiry and reverts, the partner is notified, a dead broker can't hold social open, and only the partner's user ID can flip | R-23, R-24 |
| IT-10 | daemon filters | Loop guard: opt-in, mention required, sliding-window drop | R-25 |
| IT-11 | doorbell | Events from all three adapters merge, one exit per event | R-26 |
| IT-12 | doorbell | One backend down: a `backend.down` event, and the others keep flowing | R-27 |
| IT-13 | doorbell | Reconnect replays from the cursor exactly once | R-28 |
| IT-14 | Mattermost adapter | Server mention markers are used; echo is filtered by user ID | R-04, R-29 |
| IT-15 | Discord adapter | Today's `disc_*` behaviours, through `chat_*` and through the deprecated aliases | R-15 |
| IT-16 | daemon | One contract across N configured Mattermost servers plus Discord | R-09, R-10 |
| IT-17 | broker | Account naming fits CT-05; the crew defaults to the partner's username and can be overridden | R-01, R-02 |
| IT-18 | broker ↔ Mattermost | A test user standing in for the human partner DMs `social 2h`, `crew` and `kiosk off`; the state, expiry and lapse notice change | R-23, R-24, R-34 |
| IT-19 | Mattermost API | Each bot account's avatar is set (`GET /users/{id}/image`) and the bot is listed by `GET /users/autocomplete` | R-01 |
| IT-20 | runbook | The install and verify commands, **extracted from `docs/runbook.md`**, run in a fresh container | R-06, R-08 |
| IT-21 | daemon ↔ kiosk | Under crew only, an outsider's message reaches the kiosk; the kiosk starts lazily, has no tools, and sees only the `## Kiosk` section; an empty section gives the fixed reply | R-31, R-32 |
| IT-22 | daemon ↔ working agent | Engaging social delivers the kiosk transcript since the cursor, marked as outsider content; the cursor advances | R-33 |
| IT-23 | daemon filters | With the kiosk off, an outsider's message is dropped | R-34 |

### 6.3 End-to-End Tests (Automated)

| ID | Flow | Description | Req IDs |
|----|------|-------------|---------|
| E2E-01 | Cross-crew collaboration | The OaW crew and a synthetic second crew, each with its own human, collaborate in a shared channel under trusted crews | R-01, R-09, R-12, R-16, R-20, R-26 |
| E2E-02 | Outsider and switch | An unlisted human writes to a crew-only agent and is answered by the kiosk. The partner engages social; the kiosk review is delivered, the next outsider message reaches the working agent marked as outsider content, and social lapses on time | R-21, R-23, R-31, R-33 |
| E2E-03 | Fleet broadcast | `@agents` to about 30 registered agents stays within the rate limit | R-16, R-25 |

### 6.4 Manual Verification Procedures

**None.** Anything an agent can drive through the API is automated and runs in CI (D-063). The human's switch DM is IT-18, bot avatars and autocomplete are IT-19, and the runbook's install is IT-20.

---

## 7. Definition of Done

- [ ] All Phase DoD checklists are satisfied.
- [ ] Every requirement R-01 to R-34 traces to at least one IT or E2E item in the VRTM, and that item passes. [R-01 to R-34]
- [ ] All IT-01 to IT-23 and E2E-01 to E2E-03 pass in CI, against both the Team Edition and the Enterprise images. [CT-01]
- [ ] IT-01 runs the daemon under a separate user in CI. [R-08]
- [ ] Every Deliverables Manifest row (5.A) is delivered at its path.
- [ ] The §5.N questions each have a recorded answer, and any answer that changes a decision has a superseding `[ledger]` entry.
- [ ] `git grep -niIf ~/.claude/partner-names.pattern` finds nothing in this repo. The pattern file is local and never committed.
- [ ] The final phase ends with a smoke run on the OaW dev space (two crews, the switch, the kiosk, `@agents`), run by an agent and posted to Plan #3. Deploying onto any production server is outside this DoD and needs its own yes-prod (CP-03).

### 7.2 Dev Spec Finalization Checklist

- [ ] Every Tier 1 row in the Deliverables Manifest (5.A) has a file path or "N/A — because [reason]"
- [ ] Every Tier 2 trigger that fires has a corresponding row in the Deliverables Manifest
- [ ] Every Deliverables Manifest row has a "Produced In" wave assignment
- [ ] Every MV-XX in Section 6.4 has a procedure document in the Deliverables Manifest
- [ ] No deliverable is referenced only as a verb without a corresponding noun (file path)
- [ ] At least one audience-facing doc (DM-09) has a file path assigned
- [ ] Section 7 Definition of Done references the Deliverables Manifest (not separate Artifact Manifest + Documentation Kit)

---

## 8. Phased Implementation Plan

### How to read this section

Phases are sequential milestones, each with its own Definition of Done. Each story becomes one issue, and is implemented as one PR in `Wave-Engineering/mattermost-crew`. Stories in the same wave have no dependencies on each other and can run in parallel. Each AC item is tagged with the requirements it verifies.

A dedicated dev-agent in `mattermost-crew` runs `/wavemachine` for this Plan (D-065). babelfish owns the kit integration points, IP-2 and IP-3, which sit outside the Plan's stories, and is that agent's escalation point. Each phase lands on `main` on its own. The agent's first check is whether `/wavemachine` can scope a campaign to one phase; if it can't, each phase becomes its own Plan.

### Wave Map

```
PHASE 1 — Foundation, broker, identity
P1W1 ─┬─ 1.1 Workspace scaffold (foundation)
      └─ 1.2 Verification spike (§5.N, kiosk mechanism)
           │
P1W2 ─── 1.3 Broker: accounts, tokens, expiry, re-mint, prune
           │
P1W3 ─── 1.4 Daemon core: socket, protocol, register, token store
           │
PHASE 2 — Chat contract and adapters
P2W1 ─┬─ 2.1 Mattermost adapter
      ├─ 2.2 Discord adapter
      └─ 2.3 Addressing
           │
P2W2 ─── 2.4 Thin MCP: chat_*, default resolution, disc_* aliases
           │
   ⇒ IP-2: tag release → K-1 (babelfish, claudecode-workflow): skills → chat_*, /name registers,
           install thin MCP, chat-defaults ask-once, chat_* tool reference (DM-09b)
           │
PHASE 3 — Inbound defense, doorbell, kiosk
P3W1 ─┬─ 3.1 Broker: switch and kiosk control
      └─ 3.2 Doorbell core
           │
P3W2 ─┬─ 3.3 Daemon filters
      └─ 3.4 Thin doorbell one-shot
           │
P3W3 ─── 3.5 Kiosk
           │
P3W4 ─┬─ 3.6 Social-engage kiosk review
      ├─ 3.7 Docs: runbook, partner guide, architecture
      └─ 3.8 E2E suite and remaining API tests
           │
P3W5 ─── 3.9 Closing: VRTM, manifest, partner-name grep, dev-space smoke
           │
   ⇒ IP-3: tag release → K-2 (babelfish, claudecode-workflow): doorbell wiring to the chat
           one-shot (old one-shots kept as fallback), /reseed writes the `## Kiosk` section,
           the kiosk-review step in the doorbell skill
```

| Wave | Stories | Master Issue | Parallel? |
|------|---------|-------------|-----------|
| P1W1 | 1.1, 1.2 | P1W1 master | Yes — 2 independent stories |
| P1W2 | 1.3 | Story 1.3 | Single story |
| P1W3 | 1.4 | Story 1.4 | Single story |
| P2W1 | 2.1, 2.2, 2.3 | P2W1 master | Yes — 3 independent stories |
| P2W2 | 2.4 | Story 2.4 | Single story |
| P3W1 | 3.1, 3.2 | P3W1 master | Yes — 2 independent stories |
| P3W2 | 3.3, 3.4 | P3W2 master | Yes — 2 independent stories |
| P3W3 | 3.5 | Story 3.5 | Single story |
| P3W4 | 3.6, 3.7, 3.8 | P3W4 master | Yes — 3 independent stories |
| P3W5 | 3.9 | Story 3.9 | Single story |

### Co-production Rule

Each story that delivers a deployable service (1.3 broker, 1.4 daemon) adds its own checks to `make verify-install` in the same PR. Story 3.7 then lifts those checks into the runbook, and IT-20 runs them from there.

---

### Phase 1: Foundation, Broker, Identity

**Goal:** A workspace with CI against real Mattermost, a broker that creates bot accounts and mints tokens, and a daemon that holds those tokens where agents cannot read them.

#### Phase 1 Definition of Done

- [ ] `make ci` passes locally and in CI against both Mattermost images. [CT-01]
- [ ] Every §5.N question has a recorded answer on Plan #3.
- [ ] IT-01, IT-02, IT-03 and IT-17 pass. [R-01, R-02, R-03, R-05, R-06, R-07, R-08, R-30]
- [ ] `make verify-install` checks the broker and the daemon on a fresh host. [R-06, R-08]
- [ ] All Phase 1 unit tests pass.

---

#### Story 1.1: Workspace scaffold with CI against Mattermost

**Wave:** P1W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** None

Creates the uv workspace, the build system and the CI that every later story relies on. This is the foundation story.

**Implementation Steps:**

1. Create the root `pyproject.toml` as a uv workspace with `members = ["packages/*"]`, requiring Python 3.12. Configure ruff and mypy.
2. Create three package skeletons, each with a `pyproject.toml`, `src/<pkg>/__init__.py` exposing `__version__ = "0.1.0"`, and a smoke test:
   - `packages/daemon` (`crewchat`)
   - `packages/broker` (`crewbroker`)
   - `packages/clients` (`crewchat_clients`)
3. Add the dependencies:
   - daemon: the upstream Mattermost library at an exact reviewed version, `websockets`, `httpx`;
   - clients: `mcp`;
   - dev: `pytest`, `pytest-cov`, `ruff`, `mypy`.
   Run `uv lock` so that `uv.lock` carries hashes.
4. Create the `Makefile` with these targets:
   - `lint`: `uv run ruff check . && uv run mypy packages`
   - `test`: `uv run pytest tests -q --junitxml=reports/junit.xml --cov=packages --cov-report=xml:reports/coverage.xml`
   - `ci`: `lint test`
   - `verify-install`: runs `scripts/verify-install.sh`, which starts empty and gains checks in 1.3 and 1.4.
5. Create `tests/integration/conftest.py` with a `mattermost` fixture. It reads `MM_URL` and `MM_ADMIN_TOKEN` from the environment, and skips with a clear reason when they are absent.
6. Create `scripts/ci/mattermost-up.sh`. It starts the image named by `MM_IMAGE` (`mattermost/mattermost-team-edition:11.11.3` or `mattermost/mattermost-enterprise-edition:11.11.3`), waits for `/api/v4/system/ping`, creates an admin, sets `EnableBotAccountCreation` and `EnableUserAccessTokens` and the dev-space rate limits, and exports `MM_URL` and `MM_ADMIN_TOKEN`.
7. Create `scripts/ci/two-user-setup.sh`. It creates the `crewchat`, `crewbroker` and `agent` users and the `crewchat-clients` group in the CI image, so tests can run as `agent` while services run as their own users.
8. Create `.github/workflows/ci.yml`, with each step one line calling `scripts/ci/*.sh`: a matrix over the two `MM_IMAGE` values, running `make ci`, then uploading `reports/`.
9. Write `README.md` (overview, layout, `make` targets) and `CHANGELOG.md` with an "Unreleased" section.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_packages_import` | All three packages import and expose `__version__` | `tests/unit/test_scaffold.py` |
| `test_mattermost_fixture_skips_without_env` | The integration fixture skips cleanly when `MM_URL` is unset | `tests/unit/test_scaffold.py` |

*Integration/E2E Coverage:*

- None runnable yet. This story provides the Mattermost fixture that every IT uses.

**Acceptance Criteria:**

- [ ] `uv sync --frozen` succeeds, and `uv.lock` contains `--hash` entries for every package. [CT-03]
- [ ] `make ci` passes locally with the Team Edition container. [CT-01]
- [ ] CI runs the matrix over both images, and both legs are green on the PR. [CT-01]
- [ ] `reports/junit.xml` and `reports/coverage.xml` are uploaded as CI artifacts.
- [ ] `.github/workflows/ci.yml` has no `run:` step longer than one line; the logic is in `scripts/ci/`.
- [ ] `README.md` and `CHANGELOG.md` exist.

---

#### Story 1.2: Verification spike — §5.N and the kiosk mechanism

**Wave:** P1W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** None

Answers the open questions that could overturn a locked decision, before any code depends on them.

**Implementation Steps:**

1. Against the OaW dev space, or a local Team Edition container, answer §5.N questions 1, 2, 3, 5 and 6 by experiment. For each one, record the command run and its output in `docs/spikes/2026-10-spike-1.2.md`.
2. **Question 2 (one client per token):** write `spikes/two_clients.py`. It imports the upstream library, creates two client objects with two different bot tokens in one process, and posts as each. Record which author IDs the server reports.
3. **Question 3 (`·p1`):** run the signature-parsing code from mcp-server-discord and the watcher, as of their current release tags, against a signature line ending in ` ·p1`, and record whether the name still parses.
4. **Question 4 (the kiosk mechanism):** time and test both options with a fixture brief.
   - (a) `claude -p --tools "" --disallowedTools "mcp__*" --strict-mcp-config` with the brief in the prompt. Ask it to run a shell command, and record whether it ran.
   - (b) a direct model API call with no tool definitions.
   Record latency, cost per question and the credentials each needs.
5. Post one `[ledger D-NNN]` comment per answer on Plan #3. Where an answer changes a locked decision, the entry carries `**Supersedes:**`.
6. Update §5.N of this Dev Spec with the answer and the ledger ID for each question.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_spike_doc_answers_every_question` | Every §5.N item has an "Answer:" line in the spike doc | `tests/unit/test_spike_doc.py` |

*Integration/E2E Coverage:*

- None. This story produces evidence that the design rests on.

**Acceptance Criteria:**

- [ ] `docs/spikes/2026-10-spike-1.2.md` records a command, its output and an answer for every §5.N question. [CT-01]
- [ ] The kiosk mechanism is chosen, with measured latency and cost per question, and recorded as a ledger entry. [R-31]
- [ ] Every answer has a `[ledger]` entry on Plan #3; those that change a decision supersede it.
- [ ] §5.N of the Dev Spec cites each answer's ledger ID.

---

#### Story 1.3: Broker — accounts, tokens, expiry, re-mint, prune

**Wave:** P1W2
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.1, 1.2

Delivers the per-crew broker that creates bot accounts and mints, rotates and prunes their tokens, with no admin rights.

**Implementation Steps:**

1. `packages/broker/src/crewbroker/config.py`: `load_config(path="/etc/crewbroker/crew.toml") -> CrewConfig`, a dataclass with `crew`, `server_url`, `human_user_ids: list[str]` and `trusted_crews: list[str]`. If `crew` is absent, it defaults to the username of the first human user ID, fetched from the server (R-02).
2. `crewbroker/mint.py`:
   - `account_name(dev_name, crew) -> str` returns `f"{dev_name}.{crew}"`. It raises `ValueError` past 64 characters, or on characters outside `a-z0-9._-` (CT-05).
   - `ensure_bot(client, name) -> bot_user_id` looks up a bot account by username and creates it if absent (R-03).
   - `mint_token(client, bot_user_id, ttl_hours=26) -> Token` creates a user access token. Where the server supports expiry, it sets the expiry, detected by a capability probe cached per server (R-07). It records the token's id and creation time in `/var/lib/crewbroker/tokens.json`.
3. `crewbroker/prune.py`:
   - `remint_all(client, now)` revokes and re-mints each live token older than 24 hours, paced at 5/s or less (R-05, CT-06).
   - `prune_idle(client, now, idle_days=30)` deactivates bot accounts whose last mint is more than 30 days old (R-30).
4. `crewbroker/service.py`: a unix-socket server on `/run/crewbroker/crewbroker.sock` (mode `0660`, group `crewchat`), using the §5.4 frame shape. The op `mint{dev_name}` returns `{account, token}`. The token goes only to the daemon's user, checked with `SO_PEERCRED` against the `crewchat` UID. A daily scheduler thread runs `remint_all` and `prune_idle`.
5. `packaging/crewbroker.service`: a systemd unit with `User=crewbroker`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/crewbroker /run/crewbroker`.
6. `scripts/verify-install.sh`: add checks that the broker socket exists with the right mode and group, and that a `mint` for `verify-canary` succeeds.
7. `scripts/ci/broker-role-patch.sh` (CI only): applies the `create_bot`, `read_bots` and `manage_bots` patch to `system_user_access_token`, and assigns that role to the broker account in the test server. On real servers this step is covered by CP-03 and is never automated against production.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_account_name_format_and_limits` | `<dev>.<crew>`; rejects over 64 characters and bad characters | `tests/unit/broker/test_mint.py` |
| `test_ensure_bot_creates_once` | A second call returns the same ID with no second create | `tests/unit/broker/test_mint.py` |
| `test_mint_sets_expiry_when_supported` | The capability probe drives whether expiry is set | `tests/unit/broker/test_mint.py` |
| `test_remint_paces_under_limit` | A burst of 40 tokens is paced at 5/s or less | `tests/unit/broker/test_prune.py` |
| `test_prune_idle_30_days` | With an injected clock, 31 days idle → deactivated; 29 → kept | `tests/unit/broker/test_prune.py` |
| `test_mint_refused_to_non_daemon_peer` | `SO_PEERCRED` ≠ the crewchat UID → error, and no token returned | `tests/unit/broker/test_service.py` |
| `test_crew_defaults_to_partner_username` | `crew` is absent from the config → the partner's username | `tests/unit/broker/test_config.py` |

*Integration/E2E Coverage:*

- IT-02 and IT-03 are now runnable.
- IT-17 is partially runnable; the daemon-side naming arrives in 1.4.
- IT-01 is partially runnable; the daemon arrives in 1.4.

**Acceptance Criteria:**

- [ ] `ensure_bot` creates `<dev>.<crew>` on the first call only, against the CI Mattermost. [R-01, R-03]
- [ ] `mint_token` returns a working token, and sets native expiry when the server supports it. [R-05, R-07]
- [ ] The broker account holds the patched role and no system-admin role; a test asserts this through the API. [R-06]
- [ ] IT-02 and IT-03 pass on both images. [R-05, R-07, R-30]
- [ ] `mint` from a peer that isn't `crewchat` is refused. [R-08]
- [ ] `make verify-install` includes the broker checks, and they pass in CI. [R-06]

---

#### Story 1.4: Daemon core — socket, protocol, register, token store

**Wave:** P1W3
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.3

Delivers the chat daemon's process, socket protocol and registration, holding tokens as the `crewchat` user.

**Implementation Steps:**

1. `packages/daemon/src/crewchat/protocol.py`:
   - `PROTO = 1`;
   - `parse_frame(line: bytes) -> Request` validates `proto`, `id`, `op` and `args`. It accepts `proto <= PROTO` and rejects a higher `proto` with `unsupported` (CP-05);
   - `ok(id, result)` and `err(id, msg)`.
2. `crewchat/server.py`: an asyncio unix server on `/run/crewchat/crewchat.sock` (mode `0660`, group `crewchat-clients`) that dispatches ops to handlers. Unknown ops return `unsupported`.
3. `crewchat/registry.py`: `register(dev_name, crew, repo_root, session_id) -> Registration`. It keeps a registration per Dev-Name and accepts the declared name (D-055).
4. `crewchat/tokens.py`:
   - `BrokerClient.mint(dev_name)` calls the broker socket;
   - `TokenStore` keeps tokens in memory, plus `/var/lib/crewchat/tokens/` (mode `0700`, owner `crewchat`), refreshing on mint failure or a 401.
5. `crewchat/__main__.py`: loads `/etc/crewchat/servers.toml` (an array of `mattermost` tables with `id` and `url`, plus a `discord` table with `enabled`) and starts the server.
6. `packaging/crewchat.service`: a systemd unit with `User=crewchat`, `ReadWritePaths=/var/lib/crewchat /run/crewchat`.
7. `scripts/verify-install.sh`: add checks that the daemon socket exists with mode `0660` and group `crewchat-clients`, that `health` answers, and that `sudo -u agent cat /var/lib/crewchat/tokens/*` fails with EACCES.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_parse_frame_accepts_older_proto` | `proto` 0 or 1 is accepted; 2 → `unsupported` | `tests/unit/daemon/test_protocol.py` |
| `test_unknown_op_unsupported` | An unknown op returns `ok:false` | `tests/unit/daemon/test_server.py` |
| `test_register_returns_account_name` | `register` → `<dev>.<crew>` | `tests/unit/daemon/test_registry.py` |
| `test_token_store_mode_0700` | The store directory and files are not readable by others | `tests/unit/daemon/test_tokens.py` |
| `test_token_refresh_on_401` | A 401 triggers one re-mint, then a retry | `tests/unit/daemon/test_tokens.py` |

*Integration/E2E Coverage:*

- IT-01 and IT-17 are now runnable.

**Acceptance Criteria:**

- [ ] IT-01 passes on both images: the daemon runs as `crewchat`, a test running as `agent` gets EACCES reading the token store, and the minted token works. [R-01, R-03, R-05, R-06, R-08]
- [ ] IT-17 passes. [R-01, R-02]
- [ ] A frame with `proto: 2` gets `unsupported`; a frame with `proto: 0` is handled. [R-14]
- [ ] `make verify-install` includes the daemon checks, and they pass in CI. [R-08]

---

### Phase 2: Chat Contract and Adapters

**Goal:** One chat contract that sends, reads and replies across N Mattermost servers and Discord, with the agreed addressing rules, exposed to agents as `chat_*` tools.

#### Phase 2 Definition of Done

- [ ] IT-04 to IT-07, and IT-14 to IT-16, pass on both images. [R-04, R-09 to R-19, R-29]
- [ ] Every `disc_*` alias is covered by IT-15. [R-15]
- [ ] A `mattermost-crew` release is tagged, ready for IP-2 (K-1).
- [ ] All Phase 2 unit tests pass.

---

#### Story 2.1: Mattermost adapter

**Wave:** P2W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.4

Implements send, read, thread, DM and react against Mattermost through the upstream library, plus the protocol marker and echo filtering by user ID.

**Implementation Steps:**

1. `crewchat/adapters/base.py`: a `ChatBackend` protocol with `send`, `read`, `thread`, `dm`, `react` and `stream() -> AsyncIterator[InboundMessage]`, and an `InboundMessage` dataclass with `backend`, `channel`, `post_id`, `root_id`, `author_id`, `author_name`, `text`, `mentions: list[str]`, `proto` and `ts`.
2. `crewchat/adapters/mattermost.py`: `MattermostBackend(server_id, url, token_for)`, where `token_for(dev_name) -> str` returns that agent's token.
   - It creates one upstream client per Dev-Name.
   - `send` sets `props={"crew_proto": PROTO}` (R-14).
   - `read` and `thread` map posts to `InboundMessage`, taking `proto` from `props.crew_proto`, defaulting to 0.
3. `stream()` opens `/api/v4/websocket` per agent, pings every 30 s and reconnects with exponential backoff, capped at 60 s (CT-07). It emits a `posted` event as an `InboundMessage`, with `mentions` taken from the event's `mentions` field (R-29).
4. Echo filter: drop any `InboundMessage` whose `author_id` equals the receiving agent's own user ID (R-04).

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_send_sets_crew_proto_prop` | Posts carry `props.crew_proto` | `tests/unit/daemon/test_mattermost.py` |
| `test_inbound_proto_defaults_zero` | No prop → `proto` 0 | `tests/unit/daemon/test_mattermost.py` |
| `test_echo_filtered_by_user_id` | The agent's own post is dropped, even when it carries another agent's signature text | `tests/unit/daemon/test_mattermost.py` |
| `test_mentions_from_server_field` | `mentions` comes from the event, not from parsing the text | `tests/unit/daemon/test_mattermost.py` |
| `test_ping_interval_and_backoff` | 30 s ping; backoff capped at 60 s | `tests/unit/daemon/test_mattermost_ws.py` |

*Integration/E2E Coverage:*

- IT-14 is now runnable.
- IT-06 (the Mattermost half) and IT-16 are partially runnable.

**Acceptance Criteria:**

- [ ] IT-14 passes on both images. [R-04, R-29]
- [ ] Posts made through the adapter carry `props.crew_proto = 1`, and older posts parse with `proto` 0. [R-14]
- [ ] Two agents post from one daemon process as two distinct authors (two client objects). [R-01]
- [ ] Websocket reconnect after a forced close is covered by a unit test. [CT-07]

---

#### Story 2.2: Discord adapter

**Wave:** P2W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.4

Gives the daemon Discord parity: reads go through scream-hole, sends go through the Discord REST API, and the protocol marker is added to the signature.

**Implementation Steps:**

1. `crewchat/adapters/discord.py`: `DiscordBackend(screamhole_url, credential_path)`.
   - `read`, `thread` and `stream` use scream-hole's read API, as mcp-server-discord-watcher's poll core uses it today.
   - `send`, `dm` and `react` use the Discord REST API with the credential at `credential_path`, owned by `crewchat` and mode `0600`.
2. `send` appends the signature `— **<dev-name>** <avatar> (<team>) ·p1` (R-14, D-059). It uses the `·p1` form decided in spike 1.2.
3. Inbound parsing takes the author from the signature regex today's watcher uses. The `·p1` suffix is optional, and a missing suffix gives `proto` 0.
4. Echo filter: drop inbound messages whose signature name is the receiving agent's Dev-Name. Discord has no per-agent user ID (R-15, today's behaviour).
5. Add `tests/fakes/screamhole.py`, an HTTP fake of scream-hole's read API, for the unit and integration tests.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_signature_with_p1_suffix` | Send emits the suffix, and parse accepts it with or without | `tests/unit/daemon/test_discord.py` |
| `test_read_via_screamhole_fake` | Reads go through the fake, never Discord directly | `tests/unit/daemon/test_discord.py` |
| `test_discord_echo_by_signature` | The agent's own signed message is dropped | `tests/unit/daemon/test_discord.py` |

*Integration/E2E Coverage:*

- IT-15 is partially runnable; the aliases arrive in 2.4.
- IT-06 (the Discord half) is now runnable.

**Acceptance Criteria:**

- [ ] Every `disc_*` operation that exists today (send, read, thread, DM, react) has a working `DiscordBackend` equivalent, each covered by a unit test. [R-15]
- [ ] Sends carry `·p1`, and messages without it parse as `proto` 0. [R-14]
- [ ] No code path calls Discord's read endpoints directly; reads go through scream-hole. [D-013]

---

#### Story 2.3: Addressing

**Wave:** P2W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.4

Implements the addressing rules every adapter inherits: `@agents`, `@all` during the transition, `@name.crew`, crew and team tokens, and "reply to me".

**Implementation Steps:**

1. `crewchat/addressing.py`:
   - `tokens(text) -> set[str]` extracts `@`-tokens and keeps `.` inside them (R-18);
   - `is_broadcast(tokens, backend) -> bool` is true for `@agents` on every backend, and for `@all` only on Discord (R-16).
2. `addressed_to(msg, reg: Registration, my_threads: set[str]) -> bool` is true when any of these holds:
   - a broadcast;
   - `@<dev-name>` or `@<dev-name>.<crew>`;
   - `@<crew>` or `@<dev-team>`, matched by our filter and never by Mattermost user groups (R-17);
   - `msg.root_id` is in `my_threads`, the threads the agent started or posted in (R-19).
3. `crewchat/threads.py`: a `MyThreads` store that records the root ID of every post the agent makes, and that is consulted by `addressed_to`.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_agents_broadcast_both_backends` | `@agents` is a broadcast on Mattermost and Discord | `tests/unit/daemon/test_addressing.py` |
| `test_all_only_on_discord` | `@all` counts only on Discord | `tests/unit/daemon/test_addressing.py` |
| `test_dot_kept_in_name_crew` | `@kessel.oaw` stays whole | `tests/unit/daemon/test_addressing.py` |
| `test_crew_and_team_tokens` | Matched by our filter | `tests/unit/daemon/test_addressing.py` |
| `test_reply_to_me_by_thread_root` | A post in my thread is addressed to me | `tests/unit/daemon/test_addressing.py` |

*Integration/E2E Coverage:*

- IT-07 is now runnable.

**Acceptance Criteria:**

- [ ] IT-07 passes on both images. [R-16, R-17, R-18, R-19]
- [ ] No code path calls the Mattermost user-groups API (checked by grep in CI). [R-17, CT-01]

---

#### Story 2.4: Thin MCP — chat_*, default resolution, disc_* aliases

**Wave:** P2W2
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 2.1, 2.2, 2.3

Exposes the chat contract to agents as `chat_*` tools through a thin MCP shim with no credentials, resolves the default backend, and keeps the `disc_*` names as deprecated aliases.

**Implementation Steps:**

1. `packages/clients/src/crewchat_clients/mcp_shim.py`: a stdio MCP server built with the `mcp` SDK. It holds no credentials.
   - It defines the §5.5 tools (`chat_send`, `chat_read`, `chat_thread`, `chat_dm`, `chat_react`, `chat_health`), each forwarding one frame to `/run/crewchat/crewchat.sock`.
   - On start it sends `register` with the Dev-Name from `<repo_root>/.claude/agent-identity.json`.
2. `crewchat/defaults.py`: `resolve_default(reg, repo_root) -> Backend | NeedsHuman`, following R-11's order:
   1. the identity file's `home_backend`;
   2. `~/.claude/chat-defaults.json`, keyed by `<forge-host>/<org>` from `git remote get-url origin`, or by `repo_root` when there is no remote;
   3. otherwise, `NeedsHuman`.
3. In the shim, `NeedsHuman`:
   - **interactive:** the tool returns a structured question for the agent to ask its human. A follow-up tool `chat_set_default(key, backend)` writes `chat-defaults.json` atomically.
   - **headless** (`CLAUDE_CODE_ENTRYPOINT` is not interactive, or `--print`): the send fails with a loud error, and the shim runs `sqtk add <needs-list> "<question>"`, where `<needs-list>` is the crew's human partner's needs list, named in `/etc/crewchat/servers.toml` (`bj:needs` for crews partnered with BJ) (R-11, CP-02).
4. A backend named in the call is used, and the daemon logs `off-default: <dev> <backend>` (R-13).
5. Aliases: `disc_send` maps to `chat_send(backend="discord")`, and likewise for each `disc_*` tool. Each alias logs one deprecation warning per session.
6. `chat_send` with `thread` replies on that thread's backend, whatever the default is (R-12).

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_resolution_order` | identity file > defaults file > NeedsHuman | `tests/unit/daemon/test_defaults.py` |
| `test_forge_key_from_remote` | The SSH and HTTPS remote forms give `<host>/<org>` | `tests/unit/daemon/test_defaults.py` |
| `test_headless_fails_loud_and_squawks` | Headless + no default → an error and a squawk | `tests/unit/clients/test_mcp_shim.py` |
| `test_set_default_atomic_write` | `chat_set_default` writes atomically | `tests/unit/clients/test_mcp_shim.py` |
| `test_reply_follows_thread_backend` | A reply goes to the thread's backend | `tests/unit/clients/test_mcp_shim.py` |
| `test_disc_alias_maps_and_warns` | The alias forwards with `backend=discord` and warns once | `tests/unit/clients/test_mcp_shim.py` |
| `test_shim_holds_no_credentials` | The shim's environment and files contain no token | `tests/unit/clients/test_mcp_shim.py` |

*Integration/E2E Coverage:*

- IT-04, IT-05 and IT-16 are now runnable.
- IT-15 is now fully runnable.

**Acceptance Criteria:**

- [ ] IT-04, IT-05, IT-15 and IT-16 pass on both images. [R-09, R-10, R-11, R-12, R-13, R-15]
- [ ] A headless send with no default fails, and leaves a squawk on the crew's configured needs list; there is no Discord fallback. [R-11]
- [ ] `grep -rn "token" packages/clients/` finds no credential handling. [R-08]
- [ ] Every `disc_*` alias forwards and warns. [R-15]

---

### Phase 3: Inbound Defense, Doorbell, Kiosk

**Goal:** One merged doorbell over three backends, gated by the crew's social switch and the loop guard, with outsiders answered by a tool-less kiosk, and the operator and partner docs that make it runnable.

#### Phase 3 Definition of Done

- [ ] IT-08 to IT-13 and IT-18 to IT-23 pass on both images. [R-20 to R-28, R-31 to R-34]
- [ ] E2E-01 to E2E-03 pass in CI. [R-01, R-09, R-12, R-16, R-20, R-21, R-23, R-25, R-26, R-31, R-33]
- [ ] DM-09a, DM-09c and DM-10 are delivered.
- [ ] The dev-space smoke run is posted to Plan #3.
- [ ] A `mattermost-crew` release is tagged, ready for IP-3 (K-2).
- [ ] All Phase 3 unit tests pass.

---

#### Story 3.1: Broker — social switch and kiosk control

**Wave:** P3W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.3

The broker holds each crew's social switch and kiosk flag, and changes them only on DMs from the crew's human partner.

**Implementation Steps:**

1. `crewbroker/switch.py`: a `SwitchState` dataclass with `mode` (`crew` | `trusted` | `social`), `expires_at: datetime | None`, `revert_to` and `kiosk_on: bool = True`, persisted to `/var/lib/crewbroker/switch.json`. The default is `mode="crew"`, `kiosk_on=True` (R-20, R-34).
2. `apply_command(state, sender_user_id, text, cfg, now) -> (SwitchState, reply)`:
   - It accepts `social <dur>`, `trusted`, `crew`, `kiosk off` and `kiosk on`.
   - It rejects any sender not in `cfg.human_user_ids`, and the state stays unchanged (R-24).
   - `social <dur>` stores `revert_to` as the current mode, plus the expiry.
3. `effective_mode(state, now) -> (mode, lapsed: bool)`: past the expiry it returns `revert_to` (or `crew` if that is unreadable) and `lapsed=True` (R-23).
4. The broker's websocket listener in `crewbroker/service.py`: on a DM to the broker account, call `apply_command` and post the reply. When a lapse is detected, DM the human partner "social lapsed → <mode>".
5. A socket op `switch_state{}` returns the stored state plus `effective_mode`.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_default_crew_only_kiosk_on` | Defaults | `tests/unit/broker/test_switch.py` |
| `test_only_partner_can_flip` | A non-partner's command → refused, state unchanged | `tests/unit/broker/test_switch.py` |
| `test_social_lapses_and_reverts` | After expiry → `revert_to`; `lapsed` is reported | `tests/unit/broker/test_switch.py` |
| `test_unreadable_revert_falls_back_crew` | A corrupt `revert_to` → `crew` | `tests/unit/broker/test_switch.py` |
| `test_kiosk_off_on` | The flag toggles | `tests/unit/broker/test_switch.py` |

*Integration/E2E Coverage:*

- IT-18 is now runnable.
- IT-09 is partially runnable; the daemon's side arrives in 3.3.

**Acceptance Criteria:**

- [ ] IT-18 passes: the partner test user's DMs change the mode, the expiry and the kiosk flag, and a lapse DMs the partner. [R-23, R-24, R-34]
- [ ] A DM from any other user leaves the state unchanged and is answered with a refusal. [R-24]
- [ ] The defaults are crew only, with the kiosk on. [R-20, R-34]

---

#### Story 3.2: Doorbell core

**Wave:** P3W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 2.1, 2.2

Merges the Mattermost, Discord and squawkie streams into one queue per agent, with saved cursors and loud backend-down events.

**Implementation Steps:**

1. `crewchat/adapters/squawkie.py`: `SquawkieBackend(url, token)`.
   - `stream()` subscribes to `GET /api/stream` and emits `squawk.updated` events for the IDs in `<repo_root>/.claude/sqtk-mine.json`, with the same predicate as `sqtk-doorbell.sh`.
   - On connect it runs a catch-up check, mirroring `sqtk-drain.sh --pending`.
2. `crewchat/doorbell.py`: `Doorbell` runs one task per (agent, backend) that consumes `stream()`. The filter hook (`filters.admit`) is a pass-through in this story; 3.3 fills it in. Events go into an `asyncio.Queue` per agent.
3. Cursors: `CursorStore` at `/var/lib/crewchat/cursors/<dev>/<backend>.json` saves the last delivered event ID. On reconnect, the stream replays events after the cursor and de-duplicates by ID (R-28).
4. Backend health: on a stream failure or an auth error, enqueue `{"type":"backend.down","backend":b,"reason":...}` once per outage, and keep the other tasks running. On recovery, enqueue `backend.up` (R-27).
5. The op `subscribe{dev_name}` awaits that agent's queue and returns one event.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_three_adapters_one_queue` | Events from all three arrive on one queue | `tests/unit/daemon/test_doorbell.py` |
| `test_subscribe_returns_one_event` | One event per `subscribe` | `tests/unit/daemon/test_doorbell.py` |
| `test_cursor_replay_exactly_once` | A disconnect after event N → replay from N+1, with no duplicates | `tests/unit/daemon/test_doorbell.py` |
| `test_backend_down_once_others_flow` | One `backend.down` per outage; the other backends still deliver | `tests/unit/daemon/test_doorbell.py` |
| `test_squawkie_filter_and_catchup` | Only my squawks; catch-up on connect | `tests/unit/daemon/test_squawkie.py` |

*Integration/E2E Coverage:*

- IT-11, IT-12 and IT-13 are now runnable.

**Acceptance Criteria:**

- [ ] IT-11, IT-12 and IT-13 pass on both images. [R-26, R-27, R-28]
- [ ] squawkie events reproduce `sqtk-doorbell.sh`'s predicate, which a shared fixture verifies. [R-26]
- [ ] Killing one adapter's connection enqueues exactly one `backend.down`. [R-27]

---

#### Story 3.3: Daemon filters — switch cache, fail-closed, loop guard

**Wave:** P3W2
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.1, 3.2

Applies the social switch and the loop guard on both the push path and the pull path, failing closed.

**Implementation Steps:**

1. `crewchat/filters.py`:
   - `SwitchCache` fetches `switch_state` from the broker every 30 s, and on demand. It stores the state with its expiry.
   - `admit(msg, reg, now) -> Verdict` returns one of `DELIVER`, `DELIVER_MARKED_OUTSIDER`, `KIOSK`, or `DROP(reason)`.
2. `admit` resolves the sender's crew from `author_id` (broker-known bot IDs plus configured human IDs plus trusted crews). If the crew is unresolvable, or no switch state is cached, it returns `DROP` (R-22).
   - A crewmate → `DELIVER`.
   - A trusted crew under `trusted` or `social` → `DELIVER`.
   - An outsider under `social` (per `effective_mode`, checked now) → `DELIVER_MARKED_OUTSIDER` (R-23).
   - An outsider under `crew` or `trusted`, with the kiosk on → `KIOSK`; with the kiosk off → `DROP` (R-31, R-34).
3. Loop guard, `LoopGuard.admit(msg, reg)`:
   - Require the conversation to be opted in, meaning the agent has posted in it or was invited.
   - Require an inline mention of the agent, unless it's a reply in the agent's own thread (R-19).
   - Drop once there are more than 20 bot messages in the conversation's sliding window, which is configurable (R-25).
4. Wire `admit` into `Doorbell` before the queue (push), and into the `read` and `thread` handlers before results are returned (pull) (R-21). In this story, `KIOSK` verdicts go into a holding queue; 3.5 consumes it.
5. Mark outsider content: `DELIVER_MARKED_OUTSIDER` adds `{"outsider": true}` to the event, and the shim prefixes its text with `[from outside the crew]`.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_unresolvable_crew_dropped` | Unknown author → DROP | `tests/unit/daemon/test_filters.py` |
| `test_no_cache_drops` | No switch state → DROP | `tests/unit/daemon/test_filters.py` |
| `test_modes_matrix` | Every mode × sender class → the expected verdict | `tests/unit/daemon/test_filters.py` |
| `test_expiry_checked_per_delivery` | A stale cached social past its expiry → reverted verdict | `tests/unit/daemon/test_filters.py` |
| `test_read_path_filtered` | `read` omits dropped posts and marks outsiders | `tests/unit/daemon/test_filters.py` |
| `test_loop_guard_window` | The 21st bot message in the window is dropped | `tests/unit/daemon/test_loopguard.py` |
| `test_loop_guard_requires_mention_and_optin` | No mention or no opt-in → dropped | `tests/unit/daemon/test_loopguard.py` |

*Integration/E2E Coverage:*

- IT-08, IT-09, IT-10 and IT-23 are now runnable.

**Acceptance Criteria:**

- [ ] IT-08, IT-09, IT-10 and IT-23 pass on both images. [R-20, R-21, R-22, R-23, R-24, R-25, R-34]
- [ ] With the broker stopped and the cached social past its expiry, deliveries use the reverted mode. [R-23]
- [ ] The same filter applies to `subscribe` and to `read`, and a test asserts both. [R-21]

---

#### Story 3.4: Thin doorbell one-shot

**Wave:** P3W2
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.2

Gives agents a one-shot doorbell client that exits once per event, so an idle agent wakes on the task notification.

**Implementation Steps:**

1. `packages/clients/src/crewchat_clients/doorbell.py`: a `main()` entry point (`crewchat-doorbell`).
   - It reads the Dev-Name from `.claude/agent-identity.json` and sends `subscribe{dev_name}`.
   - It prints the event as one JSON line, and exits with the §5.8 codes: 0 for an event, 2 when not registered, 3 on timeout (`CREWCHAT_DOORBELL_TIMEOUT`, default 0 = forever), 4 when the daemon is unreachable.
2. Expose the console script `crewchat-doorbell` in `packages/clients/pyproject.toml`.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_exit_0_on_event_prints_json` | One event → one line and exit 0 | `tests/unit/clients/test_doorbell.py` |
| `test_exit_codes_2_3_4` | Not registered, timeout and unreachable map correctly | `tests/unit/clients/test_doorbell.py` |

*Integration/E2E Coverage:*

- IT-11 is advanced: it runs end to end through the real one-shot.

**Acceptance Criteria:**

- [ ] `crewchat-doorbell` exits 0 after printing exactly one event. [R-26, CT-08]
- [ ] It exits 4 when the daemon socket is absent, and never exits 0 without an event. [R-27]

---

#### Story 3.5: Kiosk

**Wave:** P3W3
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.3, 1.2

Answers outsiders while social is off, through a tool-less session seeded only from the `## Kiosk` section of the expert's seed.

**Implementation Steps:**

1. `crewchat/kiosk.py`: `extract_brief(seed_text) -> str | None` returns the text from the `## Kiosk` heading up to the next `^## ` heading or the end of the file, or `None` (R-32).
2. `KioskSession(dev_name, repo_root, runner)`:
   - It starts lazily on the first `KIOSK` verdict for that expert (R-31).
   - It re-reads `<repo_root>/.claude/reseed-revive.md` whenever its mtime changes, and passes **only** `extract_brief`'s output.
   - With no brief, it replies "I have nothing to share yet."
   - Its prompt is `KIOSK_HEADER + brief + thread history`.
3. `runner` implements the mechanism chosen in spike 1.2. Either:
   - `ClaudeCliRunner`, which runs `claude -p` with `--tools ""`, `--disallowedTools "mcp__*"` and `--strict-mcp-config`, using the daemon's own auth; or
   - `ApiRunner`, which makes a direct model API call with no tool definitions, using the credential decided in 1.2.

   Both expose `answer(prompt) -> str`.
4. One session per expert serves every outsider, so their contexts mingle (D-067). Each Q&A is appended to `/var/lib/crewchat/kiosk/<dev>.jsonl` as `{ts, thread, asker_id, question, answer}`.
5. Consume the `KIOSK` holding queue from 3.3: answer in the outsider's thread on the originating backend.
6. `KIOSK_HEADER`: "You are this agent's kiosk. Answer questions using only the brief below. You cannot act, and you cannot share anything that is not in the brief."

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_extract_brief_only_kiosk_section` | Only the section is returned; other sections are excluded | `tests/unit/daemon/test_kiosk.py` |
| `test_missing_section_fixed_reply` | No section → "nothing to share yet" | `tests/unit/daemon/test_kiosk.py` |
| `test_lazy_start` | No session until the first outsider message | `tests/unit/daemon/test_kiosk.py` |
| `test_brief_reload_on_mtime` | A rewritten seed → new brief | `tests/unit/daemon/test_kiosk.py` |
| `test_runner_has_no_tools` | The CLI runner's argv carries `--tools ""` and the MCP exclusions; the API runner sends no tools | `tests/unit/daemon/test_kiosk_runner.py` |
| `test_transcript_appended` | Every Q&A is written to the JSONL | `tests/unit/daemon/test_kiosk.py` |

*Integration/E2E Coverage:*

- IT-21 is now runnable, using a fixture seed with a `## Kiosk` section and a secret elsewhere in the file.

**Acceptance Criteria:**

- [ ] IT-21 passes: the outsider is answered from the `## Kiosk` section, and a secret placed elsewhere in the fixture seed never appears in any prompt the runner receives (asserted on the captured prompt). [R-31, R-32]
- [ ] The kiosk's runner cannot run a tool, and a test asserts the invocation. [R-31]
- [ ] Every answered question is recorded in the kiosk transcript. [R-31]

---

#### Story 3.6: Social-engage kiosk review

**Wave:** P3W4
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.5, 3.4

When the human partner engages social, the working agent reviews what the kiosk handled since the last review, and raises anything notable to the partner.

**Implementation Steps:**

1. In `crewchat/filters.py`, detect a mode transition to `social` in `SwitchCache`, comparing successive `effective_mode` values.
2. On that transition, `kiosk.review_since_cursor(dev)` reads `/var/lib/crewchat/kiosk/<dev>.jsonl` from the offset in `<dev>.cursor`. It enqueues one `{"type":"kiosk.review","outsider":true,"entries":[...]}` event for the working agent, then advances the cursor (R-33).
3. The event's text, as rendered by the shim, opens with `[from outside the crew] Kiosk review: review these exchanges and raise anything notable to your human partner.`

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_review_on_social_transition_only` | Fires on crew/trusted→social, not on other changes | `tests/unit/daemon/test_kiosk_review.py` |
| `test_review_since_cursor_and_advance` | Only the new entries; the cursor advances | `tests/unit/daemon/test_kiosk_review.py` |
| `test_review_marked_outsider` | The event carries `outsider: true` | `tests/unit/daemon/test_kiosk_review.py` |

*Integration/E2E Coverage:*

- IT-22 is now runnable.
- E2E-02 is partially runnable; it completes in 3.8.

**Acceptance Criteria:**

- [ ] IT-22 passes on both images. [R-33]
- [ ] No kiosk content reaches the working agent while social is off: a test asserts its queue stays empty during kiosk traffic. [R-31, R-33]

---

#### Story 3.7: Docs — runbook, partner guide, architecture

**Wave:** P3W4
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.3, 3.4

Delivers the operator runbook (with its prerequisites and install verification folded in), the human partner guide and the architecture document. CI runs the runbook's commands, so it can't drift.

**Implementation Steps:**

1. `docs/runbook.md` has these sections:
   - "Prerequisites": the OS users and group, Python 3.12, uv, and the socket mount into containers;
   - "Install the daemon" and "Install the broker", as fenced `bash` blocks taken from §5.B;
   - "Configure": `servers.toml` and `crew.toml`;
   - "Verify the install": a fenced `bash` block that runs `make verify-install`;
   - "Rollback".
2. `tests/integration/test_runbook.py` (IT-20) extracts every fenced `bash` block under "Install …" and "Verify the install" from `docs/runbook.md` and runs them in order, in a fresh container in CI. Every block must exit 0.
3. `docs/partner-guide.md`: the broker DM commands (`social <dur>`, `trusted`, `crew`, `kiosk off`, `kiosk on`); what each mode delivers; expiry and lapse notices; the kiosk and its review on social engage; and how to write a `## Kiosk` section.
4. `docs/architecture.md`: the §4.1 diagram, the components, the data paths for push, pull and kiosk, and the trust boundaries (the per-host socket, the `crewchat` and `crewbroker` users).

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_runbook_has_required_sections` | All five sections are present | `tests/unit/test_docs.py` |
| `test_partner_guide_lists_every_command` | Every command `apply_command` accepts is documented | `tests/unit/test_docs.py` |

*Integration/E2E Coverage:*

- IT-20 is now runnable.

**Acceptance Criteria:**

- [ ] IT-20 passes: the runbook's own install and verify blocks run clean in a fresh container. [R-06, R-08]
- [ ] `docs/partner-guide.md` documents every broker command, and a test checks it against the parser. [R-24, R-34]
- [ ] `docs/architecture.md` exists, with the trust boundaries stated. [R-08]

---

#### Story 3.8: E2E suite and remaining API tests

**Wave:** P3W4
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.5

Runs the three end-to-end flows and the remaining API tests in CI.

**Implementation Steps:**

1. `tests/e2e/conftest.py`: two crews (`oaw` and `synth`), each with its own human test user, its own broker config and two registered agents; plus one unlisted outsider user.
2. `tests/e2e/test_cross_crew.py` (E2E-01): under `trusted`, an agent from each crew converses in a shared channel; replies follow the thread, and each doorbell exits once per addressed message.
3. `tests/e2e/test_outsider_switch.py` (E2E-02):
   - The outsider asks a crew-only agent a question → the kiosk answers.
   - The partner DMs `social 1m` → a `kiosk.review` event arrives.
   - The outsider asks again → the working agent receives it marked as outsider content.
   - After expiry (injected clock), the next delivery uses `crew`.
4. `tests/e2e/test_broadcast.py` (E2E-03): 30 registered agents receive `@agents`, and server 429 counts stay at 0 under the configured limits.
5. `tests/integration/test_bot_profile.py` (IT-19): set each bot account's avatar; assert `GET /users/{id}/image` returns it and `GET /users/autocomplete?name=<dev>` lists the bot account.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_e2e_fixture_builds_two_crews` | The fixture creates both crews and the outsider | `tests/e2e/test_fixture.py` |

*Integration/E2E Coverage:*

- E2E-01, E2E-02, E2E-03 and IT-19 are now runnable and passing.

**Acceptance Criteria:**

- [ ] E2E-01, E2E-02 and E2E-03 pass in CI on both images. [R-01, R-09, R-12, R-16, R-20, R-21, R-23, R-25, R-26, R-31, R-33]
- [ ] IT-19 passes. [R-01]

---

#### Story 3.9: Closing — VRTM, manifest, partner-name grep, dev-space smoke

**Wave:** P3W5
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.6, 3.7, 3.8

Closes the Plan. It completes the trace and confirms every deliverable, checks for partner names, and runs the smoke on the dev space. There are no manual procedures to execute (D-063).

**Implementation Steps:**

1. Fill in Appendix V of this Dev Spec. For every R-01 to R-34 row, record the verifying IT/E2E/AC item, its method, and Pass/Fail from the last CI run on `main`.
2. Walk every 5.A row and confirm its file exists at its path. For DM-09b, confirm K-1's PR in claudecode-workflow has merged.
3. Run `git grep -niIf ~/.claude/partner-names.pattern` in this repo and record the empty result.
4. Run the dev-space smoke with `scripts/smoke-devspace.sh`, against the OaW dev space with two crews: a cross-crew exchange, kiosk Q&A, a social flip with its review, and an `@agents` broadcast. Post the script's output to Plan #3 as a comment. No production server is touched (CP-03, CP-06).
5. Update `README.md` and `CHANGELOG.md` for the release that IP-3 tags.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_vrtm_covers_every_requirement` | Appendix V has a row with a status for R-01 to R-34 | `tests/unit/test_devspec.py` |

*Integration/E2E Coverage:*

- All IT and E2E items are re-run on the final `main`.

**Acceptance Criteria:**

- [ ] Appendix V has a verification item and a Pass status for every R-01 to R-34. [R-01 to R-34]
- [ ] Every 5.A row is delivered at its path.
- [ ] The partner-name grep is empty.
- [ ] The dev-space smoke output is posted to Plan #3. [R-01, R-16, R-23, R-31, R-33]

---

## 9. Appendices

### Appendix L: Ledger Index

The sketchbook (`docs/SKETCHBOOK.md`, D-01 to D-26) holds the shaping decisions. Plan #3's `[ledger D-NNN]` comments hold the Dev Spec decisions.

| Sketchbook | Subject | Plan #3 entries |
|-----------|---------|-----------------|
| D-01 | Cross-crew collaboration goal | §1.2; D-017 |
| D-02 | Control of data | D-002 |
| D-03 | Configured server instances | D-002, D-032 (R-10), D-016 |
| D-04 | Backend is a default | D-003, D-033 (R-11) |
| D-05 | Replies follow the thread | D-034 (R-12) |
| D-06 | Explicit backend parameter | D-035 (R-13) |
| D-07 | Forge-less home backend | D-033 (R-11 step 1) |
| D-08 | Abstract in the server | D-010 (CT-04), D-031 (R-09) |
| D-09 | Partnered, not bots | D-017 (CP-01) |
| D-10 | Partner direction is authority | D-004, D-019 (CP-03) |
| D-11 | Crew namespace | D-011 (CT-05), D-023, D-024 (R-01, R-02) |
| D-12 | Real accounts | D-023, D-026, D-051 (R-01, R-04, R-29) |
| D-13 | scream-hole stays for Discord | D-001, D-060 |
| D-14 | Upstream library | D-009 (CT-03) |
| D-15 | Discord parity | D-001, D-037 (R-15) |
| D-16 | Backend down is loud | D-049 (R-27) |
| D-17 | Wave order | §8 (superseded by D-064, D-065) |
| D-18 | Its own program | D-064 |
| D-19 | Per-crew broker | D-008, D-027 to D-029 (R-05 to R-07) |
| D-20 | Read and write through our server | D-008 (CT-02), D-030 (R-08), D-043 |
| D-21 | Loop guard | D-047 (R-25) |
| D-22 | One doorbell core | D-015 (CT-09), D-048 (R-26), D-060 |
| D-23 | Social switch | D-005, D-018 (CP-02), D-042 to D-046 |
| D-24 | Addressing | D-020 (CP-04), D-038 to D-041 |
| D-25 | Prod scope | D-019 (CP-03) |
| D-26 | Order of work, dev space | D-006, D-022, D-066 |
| — | Constraints | D-007 to D-016 |
| — | Design | D-053 (superseded by D-064), D-054 to D-062, D-071 |
| — | Test policy (no MV) | D-063 |
| — | Agent and integration points | D-065 |
| — | Kiosk | D-067, D-068 (superseded by D-072), D-069, D-070, D-072 |

### Appendix V: Verification Requirements Traceability Matrix (VRTM)

Story 3.9 fills in the Status column.

| Req ID | Requirement (short) | Source | Verification Item | Verification Method | Status |
|--------|-------------------|--------|------------------|-------------------|--------|
| R-01 | Per-agent account `<dev>.<crew>` | Stories 1.3, 1.4, 2.1 | IT-01, IT-17, IT-19, E2E-01 | integration / e2e test | Pending |
| R-02 | Crew defaults to the partner's username | Story 1.3 | IT-17 | integration test | Pending |
| R-03 | Broker creates a missing bot account | Story 1.3 | IT-01 | integration test | Pending |
| R-04 | Echo filtered by user ID | Story 2.1 | IT-14 | integration test | Pending |
| R-05 | Mint on demand, re-mint daily | Story 1.3 | IT-01, IT-02 | integration test | Pending |
| R-06 | Broker has no admin; sole holder of the patched role | Story 1.3 | IT-01, IT-20 | integration test | Pending |
| R-07 | Native expiry, else revocation | Story 1.3 | IT-02 | integration test | Pending |
| R-08 | Token not readable by the agent | Stories 1.4, 3.7 | IT-01, IT-20 | integration test | Pending |
| R-09 | One tool contract | Story 2.4 | IT-16, E2E-01 | integration / e2e test | Pending |
| R-10 | Configured server instances | Story 2.4 | IT-16 | integration test | Pending |
| R-11 | Default-backend resolution; headless fails loud | Story 2.4 | IT-04 | integration test | Pending |
| R-12 | Reply follows thread and backend | Story 2.4 | IT-05, E2E-01 | integration / e2e test | Pending |
| R-13 | Explicit backend used and logged | Story 2.4 | IT-05 | integration test | Pending |
| R-14 | Protocol marker; older versions parse | Stories 1.4, 2.1, 2.2 | IT-06 | integration test | Pending |
| R-15 | Discord parity | Stories 2.2, 2.4 | IT-15 | integration test | Pending |
| R-16 | `@agents` (and `@all` on Discord) | Story 2.3 | IT-07, E2E-01, E2E-03 | integration / e2e test | Pending |
| R-17 | Crew and team tokens by our filter | Story 2.3 | IT-07 | integration test | Pending |
| R-18 | `.` kept in `@name.crew` | Story 2.3 | IT-07 | integration test | Pending |
| R-19 | "Reply to me" by thread | Story 2.3 | IT-07 | integration test | Pending |
| R-20 | Three switch settings, crew only by default | Story 3.1 | IT-08, E2E-01 | integration / e2e test | Pending |
| R-21 | Switch gates push and pull | Story 3.3 | IT-08, E2E-02 | integration / e2e test | Pending |
| R-22 | Unresolvable crew dropped | Story 3.3 | IT-08 | integration test | Pending |
| R-23 | Social marked, lapses per delivery, reverts, notifies | Stories 3.1, 3.3 | IT-09, IT-18, E2E-02 | integration / e2e test | Pending |
| R-24 | Only the partner flips; broker holds state | Story 3.1 | IT-09, IT-18 | integration test | Pending |
| R-25 | Loop guard | Story 3.3 | IT-10, E2E-03 | integration / e2e test | Pending |
| R-26 | One merged doorbell | Stories 3.2, 3.4 | IT-11, E2E-01 | integration / e2e test | Pending |
| R-27 | Backend down is loud; others flow | Story 3.2 | IT-12 | integration test | Pending |
| R-28 | Cursor replay exactly once | Story 3.2 | IT-13 | integration test | Pending |
| R-29 | Server mention markers | Story 2.1 | IT-14 | integration test | Pending |
| R-30 | Prune bot accounts idle 30 days | Story 1.3 | IT-03 | integration test | Pending |
| R-31 | Outsiders answered by a tool-less kiosk while social is off | Story 3.5 | IT-21, E2E-02 | integration / e2e test | Pending |
| R-32 | Kiosk sees only the `## Kiosk` section | Story 3.5 | IT-21 | integration test | Pending |
| R-33 | Kiosk review on social engage | Story 3.6 | IT-22, E2E-02 | integration / e2e test | Pending |
| R-34 | Kiosk can be turned off; outsiders then dropped | Stories 3.1, 3.3 | IT-18, IT-23 | integration test | Pending |
