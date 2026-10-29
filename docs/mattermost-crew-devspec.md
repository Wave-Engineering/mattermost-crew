<!-- DEV-SPEC-APPROVAL
approved: true
approved_by: BJ
approved_at: 2026-10-24T01:27:12Z
finalization_score: 8/8
-->

# Mattermost Program — Development Specification

**Version:** 1.0
**Date:** 2026-10-23
**Status:** Approved (2026-10-24, D-088); amended pre-kickoff by D-089 to D-092
**Authors:** bakerb, babelfish (oaw)
**Plan:** Wave-Engineering/mattermost-crew#3
**Phase Plans:** #26 (Phase 1), #27 (Phase 2), #28 (Phase 3); Plan #3 is the program Plan and ledger of record (D-089).
**Ledger of record:** `docs/SKETCHBOOK.md` (sketchbook S-01 to S-26) and the `[ledger D-NNN]` comments on Plan #3 (D-001 to D-092, including the PR #5 precheck-review entries D-073 to D-087 and the pre-kickoff amendments D-089 to D-092)
**Citation rule:** Sketchbook decisions are cited S-NN (sketchbook D-NN); Plan #3's ledger entries D-NNN (D-092).

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
- **On Mattermost, every agent is a real account** (`<dev-name>.<crew>`). Its ephemeral token is minted by a broker per (crew, server), and the agent itself can never read it.
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
| CT-06 | Clients stay within a per-user rate limit of 10/s with a burst of 50. The daemon paces its daily re-mint (it owns rotation, D-076). | The dev space's RateLimitSettings use `VaryByUser: true`. D-012. |
| CT-07 | The Mattermost websocket client pings every 30 s and reconnects with backoff. | A design requirement committed to mother. Her 660 s idle test passed even without pings. D-013. |
| CT-08 | An agent is woken by a background task's **exit** (the one-shot doorbell pattern), and every adapter feeds that pattern. | There is no Monitor primitive on Bedrock. D-014. |
| CT-09 | The `discord-watcher` binary name is kept or aliased. | About 27 kit files invoke it. D-015. |
| CT-10 | Development uses the LAN-only dev space. The implementation carries **no network assumption**: server URLs are configuration, and it runs anywhere that can reach a configured server. | BJ: it must be able to run on other LANs. D-016. |
| CT-11 | The program supports Mattermost 11.8 and later; CI runs a third leg at the newest 11.8 patch. | The oldest server the program must join predates 11.9; the design relies there on bot `owner_id`, bot reassignment, the role patch and token create/revoke. Native expiry stays a runtime capability (R-07). D-091. |

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
| R-30 | Event-driven | When a bot account hasn't registered in 30 days, the broker shall deactivate it. [D-052; amended by D-076] |

### 3.2 Broker and Tokens

| ID | Type | Requirement |
|----|------|-------------|
| R-05 | Ubiquitous | The broker shall mint each agent's token on demand and re-mint it at least daily. [D-027] |
| R-06 | Ubiquitous | The broker shall hold no admin rights. The patched `system_user_access_token` role shall be held only by configured broker accounts. [D-028] |
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
| R-19 | Ubiquitous | "A reply to me" shall mean a post in a thread the agent started or posted in, or a direct message to the agent. [D-041; DM added by D-080] |

### 3.5 Inbound Defense and Kiosk

| ID | Type | Requirement |
|----|------|-------------|
| R-20 | Ubiquitous | Each crew's switch on each server shall have three settings: **crew only** (the default), **trusted crews**, and **social**. [D-042; per server by D-086] |
| R-21 | Ubiquitous | The switch shall gate both the doorbell (push) and the read path (pull). On pull, membership and the switch apply; addressing and the loop guard apply to push only. [D-043] |
| R-22 | Unwanted | If a sender's crew cannot be resolved, the message shall not be delivered. [D-044] |
| R-23 | State-driven | While a crew is in social, messages from outside the crew shall be marked as such. Social shall lapse at its expiry, which is checked on every delivery and never by a timer. When it lapses it shall revert to the setting it was flipped from (or to crew only if that can't be read), and the human partner shall be notified. [D-045] |
| R-24 | Ubiquitous | Only the crew's human partner shall be able to change each crew's switch on each server, and that server's broker shall hold its state. [D-046] |
| R-25 | Ubiquitous | An agent shall respond to a message from a bot account only in a conversation it has opted into, and only to an inline mention. It shall drop anything past about 20 messages from bot accounts per conversation in a sliding window. Human messages shall be admitted when addressed per R-16 to R-19, which includes any direct message to the agent. A conversation is opted into when the agent started it, posted in it, or was mentioned into it, and every DM is opted into. A mention is `@<dev>`, `@<dev>.<crew>`, `@agents`, a crew token or a team token. [D-047; amended by D-075] |
| R-31 | Event-driven | When social is off and a message arrives from outside the crew, the daemon shall route it to the expert agent's kiosk session, started on the first such message, and never to the working agent. The kiosk shall have no tools. One kiosk session per expert per server shall serve every outsider on that server, and its transcript shall be kept. [D-067, D-087] |
| R-32 | Ubiquitous | The kiosk shall be seeded only from the `## Kiosk` section of the expert's reseed seed (`.claude/reseed-revive.md`), which code extracts mechanically (the shim, running as the agent's user, extracts it and pushes only that section to the daemon); the kiosk shall never see the rest of the file. If the section is missing or empty, the kiosk shall say it has nothing to offer yet. The kiosk shall refresh on each reseed. [D-072, superseding D-068] |
| R-33 | Event-driven | When the human partner engages social, the daemon shall deliver the kiosk transcript since the last review to the working agent, marked as outsider content. The agent's first action shall be to review it and raise anything notable to its human partner. The review cursor shall advance once the review is handed over. [D-069] |
| R-34 | Optional | Where the crew's human partner turns the kiosk off, outsider messages shall be dropped. The kiosk shall be on by default. [D-070] |

### 3.6 Doorbell

| ID | Type | Requirement |
|----|------|-------------|
| R-26 | Ubiquitous | One doorbell core shall merge Discord, Mattermost and squawkie events into one stream, exiting once per event. [D-048] |
| R-27 | Unwanted | If a backend is unreachable or unauthenticated, the doorbell shall report that backend as down, loudly, and keep delivering from the others. [D-049] |
| R-28 | Event-driven | When a backend stream connects or reconnects, the doorbell shall deliver the events missed since its saved cursor: it queues each exactly once and hands it over at least once. [D-050] |
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
    into every container on it)          crewbroker OS user: broker (one per crew, server)
```

R-08 forces this shape. Any MCP server that Claude Code spawns runs as the agent's user, and so does the doorbell, so neither of them can hold the token. Both are therefore **thin clients** that talk over a local unix socket to a **chat daemon**. The daemon runs as its own user and holds the tokens, the websockets, the switch, the loop guard and the kiosk sessions.

Agents on one host, including containers on a common host, are inside one trust boundary. A thin client registers with the Dev-Name from its identity file, and the daemon takes it at its word (D-055).

### 4.2 Session Start

1. `/name` picks a Dev-Name. The thin MCP registers with the daemon: `register{dev_name, dev_team, dev_avatar, crew, session_id}`. `crew` comes from the identity file's `crew` field, or else the host's default crew in `servers.toml`. `dev_team` and `dev_avatar` come from the identity file too (D-103). The shim then pushes the agent's kiosk brief and squawk IDs (`kiosk_brief`, `squawk_ids`), because the daemon never reads agent-home or repo paths.
2. For each configured Mattermost server, the daemon asks that crew's broker for that server for a token for `<dev-name>.<crew>` (`mint{dev_name, crew, current_token_id, deadline}`). Brokers are keyed by (crew, server_id). Only `register` causes a `mint`. The broker creates the bot account if it is missing (R-03), records `last_register`, and mints a token (R-05).
3. The daemon opens that agent's Mattermost websocket, pinging every 30 s (CT-07), and attaches the Discord and squawkie adapters.

### 4.3 Send

1. A skill calls `chat_send` without naming a backend.
2. The **shim**, running as the agent's user, resolves the default: the identity file, then `~/.claude/chat-defaults.json`, then asking the human (R-11). It passes the resolved backend to the daemon.
3. The adapter posts the message with the protocol marker (R-14).
4. A reply goes to the thread it answers (R-12). A backend named in the call is used and logged (R-13).

### 4.4 Inbound (Crew or Trusted)

1. An event arrives from any backend.
2. **Membership:** the sender's crew is resolved by user ID. On Mattermost, a bot account belongs to a crew when its `owner_id` is that crew's broker **and** it appears in that broker's `minted_bot_ids`; a human belongs when listed by ID. Discord is crew-equivalent, and squawkie is exempt (§5.9). If it can't be resolved, the event is dropped (R-22).
3. **Addressing:** the daemon checks whether the event is addressed to this agent (R-16 to R-19, R-29).
4. **Loop guard** (R-25), then **the switch** (R-20, R-21). This is the push order, the same as §5.9.
5. The one-shot doorbell exits (R-26). The agent wakes, reads through the MCP (on pull only membership and the switch apply, R-21) and re-arms.

### 4.5 Inbound (Outsider)

1. The sender is resolved as outside the crew. Addressing (R-16 to R-19) and the loop guard (R-25) run **before** any kiosk verdict, so an unaddressed outsider post never reaches the kiosk. A post tagged `props.crew_kiosk=1` is never sent to a kiosk: it is dropped under crew only and trusted, and delivered marked as outsider content under social.
2. **If social is on:** the message is delivered to the working agent, marked as outsider content (R-23).
3. **If social is off and the kiosk is on:** the message goes to the expert's kiosk session (R-31). The session starts on first use, seeded from the `## Kiosk` section the shim pushed (R-32). The kiosk answers in the thread with its mentions stripped, and the post is tagged `props.crew_kiosk=1`. Its transcript is kept.
4. **If social is off and the kiosk is off:** the message is dropped (R-34).

### 4.6 Switch Flip and Kiosk Review

1. The human partner DMs the broker, e.g. `social 2h`, `trusted`, `crew`, `kiosk off` or `kiosk on`. The broker accepts these only from that crew's human user ID (R-24).
2. The broker stores the setting and its expiry. Expiry is checked on every delivery (R-23).
3. When social is engaged, the daemon delivers the kiosk transcript since the last review to the working agent, marked as outsider content. The agent reviews it and raises anything notable to its human partner (R-33).

### 4.7 Backend Down and Recovery

1. An adapter loses its stream.
2. The doorbell exits with a `backend.down` event and keeps the other backends flowing (R-27).
3. On reconnect, the adapter replays from its saved cursor. It queues each missed event exactly once and hands it over at least once (R-28).

### 4.8 Daily Upkeep

1. The **daemon** rotates each agent registered in the last 30 days, paced under the rate limit (R-05, CT-06). It requests a new token (`remint`), swaps it in, reconnects that agent's websocket, then asks the broker to revoke the old token (`revoke`). Revocation does not close a websocket, so rotating without reconnecting would leave the agent deaf. An agent with no `register` in 30 days has its websocket closed and is dropped from rotation.
2. The broker deactivates bot accounts with no `register` in 30 days (R-30).
3. The kiosk takes a new brief whenever the shim pushes one after the seed is rewritten (R-32).

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
│   │   ├── tokens.py                 # broker clients (per (crew, server)), token store, rotation
│   │   ├── addressing.py             # R-16..R-19
│   │   ├── filters.py                # switch cache, loop guard
│   │   ├── doorbell.py               # event queue, cursors, backend.down
│   │   ├── kiosk.py                  # kiosk sessions
│   │   └── adapters/  mattermost.py  discord.py  squawkie.py
│   ├── broker/   src/crewbroker/      # broker (runs as user crewbroker)
│   │   ├── service.py  mint.py  prune.py  switch.py  config.py
│   └── clients/  src/crewchat_clients/
│       ├── mcp_shim.py               # thin MCP server: chat_* tools
│       ├── defaults.py               # R-11 default-backend resolution (agent's user)
│       ├── brief.py                  # extract_brief: the seed's `## Kiosk` section
│       └── doorbell.py               # thin one-shot: exits on one event
├── tests/  unit/  integration/  e2e/
└── docs/   SKETCHBOOK.md  for-mother.md  architecture.md  runbook.md
            partner-guide.md  mattermost-crew-devspec.md
```

The broker remains a separate package, a separate service and a separate OS user; its trust boundary is a deploy boundary, not a repo boundary. claudecode-workflow changes are made outside the phase Plans, at phase-boundary integration points (IP-2 after Plan #27 lands, IP-3 after Plan #28), pinned to a tagged `mattermost-crew` release (D-064, D-065, D-089). mcp-server-discord and the watcher get maintenance only, and leave agent sessions once `chat_*` covers Discord.

### 5.2 Technology Choices

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Daemon, broker, clients | Python 3.12, managed by uv | The upstream Mattermost library is Python. With one process and one client object per token, the daemon imports the library rather than supervising about 30 child servers (D-054). |
| Mattermost REST | `cloud-ru-tech/mcp-server-mattermost`, pinned in `uv.lock` with hashes | D-009, CT-03 |
| Mattermost websocket | `websockets`, pinging every 30 s with backoff | CT-07 |
| Discord | all REST through scream-hole, which proxies every v10 path, with the existing Discord credential, `/etc/crewchat/discord.credential` (mode `0600`, owner `crewchat`); while scream-hole is unhealthy, reads are refused and writes alone go to `discord.com` | D-013, D-106: scream-hole stays for Discord |
| squawkie | SSE `GET /api/stream` | `sqtk-doorbell.sh` logic, moved into the daemon |
| MCP shim | the `mcp` Python SDK (stdio) | thin client; no credentials |
| Tests | pytest, pytest-cov, JUnit XML; Mattermost containers in CI | DM-04 to DM-06 |

### 5.3 Caller Identity

- Each host has **one** daemon socket, `/run/crewchat/crewchat.sock`. It is mode `0660`, owned by `crewchat:crewchat-clients`, and mounted into every container on that host.
- A thin client registers with the Dev-Name and `crew` from `.claude/agent-identity.json`, and the daemon accepts it. If the file has no `crew`, the host's `default_crew` in `/etc/crewchat/servers.toml` is used.
- The shim also sends `dev_team` and `dev_avatar` from the identity file, falling back to the legacy `/tmp/claude-agent-<md5(repo_root)>.json`, then to `""` (D-103). Both are self-declared, like the Dev-Name. The avatar is display only (the Discord signature); no addressing, membership or trust decision reads it. Only the shim reads these files; the daemon package never names them.
- The daemon never reads files in an agent's home or repo. Its `~` is `crewchat`'s home, and in containers the agent's paths aren't host paths. Anything it needs from the agent's side (the kiosk brief, squawk IDs, the resolved default backend) is pushed by the shim over the socket.
- Agents on one host are one trust boundary (D-055). The accepted cost is that a same-host agent could post as a crewmate, which falls under D-005.

### 5.4 Socket Protocol

JSON lines over the unix socket. Every frame carries `"proto": 1` (D-056). Requests are `{"proto":1,"id":"<uuid>","op":"<op>","args":{...}}`; responses are `{"proto":1,"id":"<uuid>","ok":true|false,"result"|"error":...}`.

| op | args | result |
|----|------|--------|
| `register` | `dev_name, dev_team, dev_avatar, crew, session_id, project_key` (`dev_team`, `dev_avatar` optional strings, default `""`; D-103. `project_key` optional, default `""`: the shim's `repo_key()`, never a remote URL; #117) | `{account, backends, needs_list}`. `backends` is in the one backend-name form below (`["mattermost:<server_id>", ..., "discord"]`), and omits a server whose `agents_allow`/`agents_deny` doesn't admit the agent (#117); a skipped server is never `unavailable`. `needs_list` is the crew's configured sqtk list for R-11's headless squawk. With Discord enabled, `dev_avatar` and `dev_team` go to the Discord signature. |
| `send` | `dev_name, backend, to, text, thread?, attachments?, embed?, off_default?` (`backend` already resolved by the shim) | `{backend, post_id, thread}`. The thread root is recorded in the agent's persisted `MyThreads` (D-104). `attachments` is `[upload_id]` from `upload`; `embed` is `"title:body"`, split on the first colon (a malformed one is `bad_request`). Neither is ever silently dropped (R-15). On Discord, a `to` that is a name (`agent-ops`, `#agent-ops`) is resolved against the guild's text channels. |
| `dm` | `dev_name, backend, to, text, attachments?, embed?, off_default?` | `{backend, post_id, channel}`. Recorded in `MyThreads` like `send`. |
| `read` | `dev_name, backend, channel, since?, limit?, post_id?` | `{backend, messages:[...]}` (filtered, R-21). With `post_id`, the one message (today's `disc_read(message_id)`). |
| `thread` | `dev_name, backend, post_id` | `{backend, messages:[...]}` |
| `react` | `dev_name, backend, post_id, emoji` | `{}` |
| `upload` | `dev_name, name, seq, data_b64, last` | `{seq, size}` per chunk; on `last`, `{upload_id, size}`. The shim reads an `attach_path` as the agent's user and streams it in chunks that each fit a frame (64 KiB). The daemon holds it at `/run/crewchat/uploads/<upload_id>` (`0600`, owned by `crewchat`), at most 8 MiB (`too_large` past it), deleted after use or after 10 minutes. Only the agent that uploaded it can attach it. |
| `thread_backend` | `dev_name, post_id` | `{backend}`, or `not_found` (R-12). In order: the ID's shape decides the kind (a 17–20-digit snowflake is `discord`, a 26-character lowercase id is Mattermost; anything else is `bad_request`); for a Mattermost id, the agent's persisted thread set picks the server; otherwise `GET /api/v4/posts/{id}` with the agent's token, on only the Mattermost servers the agent is registered on, and the one that answers 200. |
| `list_channels` | `dev_name, backend, type?, guild_id?` | `{backend, channels:[{id, name, type}]}`. Discord only (today's `disc_list`; `type` is `text`, `voice` or `category`); Mattermost answers `unsupported`. |
| `resolve` | `dev_name, backend, name, type?, guild_id?` | `{backend, id, name, type}`, or `not_found`. Discord only (today's `disc_resolve`; `type` is `channel`, `user` or `role`). |
| `create_channel` | `dev_name, backend, name, type?, guild_id?` | `{backend, id, name, type}`. Discord only (today's `disc_create_channel`). |
| `create_thread` | `dev_name, backend, channel, name, message?` | `{backend, id, name, channel, post_id?}`. Discord only (today's `disc_create_thread`); `message` is posted, signed, as the thread's first message and recorded in `MyThreads`. |
| `kiosk_brief` | `dev_name, text` | `{}`. The shim pushes the extracted `## Kiosk` section whenever the seed's mtime changes (R-32). |
| `squawk_ids` | `dev_name, ids` | `{}`. The shim pushes the ids from `.claude/sqtk-mine.json` (R-26). |
| `subscribe` | `dev_name` | stream of events; the thin one-shot exits on the first |
| `health` | `dev_name?` | `{backends:{name:{state, since}}}`, keyed by backend name. Discord's entry is `ok`, or `degraded` while scream-hole is unhealthy (D-106). With `dev_name`, an agent the daemon doesn't hold gets `not_registered`, so the shim re-registers after a daemon restart (#112). Without it, `health` is the daemon's liveness check. |
| `ping` | `dev_name` | `{}`, or `not_registered` for an agent the daemon doesn't hold (#129). The shim's timer asks it every 15 s, so an idle session re-registers after a daemon restart without waiting for a tool call. It touches no backend, unlike `health`. A daemon older than `ping` answers `unsupported`, and the shim stops asking. |

Every chat frame (`send`, `dm`, `read`, `thread`, `react`, and the Discord ops) names the agent it acts as (`dev_name`) and the backend it goes to (`backend`), because one daemon per host serves N backends. `off_default: true` marks a call that named a backend other than the agent's default, and is logged (R-13).

**One backend-name form** (Story 2.4c). Every backend name on the socket and in files is `mattermost:<server_id>` for a Mattermost server (`mattermost:dev`), and `discord` for the host's one guild (`discord:<guild_id>` is reserved). `register`'s `backends`, `health`'s keys, every chat frame's `backend`, `chat-defaults.json`, the identity file's `home_backend` and the persisted thread set all use it, and nothing translates between forms. A bare server id on the socket is `bad_request`.

**Discord's guild** (Story 2.4c). One daemon serves one guild (`[discord] guild_id`). A `guild_id` other than the configured one is `bad_request`, and no call is made. All Discord REST goes through scream-hole (D-106): see Story 2.2.

Unknown ops, and frames with a `proto` higher than the daemon supports, get `ok:false, error:"unsupported"`. Lower `proto` values are accepted (CP-05).

### 5.5 MCP Tool Contract

The tools are `chat_send(to, text, thread?, backend?, attach_path?, embed?)`, `chat_read(channel, since?, limit?, post_id?)`, `chat_thread(post_id)`, `chat_dm(to, text, backend?, attach_path?, embed?)`, `chat_react(post_id, emoji)`, `chat_health()` and `chat_set_default(key, backend)` (D-057), plus Discord's four guild tools below (D-108). `chat_read` with `post_id` reads that one message. `chat_health()` sends the agent's `dev_name` (#112). `chat_set_default` records the human partner's answer to the R-11 question in `~/.claude/chat-defaults.json`, under `key` (§5.10). It is the **one local-only tool**: it writes that file as the agent's user and sends nothing to the daemon. Every other tool forwards one frame to the socket, except that an `attach_path` is first streamed as `upload` frames (§5.4). `attach_path` is a local file of at most 8 MiB, read by the shim as the agent's user; `embed` is `"title:body"`, split on the first colon. On Discord the embed is a rich embed `{title, description}`, as today; on Mattermost it is a message attachment (`props.attachments: [{title, text}]`) and the file goes through `POST /api/v4/files` with the agent's token (CT-01). Neither is ever silently dropped: each is delivered, or the call fails loudly.

A call on a post (`chat_send` with `thread`, `chat_thread`, `chat_react`) that names no backend asks the daemon's `thread_backend` where the post is (R-12). The shim keeps no thread-to-backend map, so a reply follows its thread across a restart of either side; `not_found` fails loudly and never falls back to the default backend.

**Discord's guild tools** (D-108). They take today's `disc_*` argument shapes, always send `backend="discord"`, and log nothing; on a Mattermost backend the daemon answers `unsupported`:

| tool | args | daemon op |
|---|---|---|
| `chat_list_channels` | `guild_id`, `type?` (`text` \| `voice` \| `category`, default `text`) | `list_channels` |
| `chat_resolve` | `guild_id`, `name`, `type?` (`channel` \| `user` \| `role`, default `channel`) | `resolve` |
| `chat_create_channel` | `guild_id`, `name`, `type?` (`text` \| `voice` \| `category`, default `text`) | `create_channel` |
| `chat_create_thread` | `channel_id` (ID or name), `name`, `message?` (initial message) | `create_thread` |

There are **no `disc_*` aliases.** D-057 kept them for one kit minor version. D-108 removed them in v0.2.2, before any host ran the daemon, because the kit calls no `mcp__crewchat__disc_*` name. Each old name's replacement: `disc_send` is `chat_send` (a channel `name` or `#name` resolves); `disc_read` is `chat_read`, with `post_id` for `message_id`; `disc_thread`, `disc_dm` and `disc_react` are `chat_thread`, `chat_dm` and `chat_react` (`<channel>/<message>`); the other four are the guild tools above.

### 5.6 Broker

The broker is a Python service under the `crewbroker` user, with **one instance per (crew, Mattermost server)**: systemd unit `crewbroker@<crew>-<server_id>`. Several crews, and several servers per crew, can share a host. Each instance has its own files:

| What | Path |
|---|---|
| broker account credential (mode `0600`) | `/etc/crewbroker/<crew>-<server_id>.credential` |
| configuration | `/etc/crewbroker/<crew>-<server_id>.toml` |
| state, including the token-id ledger `tokens.json` and `accounts.json` | `/var/lib/crewbroker/<crew>-<server_id>/` |
| socket | `/run/crewbroker/<crew>-<server_id>.sock` |

The daemon finds each broker through `[crews.<crew>.<server_id>] broker_socket` in `/etc/crewchat/servers.toml`, and keys tokens by (agent, server_id).

The configuration names:
- the crew;
- the one server it serves (`server_id` and URL);
- the crew's human partner user IDs;
- the trusted crews, **by name only** (`trusted = ["<crew>", …]`). The list lives with the broker because it is the human partner's setting, next to the switch;
- optionally, the team each minted bot joins and that team's default channels, as Mattermost IDs (`team_id`, `default_channel_ids`; #132, D-109). Both default to empty, which manages no membership. A name in either is refused at load.

**Trusted crews in this Plan are crews whose broker for this server is on this host.** **No broker ever calls another broker.** The daemon already holds a broker client for every (crew, server) on its host, so it builds trusted membership itself: for each name in our `crew_info.trusted`, it calls that crew's own broker (`[crews.<name>.<server_id>] broker_socket` in `servers.toml`) for its `crew_info`. There is no hand-copied list. Trust across hosts is deferred (§5.N, item 7).

It serves the daemon over its socket, using the same frame shape (D-058). Every op is refused unless the peer's `SO_PEERCRED` UID is `crewchat`. **Every daemon→broker call has a short timeout (default 2 s, `broker_timeout_s`).** On a timeout the daemon keeps that crew's last good `crew_info`; only with no cached `crew_info` (or when a cache-miss re-fetch times out) is the crew unresolvable (Mattermost `DROP`). It never stalls the daemon.

| op | What it does |
|---|---|
| `mint{dev_name, crew, current_token_id, deadline}` | Called **only** in response to a daemon `register`. Creates the bot account if it is missing (R-03), records `last_register` for the account, mints a token, sets native expiry when the server supports it (R-07), and returns `{account, token, token_id}` to the daemon only. With `team_id` configured, it then ensures the bot's membership of the team and of each default channel, in order, only for a bot whose `owner_id` is this broker, and adds `membership: {joined, failed: [{id, error}]}` to the answer (D-109). An existing member counts as joined. A failed add is logged at ERROR and never fails the mint. |
| `remint{dev_name, crew, current_token_id, deadline}` | Mints a fresh token for an existing account and returns it, **without** revoking the token the daemon holds, and without touching `last_register`. Used by daily rotation and by the daemon's 401 refresh. |
| `revoke{token_id}` | Revokes a token. The daemon calls it after it has swapped in the new token and reconnected. |
| `deactivate{dev_name, crew}` | Disables the account's bot now, as the 30-day prune does (`manage_bots`, R-06), records `pruned_at`, and returns `{account, delete_at}`, with `delete_at` read back from the server. `not_found` for an account that never registered, `squatted` for a bot this broker no longer owns. The next `mint` reactivates it (R-03). `verify-install` calls it last, for its canary, so a verify leaves no active bot (#127). |
| `switch_state{}` | Returns the switch setting, its expiry, the setting it reverts to, and the kiosk on/off flag. |
| `crew_info{}` | Returns **only this broker's own fields**: `{broker_user_id, human_user_ids, trusted: ["<crew>", …], minted_bot_ids}`. `minted_bot_ids` lists the bot account user IDs this broker created. The daemon caches it with the switch state (§5.9), and gets each trusted crew's `broker_user_id`, `human_user_ids` and `minted_bot_ids` **only** from that crew's own broker's `crew_info`. |

On `mint` and `remint`, `current_token_id` names the token the daemon holds for that account on that server (`null` for none). After minting, the broker revokes the account's other recorded tokens, at most 5 per call, so a token minted by a call whose reply timed out is not left live (#41, D-102). `deadline` is the caller's timeout in epoch seconds on the shared host clock. A request the broker reaches after its deadline is refused with `expired` and mints nothing, so a late request can't supersede the token a newer call swapped in. A caller that omits `current_token_id` gets no cleanup, and one that omits `deadline` is never refused as `expired`.

The daemon owns rotation (§4.8). The broker's daily job only deactivates bot accounts whose `last_register` is more than 30 days old (R-30), so an account that is merely re-minted is never counted as active. The broker also listens on its own websocket for DMs from the configured human user IDs, and parses switch commands from them (§4.6).

### 5.7 Protocol Marker

- **Mattermost:** each post carries `props.crew_proto = 1`. The author is a real account, so there is no signature line.
- **Discord:** the existing signature line gains a trailing `·p1`, as in `— **<name>** <avatar> (<team>) ·p1` (D-059). That old parsers ignore the suffix is still to be verified (§5.N).

### 5.8 Doorbell Core

- The daemon keeps one queue per registered agent, fed by three adapters: Mattermost (websocket), Discord (scream-hole) and squawkie (SSE, filtered to the squawk IDs the shim pushed with `squawk_ids`, the same predicate as `sqtk-doorbell.sh`).
- Each event passes the filters (§5.9) and is queued.
- `subscribe` returns the first queued event; the thin one-shot prints it as JSON and exits 0.
- **Delivery is at least once** (#99). `subscribe` holds the event it takes, and the cursor moves past it only once the reply frame has been written. A reply that wasn't written (a hang-up, a timeout, a failed write) leaves the event held, and the next `subscribe` returns it again. So does a second `subscribe` for the same agent that overlaps the first. Every reply carries a stable `event_id`: `<backend>:<first ID>`, or an ID made once for an event without one. Each one names a single occurrence: a Mattermost or Discord post, one state of a squawk (`<id>:<state>|<answer>`), one outage, or one kiosk review of one transcript (its head and offset, §5.11). **The one-shot drops a repeat by it** (#109): it keeps the last 512 `event_id`s it has printed for the Dev-Name, shared by every checkout with that Dev-Name, in `${XDG_STATE_HOME:-~/.local/state}/crewchat/doorbell-seen/<dev_name>`. On a repeat it prints nothing and subscribes again, within the same timeout. An ID is kept only after its line is flushed, so a crash in between gives a repeat, never a loss, and a seen file it can't read is treated as empty. "Written" means `drain()` returned, which proves only that the reply reached the socket's buffer: a one-shot whose `CREWCHAT_DOORBELL_TIMEOUT` fires between the daemon's write and its own read can still lose that event. The default timeout is 0, which waits forever.
- **Shutdown closes every client connection first** (#107). A `subscribe` still waiting is cancelled with nothing written, so the event it holds stays held for the next daemon. Any other op already running, a `send` say, finishes and its reply is written, because cancelling it after its post reached the server would have the agent post it twice. A connection accepted as shutdown begins closes without reading a frame. A read that fails while an op waits is a hang-up, like end-of-file.
- **A client never half-closes its connection** (`SHUT_WR`). The daemon reads end-of-file on a waiting `subscribe` as a hang-up and cancels it, so a client that half-closes after sending its request never gets an event.
- Cursors are kept per agent per backend under `/var/lib/crewchat/cursors/`.
- A backend outage enqueues `{"type":"backend.down","backend":...}` (R-27). A Mattermost reconnect whose replay fails is an outage too, with the failing call in its `reason`. Its connection yields nothing and the position stays put, so the next replay delivers the gap (#99). A team or channel that Mattermost answers 403 or 404 for during the replay, with its own error `id`, is not an outage. A scope the account can't read yet is skipped with a warning naming it, and the replay goes on with the rest. Posts made there before the account can read it are not replayed. A 403 or 404 with no error `id` is a proxy's (Traefik answers 404 when Mattermost is gone), so it is an outage. When every team, or every channel, is unreadable, the replay is retried once, after a pause for a new account's membership to settle. If it is still unreadable, that is an outage: `every replay scope unreadable` (#103).
- `sqtk-doorbell.sh` and `doorbell-oneshot.sh` stay as fallbacks until the cut-over (D-060).

One-shot exit codes:

| code | meaning |
|---|---|
| 0 | an event |
| 2 | not registered, for longer than the restart grace |
| 3 | timeout |
| 4 | daemon unreachable: down for longer than the restart grace, or at once for no permission, a connect timeout, or an oversized, malformed or mismatched reply |

**The one-shot waits through a daemon restart** (#129). A restart closes the one-shot's connection, then the socket refuses or is missing until the new daemon listens, and the new daemon answers `not_registered` until the shim's `ping` registers the agent again. Each of those (a missing or refused socket, a connection closed or reset before the reply, `not_registered`) is retried, with a backoff of at most 2 s, for 60 s from the first failure (`RESTART_GRACE_S`). Only an outage that lasts longer exits, with 4 or 2. The retries spend the same `CREWCHAT_DOORBELL_TIMEOUT`, and a timeout that ends inside an outage exits with the outage's code, not 3. The event a `subscribe` held when the daemon stopped stays held (shutdown above) and comes from the new daemon, and a repeat is dropped by the seen set.

### 5.9 Switch and Loop Guard Placement

- Both run inside the daemon, before an event is queued and before a `read` result is returned (D-061).
- The daemon caches `switch_state` and `crew_info` from each broker per (crew, server), and checks expiry against the cached timestamp on every delivery.
- With no cached switch state or `crew_info`, nothing from **Mattermost** is delivered (R-22). Discord and squawkie keep flowing, because neither depends on the broker (per-backend table below, R-27).
- Trusted crews' membership data comes **only** from each trusted crew's own broker's `crew_info`, which the daemon fetches directly for every name in our `crew_info.trusted` (§5.6). A trusted name with no `[crews.<name>.<server_id>]` table on this host is skipped (logged once), and all its senders are outsiders. A trusted crew whose broker doesn't answer within the timeout keeps its last good `crew_info`. A trusted crew never fetched since the daemon started has no member list, so its bot accounts are still recognised by `owner_id` against that crew's `broker_user_id` in `servers.toml` and are `DROP`, and its humans are treated as outsiders. A cache-miss re-fetch that times out gives `DROP` (R-22).
- **Cache miss:** when a bot account's `owner_id` matches **any local broker's** `broker_user_id` (ours, or a trusted crew's on this host) but its user ID isn't in that broker's cached `minted_bot_ids`, the daemon re-fetches **that broker's** `crew_info` once before giving any verdict. Re-fetches are capped at once per bot account ID per 30 s. After any `mint` by any local broker, the daemon refreshes that broker's cached `crew_info`; all brokers are local, so every verdict reading it is current. A newly registered crewmate or trusted-crew agent is therefore never treated as an outsider or routed to the kiosk.

**Crew membership on Mattermost:**
- A bot account is in crew X **if and only if** its Mattermost `owner_id` equals X's broker account user ID **and** its user ID is in X's `minted_bot_ids`. That holds for X = the agent's own crew, and for each trusted crew against that crew's info. Both conditions are needed, because a bot's owner can reassign it to another user (`POST /bots/{id}/assign/{user}`).
- A human is in X if and only if their user ID is in X's `human_user_ids`, from X's own broker's `crew_info`. For a trusted crew, that list is what the trusted crew's own broker reports, so its operator, not our partner, decides who its humans are (D-087).
- **A trusted crew's sender under crew only** is treated as an outsider: kiosk if directly addressed, otherwise dropped.
- **Username suffixes are never used for membership.** Anyone can register a username like `x.oaw`.
- Anything else is an outsider. A user the server can't resolve is unresolvable, and is dropped (R-22).

**Per backend:**

| Backend | Rule |
|---|---|
| Mattermost | The switch and the kiosk apply, by the membership rule above. |
| Discord | Messages are **crew-equivalent**: our guild is membership-controlled, and its existing kill switch still applies. This keeps today's Discord behaviour (R-15). The switch and the kiosk do not apply to Discord. |
| squawkie | **Exempt.** The adapter delivers only the agent's own squawks (R-26). |

**My threads (R-19, D-104):** every successful daemon-side `send` and `dm` records the post's thread root, with its backend, in the agent's `MyThreads`, the set `addressed_to` and `is_direct_address` read for "a reply to me", and `thread_backend` reads for a reply's backend (R-12, Story 2.4c). The daemon holds one persisted instance, loaded at start from `<state_dir>/threads/<dev>.json` (`/var/lib/crewchat/threads/`, JSON `{"roots": {"<root_id>": {"backend": "<name>", "last_seen": "<iso8601>"}}}`, the backend in §5.4's one name form, file `0600`, directory `0700`, written atomically after each change). Roots whose `last_seen` is older than `my_threads_retention_days` (top-level in `servers.toml`, default 90) are pruned on load and on save; this is our own setting and never reads a server's retention policy (CT-01). A corrupt file is logged once and read as empty.

**Verdicts by path:**
- **Push order:** crew resolution, then addressing (R-16 to R-19), then the loop guard (R-25), then the switch. Push verdicts are `DELIVER`, `DELIVER_MARKED_OUTSIDER`, `KIOSK` (the event goes to the kiosk's holding queue), or `DROP`.
- **Kiosk routing needs direct address.** Only `@<dev>`, `@<dev>.<crew>`, a DM, or a reply in the agent's own thread can earn a `KIOSK` verdict. An outsider's broadcast (`@agents`) or crew/team token is `DROP` under crew only and trusted. Each outsider is also rate-limited to 10 kiosk routings per hour **per expert, per server** (`kiosk_rate_per_hour`, configurable); excess is `DROP`. Above both, the operator's per-server controls in `servers.toml` (#117): a server without `kiosk = true` never gives `KIOSK`, whatever the switch says (`DROP("kiosk_server_off")`), and each server's kiosk answers are capped per UTC day (`kiosk_daily_cap`, default 25, persisted in `kiosk-daily.json`; excess is `DROP("kiosk_daily_cap")`).
- **Pull** (`read`, `thread`): **membership and the switch only**; addressing and the loop guard do not apply, so a channel read returns crewmates' posts whether or not they address the agent. An outsider's post is **omitted, never enqueued**, under crew only and trusted, so a read never re-sends old outsider posts to the kiosk; under social it is returned marked. Pull never produces a `KIOSK` verdict. Kiosk posts (`crew_kiosk=1`) are omitted under crew only and trusted.
- **Kiosk posts:** a post carrying `props.crew_kiosk=1` is treated as outsider content whoever authored it, and is **never** given a `KIOSK` verdict: it is `DROP` under crew only and trusted, and `DELIVER_MARKED_OUTSIDER` under social. A crewmate never receives outsider-steered kiosk text as crewmate traffic, and two kiosks can't answer each other.

**Loop guard (R-25):**
- It applies to messages from **bot accounts**. Human messages are admitted when addressed (R-16 to R-19).
- A message from a bot account is admitted only in an opted-in conversation and only with an inline mention. A conversation is opted in when the agent started it, posted in it, or was mentioned into it, and every DM is opted in. A mention is `@<dev>`, `@<dev>.<crew>`, `@agents`, a crew token or a team token.
- A sliding window counts messages from bot accounts per conversation, and drops anything past the threshold (`loop_guard_window` in `servers.toml`, default 20).

### 5.10 Default-Backend File

`~/.claude/chat-defaults.json` maps a key to a backend (D-062):

```json
{"gitlab.com/example-group": "mattermost:oaw-dev", "/home/u/sandbox/scratch": "discord"}
```

Keys are `<forge-host>/<org>`, or an absolute repo path when there is no forge. Values are `mattermost:<server-id>` (a server id from `/etc/crewchat/servers.toml`) or `discord`. Only the shim reads and writes this file, as the agent's user: it reads it to resolve the default, and writes it through `chat_set_default` after asking the human partner. The daemon never touches it (§5.3).

### 5.11 Kiosk

- **Routing:** when social is off and the kiosk is on, an outsider's message goes to the expert's kiosk session (R-31). There is **one session per expert per server**, matching the per-server transcripts, started lazily on the first outsider message on that server. Outsiders on different servers never share a session (S-02).
- **Seed:** the shim, as the agent's user, reads `.claude/reseed-revive.md`, extracts **only** the `## Kiosk` section with `crewchat_clients.brief.extract_brief` (from that heading to the next `## ` heading or the end of the file), and pushes only that text with `kiosk_brief`. It pushes again whenever the file's mtime changes. Nothing else from the seed ever crosses the socket (R-32, D-072). An absent or empty section gives a fixed reply: "I have nothing to share yet."
- **Session:** the kiosk is a tool-less model session. Its prompt is:
  - a fixed kiosk header;
  - the brief;
  - **only the asker's own posts in the thread, and the kiosk's own earlier answers to that asker.** Crewmates' posts in the same thread are never included, so an outsider can't pull crew conversation into the kiosk.

  The transcript is appended to `/var/lib/crewchat/kiosk/<dev-name>-<server_id>.jsonl`.
- **Output:** @mentions are stripped from the kiosk's answer before posting. The post carries `props.crew_kiosk=1`, so `admit` treats it as outsider content (§5.9).
- **Mechanism:** open. Spike 1.2 decides between `claude -p` with tools and MCP disabled, and a direct model API call with no tool definitions (D-071). A 2026-10-23 test of `claude -p --resume --fork-session --tools ""` hung to its timeout.
- **If the CLI runner is chosen,** it runs as its own unprivileged OS user, `crewkiosk`, not as `crewchat`, which holds every token. It has an empty HOME and an empty working directory, and runs `claude -p --restricted --strict-mcp-config --tools "" --disallowedTools "mcp__*"`. `--restricted` ignores user, project and local settings, so no user, project or local settings, hooks or `CLAUDE.md` load; the empty working directory also means no project `CLAUDE.md`. **Managed settings and `--settings` still apply** under `--restricted`, so the kiosk host must have no managed settings configured, and spike 1.2 plants a hook in the managed-settings location too. The spike must prove, from the CLI's own transcript or debug output and not from our argv, that none of them loaded. `crewkiosk` holds **no chat or broker credential**. It does need a model credential to run at all; its path and owner are a named output of spike 1.2.
- **Review:** when the switch moves to social on a server, the daemon enqueues a `kiosk.review` event for the working agent. It carries **only that server's** transcript lines since that server's cursor (`/var/lib/crewchat/kiosk/<dev-name>-<server_id>.cursor`), marked as outsider content. That cursor advances once the review is handed over to the agent, not when it is enqueued, so a restart before then delivers it again (R-33). The cursor holds the offset and the transcript's **head**, the first 16 hex digits of the SHA-256 of its first complete record, which names the answer's own post (#110). A transcript whose head isn't the cursor's, or that is shorter than the offset, has been replaced, and is reviewed from its start; a first record still being appended gives no head. A review's `event_id` is `mattermost:<server_id>:kiosk.review:<head>:<offset>`, so a new transcript's review never repeats an old one's. An agent that registers while social is already on, after a daemon start or mid-social, gets its review then (#99). The transcript is kept per server: `/var/lib/crewchat/kiosk/<dev-name>-<server_id>.jsonl`.
- **Off:** with the kiosk off, outsider messages are dropped (R-34).

### 5.A Deliverables Manifest

| ID | Deliverable | Category | Tier | File Path | Produced In | Status | Notes |
|----|-------------|----------|------|-----------|-------------|--------|-------|
| DM-01 | README.md | Docs | 1 | `README.md` | P1W1 (Story 1.1); updated in P3W6 (Story 3.9) | required | Program overview, workspace layout, quickstart |
| DM-02 | Unified build system | Code | 1 | `Makefile` | P1W1 (Story 1.1) | required | Targets `lint`, `test`, `ci`, `verify-install`, wrapping uv |
| DM-03 | CI/CD pipeline | Code | 1 | `.github/workflows/ci.yml` + `scripts/ci/*.sh` | P1W1 (Story 1.1) | required | Mattermost Team and Enterprise service containers; a two-user image |
| DM-04 | Automated test suite | Test | 1 | `tests/unit/`, `tests/integration/`, `tests/e2e/` | P1W1 (Story 1.1) onward | required | pytest |
| DM-05 | Test results (JUnit XML) | Test | 1 | `reports/junit.xml` | P1W1 (Story 1.1) | required | CI artifact |
| DM-06 | Coverage report | Test | 1 | `reports/coverage.xml` | P1W1 (Story 1.1) | required | A completeness signal, with no fail-under gate |
| DM-07 | CHANGELOG | Docs | 1 | `CHANGELOG.md` | P1W1 (Story 1.1); each release | required | Release-by-release summary |
| DM-08 | VRTM | Trace | 1 | Dev Spec Appendix V | P3W6 (Story 3.9) | required | Every R-01 to R-34 |
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
   - `/etc/crewchat/servers.toml` lists the configured servers (D-032), the host's `default_crew`, the optional top-level `my_threads_retention_days` (default 90, D-104), a `[crews.<crew>] needs_list` per crew, and a `[crews.<crew>.<server_id>]` table per crew and Mattermost server on the host with `broker_socket` and `broker_user_id`. The `broker_user_id` entries are **the configured broker list**: the only accounts that may hold the patched role (R-06).
   - The socket is `/run/crewchat/crewchat.sock`. The agents' user, and each container's mount, must be in `crewchat-clients`.
3. **Kiosk user** (only if spike 1.2 picks the CLI runner): create the unprivileged `crewkiosk` user with an empty HOME. It holds no chat or broker credential. Its model credential is installed at the path and with the owner that spike 1.2 names. Configure no managed Claude Code settings on the kiosk host (§5.11).
4. **Broker:** create the `crewbroker` user.
   - `uv sync --frozen --package crewbroker`
   - Install `packaging/crewbroker@.service`, a template unit with one instance per (crew, Mattermost server): `crewbroker@<crew>-<server_id>`.
   - Install `packaging/crewbroker.tmpfiles.conf` to `/etc/tmpfiles.d/crewbroker.conf` and run `systemd-tmpfiles --create`.
   - Configure `/etc/crewbroker/<crew>-<server_id>.toml` and `/etc/crewbroker/<crew>-<server_id>.credential`.
   - For the OaW crew during development, run it only against the dev space (CP-06).
5. **Thin clients:** shipped by the kit's `./install` from a tagged `mattermost-crew` release (K-1 and K-2). Fleet installs need BJ.
6. `make verify-install` runs the runbook's verify section:
   - daemon `health`;
   - broker `mint` for a canary;
   - the sockets' permissions;
   - that the agent's user cannot read any credential. It checks each **known file name** with `test -e <file> && ! sudo -u agent test -r <file>`, never a glob: each daemon token file `/var/lib/crewchat/tokens/<dev>.json`, `/etc/crewbroker/<crew>-<server_id>.credential`, `/var/lib/crewbroker/<crew>-<server_id>/tokens.json`, and `/etc/crewchat/discord.credential`;
   - that `crewkiosk` (if created) can read none of those files. Its own model credential is the one file it may read.

#### CI/CD Pipeline

| Stage | Trigger | Steps | Artifacts Produced | Gate |
|-------|---------|-------|-------------------|------|
| Validate | every push and PR | `make lint` (ruff, mypy) | none | must pass to merge |
| Test | every push and PR | `make test` against three Mattermost legs (Team Edition 11.11.3, Enterprise 11.11.3, Team Edition 11.8.x), in a two-user image | DM-05, DM-06 | must pass to merge |
| Install verify | every push and PR | IT-20: the runbook's install and verify, run in a fresh container | none | must pass to merge |
| Release | version tag | build sdists and wheels; publish to the GitHub release | wheels | tag cut by babelfish (lead grant) at the integration points |

#### Production / Release Deployment

Deploying onto any production Mattermost needs a yes-prod from the agent's human partner (CP-03). The broker's routine minting, rotation and revocation rides on the role-patch yes-prod. Deploying onto the OaW dev space needs none (CP-06), but wiping it needs BJ.

### 5.N Open Questions

Spike Story 1.2 resolves each of these, and records each answer as a `[ledger]` entry on Plan #3. The evidence is in `docs/spikes/2026-10-spike-1.2.md`.

1. **Enterprise Entry's own limits:** e.g. a cap on message history that would affect the read path (D-007). Answered from the vendor's docs, plus a check against a licensed image where one is available.
   - **Answer (D-093):** Entry shows only the newest 10,000 posts across all channels; older posts stay stored but aren't returned to reads or search. The read path detects this at runtime from `GET /api/v4/limits/server` (`postHistoryLimit`, `lastAccessiblePostTime`). Nothing in Entry's limits touches bots, tokens or the API we use. On Team Edition, bot accounts don't count toward the 200/250 user limit. No design change.
2. **One client per token in one process:** does the upstream library support it? If not, §5.2 is revisited (D-054).
   - **Answer (D-094):** Yes. `mcp-server-mattermost==0.6.1` (the PyPI distribution; import `mcp_server_mattermost`) takes a token per `MattermostClient`. 22 posts from two clients, on their own pools and concurrently on one shared pool, all had the right author. Each client's `lifespan()` must be entered once and held for the token's life, never entered concurrently per call. §5.2 stands.
3. **The Discord `·p1` suffix:** is it safe for today's parsers (the watcher's echo filter and signature regex)? (D-059)
   - **Answer (D-095):** Safe. The watcher (`v1.7.0`) matches `— **<name>**` as a substring in both its echo filter and its reply routing, and gives the same verdicts with ` ·p1`. `mcp-server-discord` (`v1.4.3`) has no signature parser.
4. **The kiosk mechanism:** `claude -p` with tools and MCP disabled, or a direct model API call with no tools. Either way, name the kiosk's **model credential**: its path and owner. If the CLI, prove that nothing loads, including from the managed-settings location (§5.11). (D-071)
   - **Answer (D-096, resolves D-071):** The CLI runner, as `crewkiosk`. It ran no shell command. Its own transcript and debug log show that no user, project or local settings, hooks, skills or `CLAUDE.md` loaded, against planted ones in every location; a planted managed hook and managed `CLAUDE.md` did load until `/etc/claude-code` was removed. Measured: about 5.4 s and $0.003 per question (Sonnet). Model credential: `/etc/crewkiosk/model.credential`, mode `0600`, owner `crewkiosk:crewkiosk`, a dedicated Anthropic API key that no other user can read. The kiosk host must have no local **or remote (org-pushed)** managed settings.
5. **An unlicensed Enterprise image as a stand-in for Entry in CI:** is it faithful? (§6.1) Answered from the vendor's docs, plus a licensed-image check where one is available.
   - **Answer (D-097):** Faithful. By the vendor's definition an unlicensed Enterprise server *is* Entry, and the image reports `SkuShortName: "entry"` and `postHistoryLimit: 10000`. Every bot and token check matches Team Edition 11.11.3.
6. **Facts carried from the sketchbook:** are bot-to-bot DMs supported? Do bot accounts appear in `@` autocomplete, with their own avatars?
   - **Answer (D-098):** Bot-to-bot DMs work. Bots appear in `@` autocomplete (`is_bot: true`). Avatars are per bot, but a bot can't set its own (403); its owner, the broker, sets it at mint (`POST /users/{bot_id}/image`). The server marks every bot post `props.from_bot: "true"`.
7. **Broker transport, and trust, when a broker is on another host:** for this Plan the daemon and every broker it uses (its own and its trusted crews') share one host, using unix sockets, and no broker calls another. **Trust across hosts is deferred:** a trusted crew whose broker is not on this host has no `servers.toml` table and no reachable `crew_info`, so all its senders are treated as outsiders (the kiosk if directly addressed, otherwise dropped under crew and trusted). Spike 1.2 records the remote option (e.g. mutual-TLS TCP from the daemon to a remote broker, carrying `crew_info`) as a design note, for a later Plan.
   - **Answer (D-099):** Design note recorded for a later Plan. A remote broker serves only `crew_info` (and optionally `switch_state`) over mutual-TLS TCP, with the same frames, pinned per trusted crew by CA and `broker_user_id` in `servers.toml`, with the same timeout and caching rules. Tokens never cross hosts.
8. **Bot `owner_id`:** verify that a bot account's `owner_id` is the creating broker account, is reported by the API to any user, and cannot be changed by the bot itself. Also confirm that the owner **can** reassign it with `POST /bots/{id}/assign/{user}`, which is why membership also requires `minted_bot_ids` (§5.9).
   - **Answer (D-100, supersedes D-079 and D-087 in part):** `owner_id` is set to the creating broker; the bot can't change it; the owner can reassign it, and then the former owner can't see the bot. **But `owner_id` is not reported to other users:** `GET /bots/{id}` is 404 without `read_bots` on that bot or `read_others_bots`, and the user object has no owner. So the daemon can't check `owner_id`. Ownership moves into the broker: `crew_info.minted_bot_ids` lists the bots it created **and still owns** (intersected with its own `GET /bots`), and a bot is in X if and only if it is in X's `minted_bot_ids`. On a cache miss, an `is_bot` sender in no cached list triggers one re-fetch of every local broker's `crew_info` (once per bot per 30 s). While any local broker has no cached `crew_info`, an unrecognised bot is `DROP`. §5.9's text is to be brought in line with D-100.
9. **11.8 compatibility** (owned by spike 1.2, run against a local 11.8 Team Edition container; Story 1.1's 11.8 CI leg then keeps it true): confirm that `owner_id`, `POST /bots/{id}/assign`, the `system_user_access_token` role patch and token create/revoke behave as on 11.11, and that the absence of `expires_at` is detected (R-07). Anything that fails is recorded here with its ledger ID (D-091).
   - **Answer (D-101):** On Team Edition 11.8.5, the newest 11.8 patch and the one `ci.yml` pins, everything behaves as on 11.11.3, including question 8's `owner_id` visibility. Native expiry is detected as absent by the missing `expires_at` key on the token object; on 11.11 the key is present and honoured on create. CT-11 stands.

**Deferred, decided by usage** (policy for BJ, not facts a spike can settle): private DMs vs. visibility; how `@<dev-team>` behaves across crews; whether crew names need to be unique for `@crew` routing.

---

## 6. Test Plan

### 6.1 Test Strategy

- **Unit tests** are pytest, per package, specified per story in §8.
- **Integration and E2E tests** run in CI against real Mattermost containers, on three legs: Team Edition 11.11.3, an unlicensed Enterprise 11.11.3 image standing in for Entry (CT-01; to verify, §5.N-5), and Team Edition 11.8.x, the oldest supported server (CT-11; latest patch, pinned in the workflow). The rate limits match the dev space.
- The CI image has **two users**, so the daemon runs apart from the test "agent" user. That makes R-08 actually testable (IT-01).
- Discord is exercised through a scream-hole fake. squawkie is exercised through a local SSE fake.
- The final phase ends with a smoke run on the OaW dev space, run by an agent and posted to Plan #3.

### 6.2 Integration Tests (Automated)

| ID | Boundary | Description | Req IDs |
|----|----------|-------------|---------|
| IT-01 | daemon ↔ broker ↔ Mattermost | The broker creates the bot account and mints a token, and the daemon stores it. For each known credential file (`/var/lib/crewchat/tokens/<dev>.json`, `/etc/crewbroker/<crew>-<server_id>.credential`, `/var/lib/crewbroker/<crew>-<server_id>/tokens.json`, `/etc/crewchat/discord.credential`), `test -e` succeeds and `sudo -u agent test -r` fails; no globs. No socket op returns a token to a non-daemon peer. The broker account holds the patched role and no system-admin role, and the patched role is held only by the configured broker accounts (asserted against this host's `servers.toml` `broker_user_id` list; in CI every broker is on this host, so that list is all of them) | R-01, R-03, R-05, R-06, R-08 |
| IT-02 | daemon ↔ broker ↔ Mattermost | Daemon-driven rotation: `remint` issues a new token, the daemon swaps it in and reconnects the websocket, then `revoke` retires the old one; events keep arriving across the rotation; native expiry is set where supported | R-05, R-07 |
| IT-03 | broker ↔ Mattermost | With an injected clock, a bot account with no `register` for 31 days is deactivated even though it was re-minted daily; one registered 29 days ago is kept | R-30 |
| IT-04 | thin MCP ↔ daemon | Default resolution order, and the loud failure plus squawk in headless mode | R-11 |
| IT-05 | daemon ↔ adapters | A reply follows its thread and backend; an explicit backend is used and logged | R-12, R-13 |
| IT-06 | adapters | The protocol marker is written on both backends; a proto-0 message (no marker) still parses | R-14 |
| IT-07 | daemon addressing | `@agents` and `@all` (Discord, during the transition), `@name.crew` kept, "reply to me" semantics, and a plain DM to the agent counts as addressed | R-16, R-17, R-18, R-19 |
| IT-08 | daemon filters | The three switch modes; push and pull both gated; an unresolvable crew is dropped. A spoofed username (`x.<crew>` owned by another account) is **not** admitted as crew, and neither is a bot reassigned to the crew's broker that isn't in its `minted_bot_ids`; membership needs both `owner_id` and `minted_bot_ids`. With no cached `crew_info`, Mattermost traffic is dropped while Discord keeps flowing. Discord messages are crew-equivalent and squawkie events are exempt. A trusted crew's bot account is recognised only through the `minted_bot_ids` in **that crew's own broker's** `crew_info`, which the daemon fetches directly; with that broker not answering within the timeout, its last good `crew_info` is kept; a never-fetched trusted crew's bot accounts are dropped by `owner_id`, its humans are outsiders, and the daemon never stalls. A newly registered crewmate or trusted-crew agent (cache miss) is re-checked and never routed to the kiosk. On pull, an unaddressed crewmate post is returned. A trusted crew's sender under crew only is an outsider | R-20, R-21, R-22 |
| IT-09 | daemon ↔ broker | Social lapses on delivery after expiry and reverts, the partner is notified, a dead broker can't hold social open, and only the partner's user ID can flip | R-23, R-24 |
| IT-10 | daemon filters | Loop guard on messages from bot accounts: opt-in (started, posted in, mentioned into, or a DM), mention required (`@<dev>`, `@<dev>.<crew>`, `@agents`, crew or team token), sliding-window drop; an addressed human message is admitted without a mention | R-25 |
| IT-11 | doorbell | Events from all three adapters merge, one exit per event | R-26 |
| IT-12 | doorbell | One backend down: a `backend.down` event, and the others keep flowing | R-27 |
| IT-13 | doorbell | Reconnect replays from the cursor, queuing each missed event exactly once | R-28 |
| IT-14 | Mattermost adapter | Server mention markers are used; echo is filtered by user ID | R-04, R-29 |
| IT-15 | Discord adapter | Today's `disc_*` behaviours, through `chat_*`, through the shim against the scream-hole and Discord REST fakes (D-108): `chat_send` (by ID, `name` and `#name`, with an attachment and an embed), `chat_read` (with `limit`, and with `post_id` returning one message), `chat_thread`, `chat_dm`, `chat_react` (with `<channel>/<message>`), and the guild tools `chat_list_channels`, `chat_resolve`, `chat_create_channel` and `chat_create_thread` (with an initial message). No `disc_*` tool remains, and none logs a deprecation | R-15 |
| IT-16 | daemon | One contract across N configured Mattermost servers plus Discord: one agent registered on **two** Mattermost servers, each with its own (crew, server) broker and token, sends and receives on both | R-09, R-10 |
| IT-17 | broker | Account naming fits CT-05; the crew defaults to the partner's username and can be overridden | R-01, R-02 |
| IT-18 | broker ↔ Mattermost | A test user standing in for the human partner DMs `social 2h`, `crew` and `kiosk off`; the state, expiry and lapse notice change | R-23, R-24, R-34 |
| IT-19 | Mattermost API | Each bot account's avatar is set (`GET /users/{id}/image`) and the bot account is listed by `GET /users/autocomplete` | R-01 |
| IT-20 | runbook | The install and verify commands, **extracted from `docs/runbook.md`**, run in a fresh container | R-06, R-08 |
| IT-21 | daemon ↔ kiosk | Under crew only, an outsider's message reaches the kiosk; the kiosk starts lazily, has no tools, and sees only the `## Kiosk` section; an empty section gives the fixed reply. A crewmate's post in the same thread never reaches the kiosk's prompt. The kiosk's answer carries `props.crew_kiosk=1` and no @mentions, and is treated as outsider content by `admit`. Two crewmates' kiosks in one thread produce no second kiosk answer. An outsider's `@agents` broadcast reaches no kiosk, and an outsider past the per-hour kiosk rate limit is dropped | R-31, R-32 |
| IT-22 | daemon ↔ working agent | Engaging social delivers the kiosk transcript since the cursor, marked as outsider content; the cursor advances once it is handed over; an agent registering while social is on gets it then | R-33 |
| IT-23 | daemon filters | With the kiosk off, an outsider's message is dropped | R-34 |
| IT-24 | daemon ↔ Mattermost | An agent posts a new root through the daemon's `send`, the daemon restarts, and a human's reply in that thread is addressed to the agent (`addressed_to` true through the persisted `MyThreads`) (D-104) | R-17, R-19 |
| IT-25 | daemon ↔ scream-hole | With the scream-hole fake failing its health check, `chat_read`, with and without `post_id`, on Discord answers `unavailable: scream-hole unhealthy`, and the Discord REST fake sees no read; `chat_send` still posts, through the direct fallback. Once the fake is healthy again, the next read is served through scream-hole (D-106) | R-15 |

### 6.3 End-to-End Tests (Automated)

| ID | Flow | Description | Req IDs |
|----|------|-------------|---------|
| E2E-01 | Cross-crew collaboration | The OaW crew and a synthetic second crew, each with its own human, collaborate in a shared channel under trusted crews | R-01, R-09, R-12, R-16, R-20, R-26 |
| E2E-02 | Outsider and switch | An unlisted human writes to a crew-only agent and is answered by the kiosk. The partner engages social; the kiosk review is delivered, the next outsider message reaches the working agent marked as outsider content, and social lapses on time | R-21, R-23, R-31, R-33 |
| E2E-03 | Fleet broadcast | `@agents` to about 30 registered agents stays within the rate limit | R-16, R-25 |

### 6.4 Manual Verification Procedures

**None.** Anything an agent can drive through the API is automated and runs in CI (D-063). The human's switch DM is IT-18, bot account avatars and autocomplete are IT-19, and the runbook's install is IT-20.

---

## 7. Definition of Done

- [ ] All Phase DoD checklists are satisfied.
- [ ] Every requirement R-01 to R-34 traces to at least one IT or E2E item in the VRTM, and that item passes. [R-01 to R-34]
- [ ] All IT-01 to IT-25 and E2E-01 to E2E-03 pass in CI on all three legs: Team Edition and Enterprise at 11.11.3, and Team Edition 11.8.x. [CT-01, CT-11]
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

A dedicated dev-agent in `mattermost-crew` (hailing-frequencies) runs `/wavemachine` (D-065). **Each phase is its own Plan and its own `/wavemachine` campaign, landing on `main` independently (D-089):** Phase 1 is #26, Phase 2 is #27 and Phase 3 is #28. `/wavemachine` cuts one campaign branch per Plan and writes `main` once, at the end, so a per-phase Plan is what lets each phase land. Plan #3 stays the program Plan and the ledger of record. babelfish owns the kit integration points, which sit outside the phase Plans' stories: IP-2 follows #27 landing, and IP-3 follows #28. babelfish is also that agent's escalation point.

### Wave Map

```
PHASE 1 — Foundation, broker, identity                    (Plan #26, its own campaign)
P1W1 ─┬─ 1.1 Workspace scaffold (foundation)
      └─ 1.2 Verification spike (§5.N, kiosk mechanism)
           │
P1W2 ─── 1.3 Broker: accounts, tokens, expiry, re-mint, prune
           │
P1W3 ─── 1.4 Daemon core: socket, protocol, register, token store
           │
   ⇒ #26 lands on main
           │
PHASE 2 — Chat contract and adapters                      (Plan #27, its own campaign)
P2W1 ─┬─ 2.1 Mattermost adapter
      ├─ 2.2 Discord adapter
      └─ 2.3 Addressing
           │
P2W2 ─── 2.4 Thin MCP: chat_*, default resolution, disc_* aliases
           │
P2W3 ─── 2.4b register carries team and avatar; persisted my-threads
           │
P2W4 ─── 2.4c R-15 parity: every disc_* alias, attachments, embeds, one backend name
           │
   ⇒ #27 lands on main ⇒ IP-2: tag release → K-1 (babelfish, claudecode-workflow): skills → chat_*, /name registers,
           install thin MCP, chat-defaults ask-once, chat_* tool reference (DM-09b)
           │
PHASE 3 — Inbound defense, doorbell, kiosk                (Plan #28, its own campaign)
P3W1 ─┬─ 3.1 Broker: switch and kiosk control
      └─ 3.2 Doorbell core
           │
P3W2 ─┬─ 3.3 Daemon filters
      └─ 3.4 Thin doorbell one-shot
           │
P3W3 ─── 3.5 Kiosk
           │
P3W4 ─┬─ 3.6 Social-engage kiosk review
      └─ 3.7 Docs: runbook, partner guide, architecture
           │
P3W5 ─── 3.8 E2E suite and remaining API tests
           │
P3W6 ─── 3.9 Closing: VRTM, manifest, partner-name grep, dev-space smoke
           │
   ⇒ #28 lands on main ⇒ IP-3: tag release → K-2 (babelfish, claudecode-workflow): doorbell wiring to the chat
           one-shot (old one-shots kept as fallback), /reseed writes the `## Kiosk` section,
           the kiosk-review step in the doorbell skill
```

| Wave | Stories | Master Issue | Parallel? |
|------|---------|-------------|-----------|
| P1W1 | 1.1, 1.2 | P1W1 master | Yes — 2 independent stories (1.2 is docs and spike scripts only, D-090) |
| P1W2 | 1.3 | Story 1.3 | Single story |
| P1W3 | 1.4 | Story 1.4 | Single story |
| P2W1 | 2.1, 2.2, 2.3 | P2W1 master | Yes — 3 independent stories |
| P2W2 | 2.4 | Story 2.4 | Single story |
| P2W3 | 2.4b | Story 2.4b | Single story (D-103, D-104; after 2.4 integrates) |
| P2W4 | 2.4c | Story 2.4c | Single story (D-105, D-106; after 2.4b integrates) |
| P3W1 | 3.1, 3.2 | P3W1 master | Yes — 2 independent stories |
| P3W2 | 3.3, 3.4 | P3W2 master | Yes — 2 independent stories |
| P3W3 | 3.5 | Story 3.5 | Single story |
| P3W4 | 3.6, 3.7 | P3W4 master | Yes — 2 independent stories |
| P3W5 | 3.8 | Story 3.8 | Single story |
| P3W6 | 3.9 | Story 3.9 | Single story |

### Co-production Rule

Each story that delivers a deployable service (1.3 broker, 1.4 daemon) adds its own checks to `make verify-install` in the same PR. Story 3.7 then lifts those checks into the runbook, and IT-20 runs them from there.

---

### Phase 1: Foundation, Broker, Identity

**Goal:** A workspace with CI against real Mattermost, a broker that creates bot accounts and mints tokens, and a daemon that holds those tokens where agents cannot read them.

**Plan:** #26 (its own `/wavemachine` campaign, landing on `main` independently; D-089)

#### Phase 1 Definition of Done

- [ ] `make ci` passes locally and in CI on all three CI legs. [CT-01, CT-11]
- [ ] Every §5.N question has a recorded answer on Plan #3.
- [ ] IT-01, IT-03 and IT-17 pass. [R-01, R-02, R-03, R-05, R-06, R-08, R-30]
- [ ] `make verify-install` checks the broker and the daemon on a fresh host. [R-06, R-08]
- [ ] All Phase 1 unit tests pass.

---

#### Story 1.1: Workspace scaffold with CI against Mattermost (#7)

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
6. Create `scripts/ci/mattermost-up.sh`. It starts the image named by `MM_IMAGE` (`mattermost/mattermost-team-edition:11.11.3`, `mattermost/mattermost-enterprise-edition:11.11.3`, or `mattermost/mattermost-team-edition:11.8.x` with the latest 11.8 patch pinned in the workflow; CT-11), waits for `/api/v4/system/ping`, creates an admin, sets `EnableBotAccountCreation` and `EnableUserAccessTokens` and the dev-space rate limits, and exports `MM_URL` and `MM_ADMIN_TOKEN`.
7. Create `scripts/ci/two-user-setup.sh`. It creates the `crewchat`, `crewbroker`, `crewkiosk` (empty HOME) and `agent` users and the `crewchat-clients` group in the CI image, so tests can run as `agent` while services run as their own users.
8. Create `.github/workflows/ci.yml`, with each step one line calling `scripts/ci/*.sh`: a matrix over three `MM_IMAGE` legs (`mattermost-team-edition:11.11.3`, `mattermost-enterprise-edition:11.11.3`, and `mattermost-team-edition:11.8.x`, latest patch, pinned in the workflow), running `make ci`, then uploading `reports/`. On the 11.8 leg, tests that need native token expiry assert it is detected as absent rather than skipping silently (R-07, CT-11).
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
- [ ] CI runs the matrix over all three legs (Team Edition 11.11.3, Enterprise 11.11.3, Team Edition 11.8.x), and all three are green on the PR. [CT-01, CT-11]
- [ ] `reports/junit.xml` and `reports/coverage.xml` are uploaded as CI artifacts.
- [ ] `.github/workflows/ci.yml` has no `run:` step longer than one line; the logic is in `scripts/ci/`.
- [ ] `README.md` and `CHANGELOG.md` exist.

---

#### Story 1.2: Verification spike — §5.N and the kiosk mechanism (#8)

**Wave:** P1W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** None

Answers the open questions that could overturn a locked decision, before any code depends on them.

**Implementation Steps:**

1. Against the OaW dev space, or a local Team Edition container, answer §5.N questions 6 and 8 by experiment. Answer questions 1 and 5 from the vendor's docs, plus a check against a licensed image where one is available. For each question, record the command or source and its output in `docs/spikes/2026-10-spike-1.2.md`.
   - **Question 8 (bot `owner_id`):** create a bot account as the broker account, then read it as a second user. Assert `owner_id` is the broker's user ID. Attempt to change `owner_id` with the bot's own token, and record that it is refused. Then, as the owner (the broker account), reassign the bot with `POST /bots/{id}/assign/{user}` and record that it succeeds, which is why membership also requires `minted_bot_ids` (§5.9).
2. **Question 2 (one client per token):** write `spikes/two_clients.py`. It imports the upstream library, creates two client objects with two different bot tokens in one process, and posts as each. Record which author IDs the server reports. Run it against the pinned upstream, not whatever resolves: `uv run --with <upstream-dist>==<pin> spikes/two_clients.py` (D-090). `<pin>` is the version Story 1.1 locks in `uv.lock`; choose it here and record it in the spike doc, and if 1.1's PR is still open, use the version that PR pins, so that the two agree.
   - **Record the upstream's PyPI distribution name and the pinned version** in the spike doc (the Dev Spec names the repo, `cloud-ru-tech/mcp-server-mattermost`, but not its PyPI distribution name).
3. **Question 3 (`·p1`):** run the signature-parsing code from mcp-server-discord and the watcher, as of their current release tags, against a signature line ending in ` ·p1`, and record whether the name still parses.
4. **Question 4 (the kiosk mechanism):** time and test both options with a fixture brief.
   - (a) As the `crewkiosk` user, with an empty HOME and an empty working directory: `claude -p --restricted --strict-mcp-config --tools "" --disallowedTools "mcp__*"`, with the brief in the prompt. Ask it to run a shell command and record whether it ran. Then prove, **from the CLI's own transcript or debug output and not from our argv**, that no user, project or local settings, no hooks and no `CLAUDE.md` loaded. Plant a hook and a `CLAUDE.md` in the usual locations first, **and a hook in the managed-settings location**, so the proof is against something present; then confirm the kiosk host has no managed settings configured.
   - (b) a direct model API call with no tool definitions.
   Record latency, cost per question and the credentials each needs. **Name the kiosk's model credential: its path and its owner.** It must not be any chat or broker credential.
5. **Question 9 (11.8 compatibility):** start a local `mattermost-team-edition` 11.8 container at the latest 11.8 patch, and record the patch in the spike doc. If Story 1.1's PR is open, use the patch its workflow pins, so the spike and CI agree. Repeat question 8's `owner_id` and reassignment checks, apply the `system_user_access_token` role patch, create and revoke a token, and record that `expires_at` is detected as absent (R-07). Record anything that behaves differently from 11.11 as a §5.N answer with its ledger ID.
6. **Question 7 (remote broker transport):** write a one-page design note in the spike doc for a broker on another host, e.g. mutual-TLS TCP. It is not built in this Plan.
7. Post one `[ledger D-NNN]` comment per answer on Plan #3. Where an answer changes a locked decision, the entry carries `**Supersedes:**`.
8. Update §5.N of this Dev Spec with the answer and the ledger ID for each question.

**Test Procedures:**

*Unit Tests:* none. This story is docs and spike scripts only, so its CI gate is honest when empty (it runs in parallel with 1.1, before CI exists). The check that every §5.N item is answered, `test_spike_doc_answers_every_question`, lands in Story 1.3 (D-090).

*Integration/E2E Coverage:*

- None. This story produces evidence that the design rests on.

**Acceptance Criteria:**

- [ ] `docs/spikes/2026-10-spike-1.2.md` records a command or source, its output and an answer for every §5.N question. [CT-01]
- [ ] The kiosk mechanism is chosen, with measured latency and cost per question, and recorded as a ledger entry. [R-31]
- [ ] If the CLI runner is chosen, the spike doc shows from the CLI's own output that no user, project or local settings, hooks or `CLAUDE.md` loaded, with a planted hook (including in the managed-settings location) and `CLAUDE.md` present. [R-31, R-32]
- [ ] The kiosk's model credential is named, with its path and owner, and is not a chat or broker credential. [R-08, R-31]
- [ ] Bot `owner_id` behaviour is verified as §5.9's membership rule needs: set by the creator, visible to other users, not changeable by the bot, and reassignable by the owner. [R-22]
- [ ] On an 11.8 container, the `owner_id` and reassignment checks, the role patch, and token create/revoke are re-run and recorded, and the absence of `expires_at` is detected. [CT-11, R-07]
- [ ] Every answer has a `[ledger]` entry on Plan #3; those that change a decision supersede it.
- [ ] §5.N of the Dev Spec cites each answer's ledger ID.

---

#### Story 1.3: Broker — accounts, tokens, expiry, re-mint, prune (#9)

**Wave:** P1W2
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.1, 1.2

Delivers the broker, one per (crew, server), that creates bot accounts and mints, rotates and prunes their tokens, with no admin rights.

**Implementation Steps:**

1. `packages/broker/src/crewbroker/config.py`: `load_config(instance: str) -> CrewConfig` reads `/etc/crewbroker/<crew>-<server_id>.toml` (`instance` is `<crew>-<server_id>`) into a dataclass with these fields:
   - `crew`;
   - `server_id` and `server_url` (one Mattermost server per instance);
   - `broker_user_id`, filled at startup from `/users/me`;
   - `human_user_ids: list[str]`;
   - `trusted: list[str]`, the names of the crews this crew trusts. Names only: each trusted crew's `broker_user_id`, `human_user_ids` and `minted_bot_ids` come from that crew's own broker, which the daemon calls directly (§5.6). There is no hand-copied membership data, and trust across hosts is deferred (§5.N, item 7).

   If `crew` is absent from the file, it defaults to the username of the first human user ID, fetched from the server (R-02). Paths are per (crew, server) (§5.6), so several crews' brokers, and several servers per crew, can share a host.
2. `crewbroker/mint.py`:
   - `account_name(dev_name, crew) -> str` returns `f"{dev_name}.{crew}"`. It raises `ValueError` past 64 characters, or on characters outside `a-z0-9._-` (CT-05).
   - `ensure_bot(client, name) -> bot_user_id` looks up a bot account by username and creates it if absent (R-03). Every bot account it creates is added to `minted_bot_ids` in `/var/lib/crewbroker/<crew>-<server_id>/accounts.json`. If an account with that username already exists and its `owner_id` is **not** this broker, or it is not in `minted_bot_ids`, `ensure_bot` raises `SquattedAccount` and `mint` fails loudly; it never mints for a squatted name. If the existing account **is** this broker's and is on `minted_bot_ids` but was deactivated (pruned, R-30), `ensure_bot` **reactivates** it before returning, so a pruned agent can register again.
   - `mint_token(client, bot_user_id, ttl_hours=26) -> Token` creates a user access token. Where the server supports expiry, it sets the expiry, detected by a capability probe cached per server (R-07). It records the token's id and creation time in `/var/lib/crewbroker/<crew>-<server_id>/tokens.json`.
   - `record_register(account, now)` writes `last_register` for the account to `/var/lib/crewbroker/<crew>-<server_id>/accounts.json` (mode `0600`). Only `mint` calls it, and only the daemon's `register` causes a `mint`; `remint` never does.
3. `crewbroker/prune.py`: `prune_idle(client, now, idle_days=30)` deactivates bot accounts whose `last_register` is more than 30 days old (R-30). Rotation is the daemon's job (Story 1.4), so a re-minted but unregistered account still ages out.
4. `crewbroker/service.py`: a unix-socket server on `/run/crewbroker/<crew>-<server_id>.sock` (mode `0660`, group `crewchat`, set by the tmpfiles.d entry `d /run/crewbroker 2770 crewbroker crewchat` so the setgid directory passes the group on; `crewbroker` is not itself in `crewchat`), using the §5.4 frame shape. Every op is refused unless the peer's `SO_PEERCRED` UID is `crewchat` (R-08). **No broker ever calls another broker.** The handler never blocks on anything outside its own state and the Mattermost server. The ops are:
   - `mint{dev_name, crew, current_token_id, deadline}` → `{account, token, token_id}`;
   - `remint{dev_name, crew, current_token_id, deadline}` → `{token, token_id}`, without revoking the held token. Both revoke the account's other recorded tokens after minting, and refuse with `expired` past `deadline` (§5.6, #41);
   - `revoke{token_id}` → `{}`;
   - `crew_info{}` → `{broker_user_id, human_user_ids, trusted: ["<crew>", …], minted_bot_ids}`: this broker's own fields only, from its config and `accounts.json`. Trusted crews are returned by name; the daemon fetches their membership from their own brokers (§5.6, §5.9).

   A daily scheduler thread runs `prune_idle`.
5. `packaging/crewbroker@.service`: a systemd template unit, one instance per (crew, Mattermost server) (`%i` is `<crew>-<server_id>`), with `User=crewbroker`, `ProtectSystem=strict` and `StateDirectory=crewbroker/%i` with `StateDirectoryMode=0700` (so systemd creates the state directory unreadable by other users; every file written there is mode `0600`, including `tokens.json` and `accounts.json`) and `ReadWritePaths=/var/lib/crewbroker/%i /run/crewbroker`. Also `packaging/crewbroker.tmpfiles.conf` with `d /run/crewbroker 2770 crewbroker crewchat`, because the unit cannot start under `ProtectSystem=strict` without that directory.
6. `scripts/verify-install.sh`: add these checks:
   - each broker socket exists with the right mode and group;
   - a `mint` for `verify-canary` succeeds;
   - for the known files `/etc/crewbroker/<crew>-<server_id>.credential` and `/var/lib/crewbroker/<crew>-<server_id>/tokens.json`: `test -e <file> && ! sudo -u agent test -r <file>`. Never use a glob (R-08).
7. `scripts/ci/broker-role-patch.sh` (CI only): applies the `create_bot`, `read_bots` and `manage_bots` patch to `system_user_access_token`, and assigns that role to the broker account in the test server. On real servers this step is covered by CP-03 and is never automated against production.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_account_name_format_and_limits` | `<dev>.<crew>`; rejects over 64 characters and bad characters | `tests/unit/broker/test_mint.py` |
| `test_ensure_bot_creates_once` | A second call returns the same ID with no second create | `tests/unit/broker/test_mint.py` |
| `test_mint_sets_expiry_when_supported` | The capability probe drives whether expiry is set | `tests/unit/broker/test_mint.py` |
| `test_remint_keeps_old_token_until_revoke` | `remint` returns a new token and the old one still works until `revoke` | `tests/unit/broker/test_service.py` |
| `test_prune_idle_30_days_since_register` | With an injected clock: re-minted daily but no `register` for 31 days → deactivated; registered 29 days ago → kept | `tests/unit/broker/test_prune.py` |
| `test_every_op_refused_to_non_daemon_peer` | `SO_PEERCRED` ≠ the crewchat UID → every op errors, and no token is returned | `tests/unit/broker/test_service.py` |
| `test_trusted_config_parses` | `trusted = ["<crew>", …]` loads as a list of names; a table form, or any membership key (`broker_user_id`, `human_user_ids`, `minted_bot_ids`), is rejected | `tests/unit/broker/test_config.py` |
| `test_crew_info_lists_minted_bots` | `crew_info` returns the broker's user ID, the humans, the trusted crew names, and exactly the bot accounts `ensure_bot` created | `tests/unit/broker/test_service.py` |
| `test_crew_info_own_fields_only` | `crew_info` opens no outbound broker connection (asserted with a socket spy) and returns no other crew's membership data | `tests/unit/broker/test_service.py` |
| `test_pruned_agent_reactivated_on_register` | A deactivated, broker-owned account on `minted_bot_ids` is reactivated by `ensure_bot`, then `mint` succeeds | `tests/unit/broker/test_mint.py` |
| `test_ensure_bot_rejects_squatted_name` | An existing `<dev>.<crew>` not owned by this broker (or not in `minted_bot_ids`) → `SquattedAccount`, no token minted | `tests/unit/broker/test_mint.py` |
| `test_remint_does_not_record_register` | `remint` leaves `last_register` unchanged; `mint` updates it | `tests/unit/broker/test_mint.py` |
| `test_crew_defaults_to_partner_username` | `crew` is absent from the config → the partner's username | `tests/unit/broker/test_config.py` |
| `test_spike_doc_answers_every_question` | Every §5.N item has an "Answer:" line in `docs/spikes/2026-10-spike-1.2.md` (moved from Story 1.2, D-090) | `tests/unit/test_spike_doc.py` |

*Integration/E2E Coverage:*

- IT-03 is now runnable.
- IT-02 is partially runnable: the daemon's rotation arrives in 1.4, and the websocket reconnect in 2.1.
- IT-17 is partially runnable; the daemon-side naming arrives in 1.4.
- IT-01 is partially runnable; the daemon arrives in 1.4.

**Acceptance Criteria:**

- [ ] `ensure_bot` creates `<dev>.<crew>` on the first call only, against the CI Mattermost. [R-01, R-03]
- [ ] `mint_token` returns a working token, and sets native expiry when the server supports it. [R-05, R-07]
- [ ] The broker account holds the patched role and no system-admin role; a test asserts this through the API. [R-06]
- [ ] `crew_info` returns only this broker's own fields: `minted_bot_ids` that match the bot accounts it created, and the trusted crews by name; it never calls another broker. [R-22]
- [ ] A pruned agent's account is reactivated on its next `register`, and `mint` then succeeds, against the CI Mattermost. [R-03, R-30]
- [ ] `mint` for a squatted `<dev>.<crew>` (an existing account not owned by this broker) fails loudly and mints nothing. [R-01, R-03]
- [ ] IT-03 passes on all three CI legs. [R-30]
- [ ] `remint` and `revoke` work as separate steps, so the old token stays valid until the daemon revokes it. [R-05]
- [ ] Every broker op from a peer that isn't `crewchat` is refused. [R-08]
- [ ] `make verify-install` includes the broker checks, and they pass in CI. [R-06]
- [ ] `test_spike_doc_answers_every_question` passes against the merged spike doc from Story 1.2: every §5.N item, including the 11.8 compatibility item, has an "Answer:" line. [CT-01, CT-11]

---

#### Story 1.4: Daemon core — socket, protocol, register, token store (#10)

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
3. `crewchat/registry.py`: `register(dev_name, crew, session_id) -> Registration`. It keeps a registration per Dev-Name and accepts the declared name (D-055). It never reads a path from the agent's side (§5.3). A `register` for an agent that already holds tokens (the shim's daily re-register) runs the rotation sequence with `mint` in place of `remint`: `mint` (which records `last_register`), swap, `on_token_rotated`, `revoke` the previous token. No previous token is left live.
4. `crewchat/tokens.py`:
   - `BrokerClient(socket_path, timeout_s)`, one per (crew, server_id) on this host, from `[crews.<crew>.<server_id>] broker_socket`, with `mint(dev_name, crew)`, `remint(dev_name, crew)`, `revoke(token_id)` and `crew_info()`. Every call has a short timeout (`broker_timeout_s`, default 2 s); a timeout raises `BrokerUnavailable`, never a stall. `SwitchCache` then keeps that crew's last good `crew_info`; only with none cached, or when a cache-miss re-fetch times out, is the crew unresolvable (Mattermost `DROP`). The daemon uses these same clients to fetch each trusted crew's `crew_info` directly (§5.6);
   - `TokenStore` keeps tokens keyed by (dev_name, server_id) in memory, plus `/var/lib/crewchat/tokens/<dev>.json` (mode `0600`; directory `0700`; owner `crewchat`). It exposes `token_for(dev_name, server_id)`.
   - **Only `register` calls `mint`.** On a 401, `TokenStore` runs **the same four steps as rotation** for that agent and server: `remint`, swap the token in, call each adapter's `on_token_rotated(dev_name)` (the websocket reconnect, Story 2.1), then `revoke` the old `token_id`; it then retries the failed request once. A 401 never calls `mint`, so it never refreshes `last_register` (R-30), and never leaves the websocket on a dead token (§4.8).
   - After each `register`-driven `mint` by any local broker, the daemon refreshes **that broker's** cached `crew_info`, so the new bot account is in `minted_bot_ids` before its first post, whether the agent is a crewmate or a trusted crew's (§5.9).
   - `Rotator.run_daily(now)`: an agent with no `register` in 30 days has its websockets closed and is dropped from rotation. For each agent registered in the last 30 days, on each of its servers, it does four steps, paced at 5/s or less (R-05, CT-06):
     1. call `remint`;
     2. swap the token in the store;
     3. call each adapter's `on_token_rotated(dev_name)` hook (a no-op until Story 2.1 adds the websocket reconnect);
     4. call `revoke` on the old `token_id`.
5. `crewchat/__main__.py`: loads `/etc/crewchat/servers.toml` and starts the server. The file holds:
   - an array of `mattermost` tables with `id` and `url`;
   - a `discord` table with `enabled`;
   - `default_crew`;
   - a `[crews.<crew>] needs_list` per crew (the sqtk list `register` returns for R-11's headless squawk);
   - a `[crews.<crew>.<server_id>]` table per crew and Mattermost server, with `broker_socket` and `broker_user_id`. The `broker_user_id` entries are the configured broker list that IT-01 asserts R-06 against;
   - `broker_timeout_s` (default 2).
6. `packaging/crewchat.service`: a systemd unit with `User=crewchat`, `RuntimeDirectory=crewchat`, `StateDirectory=crewchat`, `StateDirectoryMode=0700` and `ReadWritePaths=/var/lib/crewchat /run/crewchat`, so systemd creates both directories (`/run` is emptied on every boot).
7. `scripts/verify-install.sh`: add checks that the daemon socket exists with mode `0660` and group `crewchat-clients`, and that `health` answers. For a canary agent's known token file `/var/lib/crewchat/tokens/verify-canary.json`, check `test -e <file> && ! sudo -u agent test -r <file>`. Never use a glob, because a shell that can't list the directory would make the check pass vacuously (R-08).

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_parse_frame_accepts_older_proto` | `proto` 0 or 1 is accepted; 2 → `unsupported` | `tests/unit/daemon/test_protocol.py` |
| `test_unknown_op_unsupported` | An unknown op returns `ok:false` | `tests/unit/daemon/test_server.py` |
| `test_register_returns_account_name` | `register` → `<dev>.<crew>` | `tests/unit/daemon/test_registry.py` |
| `test_token_store_mode_0700` | The store directory and files are not readable by others | `tests/unit/daemon/test_tokens.py` |
| `test_token_refresh_on_401` | A 401 calls `remint` (never `mint`), swaps, calls `on_token_rotated` (asserted: the reconnect hook fires for that agent), revokes the old token, then retries once | `tests/unit/daemon/test_tokens.py` |
| `test_crew_info_refreshed_after_mint` | A `register`-driven `mint` triggers a `crew_info` refresh for that broker | `tests/unit/daemon/test_tokens.py` |
| `test_reregister_revokes_previous_token` | A second `register` for the same agent mints, swaps, reconnects and revokes the previous token | `tests/unit/daemon/test_registry.py` |
| `test_only_register_calls_mint` | No code path other than `register` calls `BrokerClient.mint` | `tests/unit/daemon/test_tokens.py` |
| `test_unregistered_30_days_closes_websocket` | An agent with no `register` in 30 days has its websockets closed and is dropped from rotation | `tests/unit/daemon/test_tokens.py` |
| `test_register_returns_needs_list` | `register` returns the crew's `needs_list` from `servers.toml` | `tests/unit/daemon/test_registry.py` |
| `test_rotation_order` | remint → swap → `on_token_rotated` → revoke, in that order; revoke is never called before the swap | `tests/unit/daemon/test_tokens.py` |
| `test_rotation_skips_unregistered_30_days` | Agents not registered in 30 days aren't rotated | `tests/unit/daemon/test_tokens.py` |
| `test_broker_client_per_crew_and_server` | Two crews and two servers → one broker socket per (crew, server_id) from `servers.toml`; `token_for` keys by server | `tests/unit/daemon/test_tokens.py` |
| `test_broker_call_timeout` | A broker that never answers → `BrokerUnavailable` within `broker_timeout_s`; other brokers' calls are unaffected | `tests/unit/daemon/test_tokens.py` |

*Integration/E2E Coverage:*

- IT-01 and IT-17 are now runnable.
- IT-02 is partially runnable; the websocket reconnect arrives in 2.1.

**Acceptance Criteria:**

- [ ] IT-01 passes on all three CI legs [R-01, R-03, R-05, R-06, R-08]:
  - the daemon runs as `crewchat`;
  - for every known credential file, `test -e` succeeds and `sudo -u agent test -r` fails;
  - no socket op returns a token to a non-daemon peer;
  - the minted token works;
  - the patched role is held only by the configured broker accounts, asserted against the `broker_user_id` entries in `servers.toml`.
- [ ] IT-17 passes. [R-01, R-02]
- [ ] A frame with `proto: 2` gets `unsupported`; a frame with `proto: 0` is handled. [CP-05, D-056]
- [ ] Rotation calls remint, swap, the reconnect hook and revoke in that order. [R-05]
- [ ] A 401 refresh runs remint, swap, the reconnect hook and revoke, never `mint`, and the test asserts the reconnect hook fires; an agent unregistered for 30 days is closed and not rotated. [R-05, R-30]
- [ ] `make verify-install` includes the daemon checks, and they pass in CI. [R-08]

---

### Phase 2: Chat Contract and Adapters

**Goal:** One chat contract that sends, reads and replies across N Mattermost servers and Discord, with the agreed addressing rules, exposed to agents as `chat_*` tools.

**Plan:** #27 (its own `/wavemachine` campaign; IP-2 follows its landing; D-089)

#### Phase 2 Definition of Done

- [ ] IT-02, IT-04 to IT-07, IT-14 to IT-16, IT-24 and IT-25, pass on all three CI legs. [R-04, R-05, R-07, R-09 to R-19, R-29]
- [ ] IT-15 covers today's `disc_*` behaviours, through `chat_*` (D-108). [R-15]
- [ ] A `mattermost-crew` release is tagged, ready for IP-2 (K-1).
- [ ] All Phase 2 unit tests pass.

---

#### Story 2.1: Mattermost adapter (#11)

**Wave:** P2W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.4

Implements send, read, thread, DM and react against Mattermost through the upstream library, plus the protocol marker and echo filtering by user ID.

**Implementation Steps:**

1. `crewchat/adapters/base.py`: a `ChatBackend` protocol with `send`, `read`, `thread`, `dm`, `react` and `stream() -> AsyncIterator[InboundMessage]`, and an `InboundMessage` dataclass with `backend`, `channel`, `post_id`, `root_id`, `author_id`, `author_name`, `text`, `mentions: list[str]`, `proto` and `ts`.
2. `crewchat/adapters/mattermost.py`: `MattermostBackend(server_id, url, token_for)`, where `token_for(dev_name, server_id) -> str` returns that agent's token for this server. One instance per configured Mattermost server (R-10).
   - It creates one upstream client per Dev-Name.
   - `send` sets `props={"crew_proto": PROTO}` (R-14).
   - `read` and `thread` map posts to `InboundMessage`, taking `proto` from `props.crew_proto`, defaulting to 0.
3. `stream()` opens `/api/v4/websocket` per agent, pings every 30 s and reconnects with exponential backoff, capped at 60 s (CT-07). It emits a `posted` event as an `InboundMessage`, with `mentions` taken from the event's `mentions` field (R-29).
4. Echo filter: drop any `InboundMessage` whose `author_id` equals the receiving agent's own user ID (R-04).
5. `on_token_rotated(dev_name)`: close that agent's websocket and reopen it with the new token from `token_for`. Reconnecting is required, because revoking a token doesn't close an open websocket; it only stops events (§4.8). Replay from the cursor covers the gap (R-28).
6. `InboundMessage` gains `owner_id` (the author's bot `owner_id`, or `None` for a human) and `kiosk` (true when `props.crew_kiosk == 1`), both read from the post and user objects for §5.9's membership rule.

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
- IT-02 is now fully runnable: rotation reconnects the websocket, and events keep arriving.
- IT-06 (the Mattermost half) and IT-16 are partially runnable.

**Acceptance Criteria:**

- [ ] IT-14 passes on all three CI legs. [R-04, R-29]
- [ ] Posts made through the adapter carry `props.crew_proto = 1`, and older posts parse with `proto` 0. [R-14]
- [ ] Two agents post from one daemon process as two distinct authors (two client objects). [R-01]
- [ ] Websocket reconnect after a forced close is covered by a unit test. [CT-07]
- [ ] IT-02 passes on all three CI legs: across a daemon-driven rotation, the agent's websocket reconnects with the new token and no event is lost. [R-05, R-07]

---

#### Story 2.2: Discord adapter (#12)

**Wave:** P2W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.4

Gives the daemon Discord parity: all Discord REST goes through scream-hole (D-106), and the protocol marker is added to the signature.

**Implementation Steps:**

1. `crewchat/adapters/discord.py`: `DiscordBackend(screamhole_url, credential_path)`.
   - Every call, reads and writes alike, uses the base `<screamhole_url>/api/v10`, as today's `mcp-server-discord` does (`resolveApiBase`); scream-hole proxies every v10 path (D-106, Story 2.4c). `read`, `thread` and `stream` use its read API, as mcp-server-discord-watcher's poll core uses it today.
   - `send`, `dm` and `react` carry the credential at `credential_path`, owned by `crewchat` and mode `0600`.
   - **While scream-hole is unhealthy (D-106):** reads never go to `discord.com`; they answer `unavailable: scream-hole unhealthy`, and `health` reports `discord` degraded. Writes alone fall back to `https://discord.com/api/v10`. Health is re-checked on a backoff (1 s doubling, capped at 60 s), and every call returns to scream-hole when it recovers.
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

- [ ] Every `disc_*` tool in today's kit (`disc_send`, `disc_read`, `disc_list`, `disc_resolve`, `disc_create_channel`, `disc_create_thread`) has a working `DiscordBackend` equivalent, each covered by a unit test (the last four, and `send`'s attachments and embed, by Story 2.4c). [R-15]
- [ ] Sends carry `·p1`, and messages without it parse as `proto` 0. [R-14]
- [ ] No code path calls Discord's read endpoints directly, healthy or not; reads go through scream-hole, and writes fall back only while it is unhealthy. [D-013, D-106]

---

#### Story 2.3: Addressing (#13)

**Wave:** P2W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.4

Implements the addressing rules every adapter inherits: `@agents`, `@all` during the transition, `@name.crew`, crew and team tokens, and "reply to me".

**Implementation Steps:**

1. `crewchat/addressing.py`:
   - `tokens(text) -> set[str]` extracts `@`-tokens and keeps `.` inside them (R-18);
   - `is_broadcast(tokens, backend) -> bool` is true for `@agents` on every backend, and for `@all` only on Discord (R-16).
2. `addressed_to(msg, reg: Registration, my_threads: set[str], author_crew: str | None = None) -> bool` is true when any of these holds:
   - a broadcast;
   - `@<dev-name>` or `@<dev-name>.<crew>`;
   - `@<crew>`, or `@<dev-team>` where `team_scope` allows it, matched by our filter and never by Mattermost user groups (R-17). `group_tokens(reg, backend, author_crew)` always includes the crew token; it includes `reg.dev_team` only as the per-backend policy `team_scope(backend)` says (D-103):

     | `team_scope(backend)` | Backends | `@<dev-team>` matches when |
     |---|---|---|
     | `"backend"` | Discord (`discord`, `discord:*`) | `reg.dev_team` is non-empty; any author (the guild is the trust boundary) |
     | `"crew"` | every Mattermost server | `reg.dev_team` is non-empty **and** `author_crew == reg.crew`; with `author_crew` None it never matches (fail closed) |

     Phase 2 has no membership step, so every Phase 2 caller passes `author_crew=None`; Story 3.3 supplies it. The agent's avatar is display only and never addresses it;
   - `msg.root_id` is in `my_threads`, the threads the agent started or posted in (R-19);
   - the message is a direct message to the agent (a DM channel whose members are the sender and the agent) (R-19).

   `is_direct_address(msg, reg, my_threads) -> bool` is the narrower test used for kiosk routing (§5.9): true only for `@<dev-name>`, `@<dev-name>.<crew>`, a DM to the agent, or a reply in one of the agent's own threads. Broadcasts and crew or team tokens are never direct address.
3. `crewchat/threads.py`: a `MyThreads` store that records the root ID of every post the agent makes, and that is consulted by `addressed_to`. Story 2.4b persists it at `<state_dir>/threads/<dev>.json` (`/var/lib/crewchat/threads/`, file `0600`, directory `0700`), with each root's `last_seen`, pruned by `my_threads_retention_days` (default 90), so "reply to me" survives a daemon restart (D-104).

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_agents_broadcast_both_backends` | `@agents` is a broadcast on Mattermost and Discord | `tests/unit/daemon/test_addressing.py` |
| `test_all_only_on_discord` | `@all` counts only on Discord | `tests/unit/daemon/test_addressing.py` |
| `test_dot_kept_in_name_crew` | `@kessel.oaw` stays whole | `tests/unit/daemon/test_addressing.py` |
| `test_crew_and_team_tokens` | Matched by our filter | `tests/unit/daemon/test_addressing.py` |
| `test_reply_to_me_by_thread_root` | A post in my thread is addressed to me | `tests/unit/daemon/test_addressing.py` |
| `test_dm_is_addressed` | A plain DM to the agent, with no mention and outside any thread of mine, is addressed to me | `tests/unit/daemon/test_addressing.py` |
| `test_direct_address_excludes_broadcast_and_crew_tokens` | `is_direct_address` is true for `@<dev>`, `@<dev>.<crew>`, a DM and a reply in my thread; false for `@agents`, `@all`, `@<crew>` and `@<dev-team>` | `tests/unit/daemon/test_addressing.py` |

*Integration/E2E Coverage:*

- IT-07 is now runnable.

**Acceptance Criteria:**

- [ ] IT-07 passes on all three CI legs. [R-16, R-17, R-18, R-19]
- [ ] No code path calls the Mattermost user-groups API (checked by grep in CI). [R-17, CT-01]

---

#### Story 2.4: Thin MCP — chat_*, default resolution, disc_* aliases (#14)

**Wave:** P2W2
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 2.1, 2.2, 2.3

Exposes the chat contract to agents as `chat_*` tools through a thin MCP shim with no credentials, resolves the default backend, and keeps the `disc_*` names as deprecated aliases.

**Implementation Steps:**

1. `packages/clients/src/crewchat_clients/mcp_shim.py`: a stdio MCP server built with the `mcp` SDK. It holds no credentials.
   - It defines the §5.5 tools. `chat_send`, `chat_read`, `chat_thread`, `chat_dm`, `chat_react` and `chat_health` each forward one frame to `/run/crewchat/crewchat.sock`. `chat_set_default(key, backend)` is the one local-only tool: it writes `~/.claude/chat-defaults.json` and sends nothing to the daemon.
   - On start it sends `register` with the Dev-Name and `crew` from `<repo_root>/.claude/agent-identity.json`; `crew` is omitted when the file has none. The result includes the crew's `needs_list` (§5.4), which the daemon takes from `[crews.<crew>] needs_list` in `servers.toml`.
   - It **re-registers** every 24 hours on a timer, and before any call if its last `register` is older than 24 h. The shim lives as long as the session, so a session that stays up for weeks never ages out under R-30.
   - The same timer sends `ping{dev_name}` every 15 s (#129). A `not_registered` answer means the daemon restarted and lost the registration: the shim registers again and pushes its files, with no tool call, so the agent's doorbell can arm. A daemon that answers `unsupported` is not asked again; one that is down is asked again on the next tick.
   - It runs as the agent's user, so it reads the agent's files; the daemon never does (§5.3).
     - It pushes `kiosk_brief{dev_name, text}`, where `text` is `crewchat_clients.brief.extract_brief` of `<repo_root>/.claude/reseed-revive.md`, at start and whenever that file's mtime changes.
     - It pushes `squawk_ids{dev_name, ids}` from `<repo_root>/.claude/sqtk-mine.json` at start and on change.
2. `packages/clients/src/crewchat_clients/defaults.py`: `resolve_default(identity, repo_root) -> Backend | NeedsHuman`, run in the shim as the agent's user, following R-11's order:
   1. the identity file's `home_backend`;
   2. `~/.claude/chat-defaults.json`, keyed by `<forge-host>/<org>` from `git remote get-url origin`, or by `repo_root` when there is no remote;
   3. otherwise, `NeedsHuman`.
3. In the shim, `NeedsHuman`:
   - **interactive:** the tool returns a structured question for the agent to ask its human. A follow-up tool `chat_set_default(key, backend)` writes `chat-defaults.json` atomically.
   - **headless** (`CLAUDE_CODE_ENTRYPOINT` is not interactive, or `--print`): the send fails with a loud error, and the shim runs `sqtk add <needs-list> "<question>"`, where `<needs-list>` is the crew's human partner's needs list, returned by `register` from `/etc/crewchat/servers.toml` (`bj:needs` for crews partnered with BJ) (R-11, CP-02). The question is the shim's own text, never message content.
4. A backend named in the call is used, and the daemon logs `off-default: <dev> <backend>` (R-13). The shim always sends the resolved `backend` in the `send` and `dm` frames; the daemon does no resolution.
5. Aliases: `disc_send` maps to `chat_send(backend="discord")`, and likewise for each `disc_*` tool. Each alias logs one deprecation warning per session. *(Superseded by D-108 (v0.2.2): the aliases are removed; see §5.5.)*
6. `chat_send` with `thread` replies on that thread's backend, whatever the default is (R-12).

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_resolution_order` | identity file > defaults file > NeedsHuman | `tests/unit/clients/test_defaults.py` |
| `test_forge_key_from_remote` | The SSH and HTTPS remote forms give `<host>/<org>` | `tests/unit/clients/test_defaults.py` |
| `test_extract_brief_only_kiosk_section` | Only the `## Kiosk` section is returned; other sections are excluded | `tests/unit/clients/test_brief.py` |
| `test_pushes_brief_on_mtime_change` | A rewritten seed → one new `kiosk_brief` frame, carrying only the section | `tests/unit/clients/test_mcp_shim.py` |
| `test_pushes_squawk_ids` | `sqtk-mine.json` ids are pushed at start and on change | `tests/unit/clients/test_mcp_shim.py` |
| `test_headless_fails_loud_and_squawks` | Headless + no default → an error and a squawk | `tests/unit/clients/test_mcp_shim.py` |
| `test_set_default_atomic_write` | `chat_set_default(key, backend)` writes atomically, and sends no frame to the daemon | `tests/unit/clients/test_mcp_shim.py` |
| `test_reply_follows_thread_backend` | A reply goes to the thread's backend | `tests/unit/clients/test_mcp_shim.py` |
| `test_guild_tools_forward_discord_ops` | Each guild tool forwards its daemon op with `backend=discord` and today's args, and `chat_read(post_id)` reads one message (D-108; was `test_disc_alias_maps_and_warns`) | `tests/unit/clients/test_mcp_shim.py` |
| `test_shim_holds_no_credentials` | The shim's environment and files contain no token | `tests/unit/clients/test_mcp_shim.py` |
| `test_shim_reregisters_daily` | With an injected clock, the shim sends `register` again after 24 h, and before a call when its last `register` is older than 24 h | `tests/unit/clients/test_mcp_shim.py` |

*Integration/E2E Coverage:*

- IT-04, IT-05 and IT-16 are now runnable.
- IT-15 is now fully runnable.

**Acceptance Criteria:**

- [ ] IT-04, IT-05, IT-15 and IT-16 pass on all three CI legs. [R-09, R-10, R-11, R-12, R-13, R-15]
- [ ] A headless send with no default fails, and leaves a squawk on the crew's configured needs list; there is no Discord fallback. [R-11]
- [ ] `grep -rn "token" packages/clients/` finds no credential handling. [R-08]
- [ ] Every `disc_*` alias forwards and warns. [R-15] *(Superseded by D-108 (v0.2.2).)*
- [ ] No daemon module opens a path under an agent's home or repo; a test asserts the daemon package has no reference to `agent-identity.json`, `chat-defaults.json`, `reseed-revive.md` or `sqtk-mine.json`. [R-11, R-32]

---

#### Story 2.4b: register carries team and avatar; persisted my-threads (#49)

**Wave:** P2W3
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 2.2, 2.3, 2.4
**Ledger:** D-103, D-104

Carries the agent's Dev-Team and avatar through `register`, scopes `@<dev-team>` per backend, and makes "replies on my threads" survive a daemon restart, by recording every send into a persisted thread set. Added by D-103 and D-104 (from `mattermost-crew:triage` #5 and #6, found by the P2W1 flights); not folded into 2.4, which was already in flight.

**Implementation Steps:**

1. **`register` carries `dev_team` and `dev_avatar` (D-103).** The `register` handler accepts both as optional strings (anything else is `bad_request`), default `""`, and passes them to `Registry.register(dev_name, crew, session_id, *, dev_team="", dev_avatar="")`, which stores both on `Registration` (a re-register overwrites both; `result()` is unchanged). With Discord enabled, a successful `register` calls `DiscordBackend.set_identity(dev_name, avatar=dev_avatar, team=dev_team)`. The shim's `register` frame (and its daily re-register) reads `dev_team` and `dev_avatar` from `<repo_root>/.claude/agent-identity.json`, then the legacy `/tmp/claude-agent-<md5(repo_root)>.json`, then `""` (§5.3).
2. **Team addressing is a per-backend policy (D-103).** `addressing.py` adds `TeamScope = Literal["crew", "backend"]` and `team_scope(backend)`, and `group_tokens(reg, backend, author_crew)` and `addressed_to(..., author_crew=None)` follow the table in Story 2.3. Phase 2 callers pass `author_crew=None`.
3. **Every daemon-side send records into `MyThreads` (D-104).** `send` and `dm` record after the backend accepts the post; a failed send records nothing. `crewchat serve` builds one persisted `MyThreads.load(state_dir, my_threads_retention_days)` and hands that instance to the chat ops.
4. **`MyThreads` is persisted (D-104).** `<state_dir>/threads/<dev>.json`, `{"roots": {"<root_id>": "<iso8601>"}}` (Story 2.4c extends each entry to `{"backend", "last_seen"}`), file `0600`, directory `0700`, written atomically after each `add`; each root's `last_seen` is refreshed on `add`; roots older than `retention_days` are pruned on load and on save, then the `limit` cap applies; `forget` deletes the file; a corrupt file is logged once and read as empty (§5.9).
5. **`my_threads_retention_days` in `servers.toml` (D-104):** top-level, an integer ≥ 1, default 90. It never reads a server's retention policy (CT-01).
6. **Dev Spec amendments** in the same PR: §4.2, §5.3, §5.4, Story 2.3, Story 3.3, §5.9, the `servers.toml` key lists, IT-24 and the VRTM, and this block.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_register_accepts_team_and_avatar` | `register` stores `dev_team`/`dev_avatar`; non-strings give `bad_request`; absent gives `""` | `tests/unit/daemon/test_server.py` |
| `test_register_sets_discord_identity` | `register` calls `DiscordBackend.set_identity`, and the next signature carries the avatar and team | `tests/unit/daemon/test_registry.py` |
| `test_shim_register_reads_identity` | The shim's `register` frame takes `dev_team`/`dev_avatar` from `agent-identity.json`, then the legacy `/tmp` file, then `""` | `tests/unit/clients/test_mcp_shim.py` |
| `test_team_scope_policy` | `discord` and `discord:x` are `"backend"`; a Mattermost server is `"crew"` | `tests/unit/daemon/test_addressing.py` |
| `test_team_token_discord_no_author_check` | On Discord, `@<dev-team>` is addressed with `author_crew=None` | `tests/unit/daemon/test_addressing.py` |
| `test_team_token_mattermost_fails_closed` | On Mattermost, `author_crew=None` or another crew → not addressed; the agent's own crew → addressed | `tests/unit/daemon/test_addressing.py` |
| `test_avatar_never_addresses` | A message naming the agent's avatar is not addressed to it | `tests/unit/daemon/test_addressing.py` |
| `test_send_records_thread` | A successful `send` (new post, then reply) records its root; a failed send records nothing | `tests/unit/daemon/test_server.py` |
| `test_threads_persist_roundtrip` | `add`, then a new `MyThreads.load` gives the same roots; file `0600`, directory `0700` | `tests/unit/daemon/test_threads.py` |
| `test_threads_prune_by_retention` | With an injected clock, roots past `retention_days` are dropped on load and on save | `tests/unit/daemon/test_threads.py` |
| `test_threads_corrupt_file_is_empty` | A corrupt file loads as empty, logs once, and doesn't raise | `tests/unit/daemon/test_threads.py` |
| `test_threads_forget_deletes_file` | `forget` removes the agent's file | `tests/unit/daemon/test_threads.py` |
| `test_config_retention_default` | `my_threads_retention_days` defaults to 90, is top-level, and rejects values below 1 | `tests/unit/daemon/test_config.py` |

*Integration/E2E Coverage:*

- IT-24 is now runnable (`tests/integration/test_my_threads_restart.py`).
- IT-07 still passes with the new `addressed_to` signature.

**Acceptance Criteria:**

- [ ] IT-24 and IT-07 pass on all three CI legs. [R-17, R-19]
- [ ] `register` carries `dev_team` and `dev_avatar` end to end: shim → daemon → `Registration`, and into `DiscordBackend.set_identity`. [D-103]
- [ ] `team_scope` is the only place a backend's team trust is decided. [D-103]
- [ ] On Mattermost a team token never matches with `author_crew=None`; on Discord it matches on `reg.dev_team` alone. [D-103]
- [ ] Every successful daemon-side `send` and `dm` records into `MyThreads`, persisted at `<state_dir>/threads/<dev>.json` (`0600`, directory `0700`), pruned by `my_threads_retention_days`. [D-104]
- [ ] The daemon package still has no reference to `agent-identity.json` or the legacy `/tmp/claude-agent-` file. [R-11, R-32, §5.3]

---

#### Story 2.4c: R-15 parity: every `disc_*` alias, attachments, embeds, one backend name (#51)

**Wave:** P2W4
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 2.2, 2.4, 2.4b
**Ledger:** D-105, D-106

Closes R-15 for Phase 2: every `disc_*` tool in today's kit gets a working alias, `attach_path` and `embed` survive the move to `chat_send`, backends get one name form on both sides of the socket, and the shim asks the daemon which backend a thread is on instead of remembering it. Found by the P2W2 flight and ruled by babelfish (D-105, D-106).

**Implementation Steps:**

1. **Aliases for every kit tool** (`crewchat_clients/mcp_shim.py`): all nine `disc_*` aliases take today's argument names and shapes exactly (§5.5), map to `backend="discord"`, and log one deprecation line per session. Channel names (`agent-ops`, `#agent-ops`) resolve against the guild's text channels from scream-hole; an unknown name is `bad_request` naming it. A `guild_id` other than `[discord] guild_id` is `bad_request`, with no call. *(Superseded by D-108 (v0.2.2): the aliases are removed, and the four guild ops are `chat_*` tools; see §5.5.)*
2. **New daemon ops** (`crewchat/chat.py`, §5.4): `list_channels`, `resolve`, `create_channel`, `create_thread`, and `read`'s `post_id`. `DiscordBackend` implements them; Mattermost answers `unsupported`. All Discord REST goes through `<screamhole_url>/api/v10`; while scream-hole is unhealthy, reads answer `unavailable: scream-hole unhealthy` and never reach `discord.com`, writes alone fall back, and health is re-checked on a backoff capped at 60 s (D-106, Story 2.2).
3. **`attach_path`** (`crewchat/uploads.py`): the shim reads the file as the agent's user and streams it in `upload` frames that each fit `MAX_FRAME_BYTES`. The daemon holds it under `/run/crewchat/uploads/` (`0600`, directory `0700`), at most 8 MiB (`too_large`), deleted after use or after 10 minutes. `send`/`dm` take `attachments`: multipart on Discord, `POST /api/v4/files` with the agent's token and `file_ids` on Mattermost (CT-01).
4. **`embed`**: `"title:body"`, split on the first colon. A rich embed `{title, description}` on Discord, and `props.attachments: [{title, text}]` on Mattermost. A malformed embed is `bad_request`; neither it nor an attachment is ever silently dropped.
5. **One backend-name form** (§5.4): `mattermost:<server_id>` and `discord` everywhere, on the socket and in files; the bare-id translation is gone from the shim and the daemon, and a bare id on the socket is `bad_request`.
6. **`thread_backend`** (R-12): the shim's thread-to-backend map is deleted. The daemon answers by ID shape, then the persisted thread set (each entry now `{"backend", "last_seen"}`, §5.9), then a `GET /api/v4/posts/{id}` probe on only the agent's registered Mattermost servers. `chat_send` with `thread`, `chat_thread` and `chat_react` use it when no backend is named; `not_found` fails loudly.
7. **Dev Spec amendments** in the same PR: §5.4, §5.5, §5.9, Story 2.2, Story 2.4b, IT-15, IT-25, the VRTM's R-15 row, the DoD's IT range, the wave map and this block.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_guild_tools_take_todays_args` | The four guild tools take today's `disc_*` arg names and defaults, and the MCP server offers each with them (D-108; was `test_every_kit_disc_tool_has_alias`) | `tests/unit/clients/test_mcp_shim.py` |
| `test_chat_send_to_discord_carries_attach_and_embed` | `chat_send(attach_path, embed)` on Discord produces an `upload` stream and a `send` with `attachments` and `embed` (D-108; was `test_disc_send_carries_attach_and_embed`) | `tests/unit/clients/test_mcp_shim.py` |
| `test_upload_chunks_fit_frame` | Every `upload` frame is under `MAX_FRAME_BYTES`; a file over 8 MiB fails loudly and sends no `send` | `tests/unit/clients/test_mcp_shim.py` |
| `test_upload_store_mode_and_expiry` | The upload file is `0600`, deleted after use, and expired after 10 minutes (injected clock) | `tests/unit/daemon/test_chat.py` |
| `test_embed_mattermost_attachment` | On Mattermost, `embed` becomes `props.attachments[0] = {title, text}`; a malformed embed is `bad_request` | `tests/unit/daemon/test_adapter_mattermost.py` |
| `test_channel_name_resolution` | `agent-ops` and `#agent-ops` resolve to the ID; an unknown name is `bad_request` | `tests/unit/daemon/test_adapter_discord.py` |
| `test_foreign_guild_refused` | Another `guild_id` is `bad_request`, with no HTTP call | `tests/unit/daemon/test_adapter_discord.py` |
| `test_discord_reads_only_screamhole` | Every read targets scream-hole only; while it is unhealthy each read is `unavailable` with no request to Discord, a write falls back, and after recovery both go to scream-hole again | `tests/unit/daemon/test_adapter_discord.py` |
| `test_backend_name_single_form` | `register` and `health` answer `mattermost:<id>`; a bare id on a chat frame is `bad_request` | `tests/unit/daemon/test_server.py` |
| `test_thread_backend_order` | Shape first, then the thread set, then a probe of only the agent's registered servers; an unknown shape is `bad_request`; `not_found` fails loudly | `tests/unit/daemon/test_chat.py` |
| `test_reply_backend_survives_shim_restart` | A new shim instance replies on an old thread on that thread's backend, through `thread_backend` | `tests/unit/clients/test_mcp_shim.py` |

*Integration/E2E Coverage:*

- IT-15, extended to each of the nine aliases. *(Superseded by D-108 (v0.2.2): IT-15 runs through `chat_*`.)*
- IT-25 is new and runnable (`tests/integration/test_chat_contract.py`).
- IT-05, extended: `chat_send` with `attach_path` and `embed` on Mattermost.
- IT-04, IT-05 and IT-16 still pass with the single backend-name form.

**Acceptance Criteria:**

- [ ] IT-04, IT-05, IT-15 and IT-16 pass on all three CI legs. [R-09, R-12, R-15]
- [ ] Every `disc_*` tool in today's kit (`send`, `read`, `list`, `resolve`, `create_channel`, `create_thread`) has an alias taking today's args, covered by IT-15. [R-15] *(Superseded by D-108 (v0.2.2).)*
- [ ] `attach_path` and `embed` are delivered on Discord and on Mattermost, or refused loudly; neither is ever silently dropped. [R-15]
- [ ] No Discord read ever reaches `discord.com`, healthy or not; IT-25 passes on all three CI legs. Writes fall back only while scream-hole is unhealthy. [D-106]
- [ ] One backend-name form on the socket and in every file; no bare-id translation is left in the shim or the daemon. [§5.4]
- [ ] The shim keeps no thread-to-backend map; `chat_send` on a thread uses `thread_backend`. [R-12]

---

### Phase 3: Inbound Defense, Doorbell, Kiosk

**Goal:** One merged doorbell over three backends, gated by the crew's social switch and the loop guard, with outsiders answered by a tool-less kiosk, and the operator and partner docs that make it runnable.

**Plan:** #28 (its own `/wavemachine` campaign; IP-3 follows its landing; D-089)

#### Phase 3 Definition of Done

- [ ] IT-08 to IT-13 and IT-18 to IT-23 pass on all three CI legs. [R-20 to R-28, R-31 to R-34]
- [ ] E2E-01 to E2E-03 pass in CI. [R-01, R-09, R-12, R-16, R-20, R-21, R-23, R-25, R-26, R-31, R-33]
- [ ] DM-09a, DM-09c and DM-10 are delivered.
- [ ] The dev-space smoke run is posted to Plan #3.
- [ ] A `mattermost-crew` release is tagged, ready for IP-3 (K-2).
- [ ] All Phase 3 unit tests pass.

---

#### Story 3.1: Broker — social switch and kiosk control (#15)

**Wave:** P3W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 1.3

The broker holds each crew's social switch and kiosk flag, and changes them only on DMs from the crew's human partner.

**Implementation Steps:**

1. `crewbroker/switch.py`: a `SwitchState` dataclass with `mode` (`crew` | `trusted` | `social`), `expires_at: datetime | None`, `revert_to` and `kiosk_on: bool = True`, persisted to `/var/lib/crewbroker/<crew>-<server_id>/switch.json`. The default is `mode="crew"`, `kiosk_on=True` (R-20, R-34). Invariant: `revert_to` is never `social`.
2. `apply_command(state, sender_user_id, text, cfg, now) -> (SwitchState, reply)`:
   - It accepts `social <dur>`, `trusted`, `crew`, `kiosk off` and `kiosk on`.
   - It rejects any sender not in `cfg.human_user_ids`, and the state stays unchanged (R-24).
   - `social <dur>`: first it applies any pending lapse (step 3). Then, if social is already in effect, it **extends** the expiry and keeps the existing `revert_to`. Otherwise it sets `revert_to` to the current effective mode, which by the invariant is `crew` or `trusted`.
3. `effective_mode(state, now) -> (mode, lapsed: bool)`: past the expiry it returns `revert_to` (or `crew` if that is unreadable or is somehow `social`) and `lapsed=True` (R-23). `settle(state, now) -> SwitchState` **persists** a lapse: it writes `mode = revert_to`, clears `expires_at`, and is called before every command and on every `switch_state` request, so a lapsed social never lingers in storage.
4. The broker's websocket listener in `crewbroker/service.py`: on a DM to the broker account, call `apply_command` and post the reply. When `settle` records a lapse, DM the human partner "social lapsed → <mode>".
5. A socket op `switch_state{}` returns the stored state plus `effective_mode`.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_default_crew_only_kiosk_on` | Defaults | `tests/unit/broker/test_switch.py` |
| `test_only_partner_can_flip` | A non-partner's command → refused, state unchanged | `tests/unit/broker/test_switch.py` |
| `test_social_lapses_and_reverts` | After expiry → `revert_to`; `lapsed` is reported | `tests/unit/broker/test_switch.py` |
| `test_unreadable_revert_falls_back_crew` | A corrupt `revert_to` → `crew` | `tests/unit/broker/test_switch.py` |
| `test_social_extend_keeps_revert` | `trusted` → `social 1h` → `social 2h`: `revert_to` stays `trusted`, and the expiry moves | `tests/unit/broker/test_switch.py` |
| `test_reengage_after_lapse` | Social lapses, then `social 1h` again: `revert_to` is the lapsed-to mode, never `social`, and the second lapse reverts | `tests/unit/broker/test_switch.py` |
| `test_lapse_persisted` | After `settle`, the stored `mode` is the reverted mode | `tests/unit/broker/test_switch.py` |
| `test_kiosk_off_on` | The flag toggles | `tests/unit/broker/test_switch.py` |

*Integration/E2E Coverage:*

- IT-18 is now runnable.
- IT-09 is partially runnable; the daemon's side arrives in 3.3.

**Acceptance Criteria:**

- [ ] IT-18 passes: the partner test user's DMs change the mode, the expiry and the kiosk flag, and a lapse DMs the partner. [R-23, R-24, R-34]
- [ ] A DM from any other user leaves the state unchanged and is answered with a refusal. [R-24]
- [ ] The defaults are crew only, with the kiosk on. [R-20, R-34]
- [ ] Extending or re-engaging social always lapses back to a non-social mode; `revert_to` is never `social`. [R-23]

---

#### Story 3.2: Doorbell core (#16)

**Wave:** P3W1
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 2.1, 2.2

Merges the Mattermost, Discord and squawkie streams into one queue per agent, with saved cursors and loud backend-down events.

**Implementation Steps:**

1. `crewchat/adapters/squawkie.py`: `SquawkieBackend(url, token)`.
   - `stream()` subscribes to `GET /api/stream` and emits `squawk.updated` events for the IDs the shim pushed with `squawk_ids` (Story 2.4), using the same predicate as `sqtk-doorbell.sh`. The daemon never reads `sqtk-mine.json` itself (§5.3).
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

- [ ] IT-11, IT-12 and IT-13 pass on all three CI legs. [R-26, R-27, R-28]
- [ ] squawkie events reproduce `sqtk-doorbell.sh`'s predicate, which a shared fixture verifies. [R-26]
- [ ] Killing one adapter's connection enqueues exactly one `backend.down`. [R-27]

---

#### Story 3.3: Daemon filters — switch cache, fail-closed, loop guard (#17)

**Wave:** P3W2
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.1, 3.2

Applies the social switch and the loop guard on both the push path and the pull path, failing closed.

**Implementation Steps:**

1. `crewchat/filters.py`:
   - `SwitchCache` fetches `switch_state` **and `crew_info`** from each local broker per (crew, server) every 30 s, on demand, and right after any local broker's `mint`, using the `BrokerClient`s from Story 1.4 (2 s timeout). For our own crew it stores the switch state with its expiry and our `crew_info` (`broker_user_id`, `human_user_ids`, `trusted` names, `minted_bot_ids`). **For each name in our `trusted` list, it fetches that crew's own broker's `crew_info` directly** (`[crews.<name>.<server_id>] broker_socket`) and stores that crew's `broker_user_id`, `human_user_ids` and `minted_bot_ids`. That is the only source of a trusted crew's membership data (§5.6). A trusted name with no `[crews.<name>.<server_id>]` table on this host is skipped (logged once), and all its senders are outsiders. A trusted crew whose broker doesn't answer within the timeout keeps its last good `crew_info`. A trusted crew never fetched since the daemon started has no member list, so its bot accounts are still recognised by `owner_id` against that crew's `broker_user_id` in `servers.toml` and are `DROP`, and its humans are treated as outsiders. A cache-miss re-fetch that times out gives `DROP` (R-22).
   - **Cache miss:** if a bot account's `owner_id` equals **any local broker's** `broker_user_id` (ours or a trusted crew's) but its user ID isn't in that broker's cached `minted_bot_ids`, `admit` re-fetches **that broker's** `crew_info` once and re-checks before giving any verdict. Re-fetches are capped at once per bot account ID per 30 s, so a reassigned bot can't make every post trigger a broker call. Only if it is still absent is the account an outsider. A re-fetch that times out gives `DROP` (R-22).
   - `admit(msg, reg, now, path) -> Verdict` returns one of `DELIVER`, `DELIVER_MARKED_OUTSIDER`, `KIOSK`, or `DROP(reason)`. `path` is `push` or `pull`.
2. **Per backend first (§5.9):**
   - Discord messages are crew-equivalent → `DELIVER` (the loop guard still applies; R-15).
   - squawkie events are exempt → `DELIVER`.
   - Discord and squawkie need nothing from the broker, so they keep flowing when the broker is down (R-27).
   - **Mattermost:** `admit` resolves the sender's crew by membership:
     - a bot account is in crew X **if and only if** `msg.owner_id` equals X's `broker_user_id` **and** `author_id` is in X's `minted_bot_ids`, for the agent's own crew and for each trusted crew (a bot's owner can reassign it, so `owner_id` alone is not enough);
     - a human is in X if and only if `author_id` is in that crew's `human_user_ids`;
     - **username suffixes are never used for membership.**

     If the author can't be resolved by the server, or no switch state or `crew_info` is cached, it returns `DROP` (R-22).
   - **Push order on Mattermost:** membership, then addressing (`addressed_to`, Story 2.3), then the loop guard (step 3), then the switch. Membership supplies `author_crew` to `addressed_to` (the author's crew when it is ours or a trusted crew's, else None), so on Mattermost a `@<dev-team>` token matches only for an author of the agent's own crew (D-103). An outsider post that isn't addressed to the agent, or fails the loop guard, is `DROP`, never `KIOSK`.
   - **Pull order on Mattermost:** membership, then the switch, **only**. Addressing and the loop guard do not apply on pull, so `chat_read` and `chat_thread` return crewmates' posts whether or not they address the agent.
   - A post with `msg.kiosk` true (`props.crew_kiosk=1`) is classed as an outsider whoever authored it, and is never `KIOSK`: `DROP` (omitted on pull) under `crew` or `trusted`, `DELIVER_MARKED_OUTSIDER` under `social` (§5.9).
   - A crewmate → `DELIVER`.
   - A trusted crew under `trusted` or `social` → `DELIVER`. **A trusted crew under `crew`** is treated as an outsider (next two rules).
   - An outsider under `social` (per `effective_mode`, checked now) → `DELIVER_MARKED_OUTSIDER` (R-23).
   - An outsider under `crew` or `trusted`, on push: with the kiosk on **and** `is_direct_address` true (Story 2.3: `@<dev>`, `@<dev>.<crew>`, a DM, or a reply in the agent's thread) **and** within that outsider's kiosk rate limit, counted per outsider, per expert, per server (`kiosk_rate_per_hour`, default 10, configurable in `servers.toml`) → `KIOSK`; otherwise `DROP`. An outsider's broadcast or crew/team token is always `DROP` here. With the kiosk off → `DROP` (R-31, R-34).
   - An outsider under `crew` or `trusted`, on pull → omitted.
3. Loop guard, `LoopGuard.admit(msg, reg)` (R-25):
   - **Messages from bot accounts:**
     - require an opted-in conversation: the agent started it, posted in it, or was mentioned into it; every DM counts as opted in;
     - require an inline mention: `@<dev>`, `@<dev>.<crew>`, `@agents`, a crew token or a team token;
     - drop once there are more than `loop_guard_window` (default 20) messages from bot accounts in the conversation's sliding window.
   - **Human messages** are admitted when addressed per R-16 to R-19, with no mention required. A direct message to the agent is addressed (R-19, Story 2.3), so the human partner's first DM, or a reply in the agent's thread, always gets through.
4. Wire `admit(path="push")` into `Doorbell` before the queue, and `admit(path="pull")` into the `read` and `thread` handlers before results are returned (R-21).
   - On **push**, `KIOSK` verdicts go into a holding queue, which 3.5 consumes.
   - On **pull**, `KIOSK` is never produced: an outsider post under crew or trusted is **omitted, and never enqueued**. Reading a channel never re-sends old outsider posts to the kiosk, and never returns them to the working agent.
5. Mark outsider content: `DELIVER_MARKED_OUTSIDER` adds `{"outsider": true}` to the event, and the shim prefixes its text with `[from outside the crew]`.

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_unresolvable_crew_dropped` | Unknown author → DROP | `tests/unit/daemon/test_filters.py` |
| `test_no_cache_drops` | No switch state, or no `crew_info` → Mattermost DROP | `tests/unit/daemon/test_filters.py` |
| `test_broker_down_discord_and_squawkie_flow` | With no broker cache, Discord and squawkie still deliver while Mattermost drops | `tests/unit/daemon/test_filters.py` |
| `test_reassigned_bot_not_crew` | A bot whose `owner_id` is our broker but which isn't in `minted_bot_ids` → outsider | `tests/unit/daemon/test_filters.py` |
| `test_unaddressed_outsider_never_kiosk` | An outsider post not addressed to the agent → DROP, never KIOSK | `tests/unit/daemon/test_filters.py` |
| `test_kiosk_post_never_kiosk_verdict` | A `crew_kiosk=1` post → DROP under crew and trusted, DELIVER_MARKED_OUTSIDER under social, never KIOSK | `tests/unit/daemon/test_filters.py` |
| `test_two_kiosks_no_second_answer` | Two crewmates' kiosks in one thread: the first kiosk answer does not trigger a second kiosk answer | `tests/unit/daemon/test_filters.py` |
| `test_modes_matrix` | Every mode × sender class → the expected verdict, including a trusted crew's sender under `crew` (outsider: KIOSK if directly addressed, else DROP) | `tests/unit/daemon/test_filters.py` |
| `test_expiry_checked_per_delivery` | A stale cached social past its expiry → reverted verdict | `tests/unit/daemon/test_filters.py` |
| `test_read_path_filtered` | `read` returns an **unaddressed** crewmate post; omits outsider posts under crew and trusted **without** enqueuing them to the kiosk; marks outsiders under social; omits kiosk posts under crew and trusted | `tests/unit/daemon/test_filters.py` |
| `test_new_crewmate_cache_miss_never_kiosk` | A bot account owned by our broker but missing from the cached `minted_bot_ids` triggers one `crew_info` re-fetch; once it appears, the post is DELIVER, and it is never KIOSK | `tests/unit/daemon/test_filters.py` |
| `test_new_trusted_agent_cache_miss_never_kiosk` | Under `trusted`, a just-registered trusted-crew agent (owned by that crew's local broker, not yet in its cached `minted_bot_ids`) directly addressing our agent triggers one re-fetch of **that** broker's `crew_info` and is DELIVER, never KIOSK | `tests/unit/daemon/test_filters.py` |
| `test_refetch_capped_per_bot` | Repeated posts from a reassigned bot owned by a local broker trigger at most one `crew_info` re-fetch per 30 s | `tests/unit/daemon/test_filters.py` |
| `test_trusted_crew_without_table_is_outsider` | A name in `trusted` with no `servers.toml` table is skipped without stopping the refresh, and its senders are outsiders | `tests/unit/daemon/test_filters.py` |
| `test_trusted_membership_only_from_its_broker` | A trusted crew's bot account is crew only via the `minted_bot_ids` in that crew's own broker's `crew_info`, fetched directly by the daemon; with that broker timing out, the last good `crew_info` is kept; a never-fetched trusted crew's bot accounts are DROP by `owner_id` and its humans are outsiders; other crews still resolve |  `tests/unit/daemon/test_filters.py` |
| `test_outsider_broadcast_never_kiosk` | An outsider's `@agents`, `@<crew>` or `@<dev-team>` post under crew or trusted → DROP, never KIOSK | `tests/unit/daemon/test_filters.py` |
| `test_kiosk_rate_limit_per_outsider` | The 11th directly addressed post from one outsider to one expert on one server within an hour → DROP; another outsider, another expert, or another server is unaffected; the limit is configurable | `tests/unit/daemon/test_filters.py` |
| `test_membership_by_owner_id_not_suffix` | A bot account named `x.<crew>` but owned by another account → outsider; a bot owned by the crew's broker → crew | `tests/unit/daemon/test_filters.py` |
| `test_trusted_membership_by_broker_user_id` | A trusted crew's bot account is recognised by its broker's `owner_id` plus that crew's `minted_bot_ids`, and its humans by listed IDs | `tests/unit/daemon/test_filters.py` |
| `test_discord_crew_equivalent_squawkie_exempt` | Discord → DELIVER (subject to the loop guard); squawkie → DELIVER | `tests/unit/daemon/test_filters.py` |
| `test_kiosk_post_classed_outsider` | A post with `props.crew_kiosk=1` from a crewmate's account → outsider verdict | `tests/unit/daemon/test_filters.py` |
| `test_loop_guard_window` | The 21st message from bot accounts in the window is dropped | `tests/unit/daemon/test_loopguard.py` |
| `test_loop_guard_requires_mention_and_optin` | A message from a bot account with no mention, or no opt-in, → dropped | `tests/unit/daemon/test_loopguard.py` |
| `test_loop_guard_mention_set` | `@<dev>`, `@<dev>.<crew>`, `@agents`, crew and team tokens all count as mentions | `tests/unit/daemon/test_loopguard.py` |
| `test_human_addressed_admitted_without_mention` | The human partner's DM, and a human reply in the agent's thread, are admitted | `tests/unit/daemon/test_loopguard.py` |

*Integration/E2E Coverage:*

- IT-08, IT-09, IT-10 and IT-23 are now runnable.

**Acceptance Criteria:**

- [ ] IT-08, IT-09, IT-10 and IT-23 pass on all three CI legs. [R-20, R-21, R-22, R-23, R-24, R-25, R-34]
- [ ] With the broker stopped and the cached social past its expiry, deliveries use the reverted mode. [R-23]
- [ ] Membership and the switch apply to both `subscribe` and `read`; addressing and the loop guard apply to `subscribe` only, so `read` returns unaddressed crewmate posts. A test asserts both paths. [R-21]
- [ ] A newly registered crewmate or trusted-crew agent is never routed to the kiosk (cache-miss re-fetch of the owning local broker, capped per bot), and an outsider's broadcast never reaches a kiosk. [R-22, R-31]
- [ ] A trusted crew's broker that doesn't answer within `broker_timeout_s` never stalls any other delivery. Its last good `crew_info` is kept; with none, its bot accounts are `DROP` by `owner_id` and its humans are outsiders. [R-22]
- [ ] Membership never uses username suffixes: IT-08's spoofed-username case is not admitted, and neither is a reassigned bot missing from `minted_bot_ids`. [R-22]
- [ ] On the pull path, outsider posts under crew and trusted are omitted, never enqueued, and pull never produces a `KIOSK` verdict. [R-21, R-31]
- [ ] Kiosk-tagged posts never get a `KIOSK` verdict, and two kiosks in one thread don't answer each other. [R-31]
- [ ] With the broker down, Mattermost drops while Discord and squawkie keep flowing. [R-22, R-27]

---

#### Story 3.4: Thin doorbell one-shot (#18)

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
- [ ] It exits 4 when the daemon socket is absent, and never exits 0 without an event. [CT-08]

---

#### Story 3.5: Kiosk (#19)

**Wave:** P3W3
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.3, 1.2

Answers outsiders while social is off, through a tool-less session seeded only from the `## Kiosk` section of the expert's seed.

**Implementation Steps:**

1. `crewchat/kiosk.py`: `BriefStore` holds the latest brief per Dev-Name, as received in `kiosk_brief` frames from the shim (Story 2.4). The shim has already extracted the `## Kiosk` section, and the daemon never reads the seed file (§5.3, R-32).
2. `KioskSession(dev_name, server_id, runner)`:
   - One session per (expert, server). It starts lazily on the first `KIOSK` verdict for that expert on that server (R-31).
   - It uses the current brief from `BriefStore`. With no brief, it replies "I have nothing to share yet."
   - Its prompt is `KIOSK_HEADER + brief + history`. **`history` holds only the asker's own posts in that thread and the kiosk's own earlier answers to that asker.** Crewmates' posts in the thread are never included (§5.11).
3. `runner` implements the mechanism chosen in spike 1.2. Either:
   - `ClaudeCliRunner` runs, as OS user `crewkiosk` (never `crewchat`), with an empty HOME and an empty working directory: `claude -p --restricted --strict-mcp-config --tools "" --disallowedTools "mcp__*"`. It is started through `crewkiosk.socket` and `crewkiosk@.service` (`Accept=yes`): the daemon, which keeps `NoNewPrivileges=yes`, connects to `/run/crewkiosk/kiosk.sock` (0660, group `crewchat`) and systemd runs this one fixed command line as `crewkiosk` for that connection. No sudo rule ships (D-107, which supersedes the earlier `sudo -u crewkiosk` rule). `crewkiosk` can read no chat or broker credential; its only credential is the model credential spike 1.2 names; or
   - `ApiRunner` makes a direct model API call with no tool definitions, using the credential decided in 1.2.

   Both expose `answer(prompt) -> str`.
4. One session per expert **per server** serves every outsider on that server, so their contexts mingle (D-067); outsiders on different servers never share a session, and the session is keyed `(<dev>, <server_id>)`. Each Q&A is appended to that server's transcript, `/var/lib/crewchat/kiosk/<dev>-<server_id>.jsonl`, as `{ts, thread, post_id, asker_id, question, answer}`. A question whose `post_id` is already in the transcript is not answered again, so a replay after a daemon restart gets no second answer (#97).
5. Consume the `KIOSK` holding queue from 3.3. Answer in the outsider's thread on the originating backend (Mattermost only; §5.9):
   - `strip_mentions(answer)` removes every `@token` before posting;
   - the post carries `props.crew_kiosk=1`, so `admit` treats it as outsider content (§5.9).
6. `KIOSK_HEADER`: "You are this agent's kiosk. Answer questions using only the brief below. You cannot act, and you cannot share anything that is not in the brief."

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_missing_brief_fixed_reply` | No brief pushed → "nothing to share yet" | `tests/unit/daemon/test_kiosk.py` |
| `test_lazy_start` | No session until the first outsider message | `tests/unit/daemon/test_kiosk.py` |
| `test_new_brief_replaces_old` | A new `kiosk_brief` frame → the next answer uses it | `tests/unit/daemon/test_kiosk.py` |
| `test_history_only_asker_and_kiosk` | A crewmate's post in the thread never appears in the prompt | `tests/unit/daemon/test_kiosk.py` |
| `test_answer_mentions_stripped_and_tagged` | `@` tokens removed; the post carries `props.crew_kiosk=1` | `tests/unit/daemon/test_kiosk.py` |
| `test_runner_has_no_tools` | The CLI runner runs as `crewkiosk` with `--restricted --strict-mcp-config --tools ""` and the MCP exclusions, with an empty HOME and working directory; the API runner sends no tools | `tests/unit/daemon/test_kiosk_runner.py` |
| `test_daemon_never_reads_seed` | The kiosk code has no file read of `reseed-revive.md` | `tests/unit/daemon/test_kiosk.py` |
| `test_transcript_appended` | Every Q&A is written to the JSONL | `tests/unit/daemon/test_kiosk.py` |

*Integration/E2E Coverage:*

- IT-21 is now runnable. It uses a fixture seed with a `## Kiosk` section and a secret elsewhere in the file, pushed through the real shim, and a thread containing a crewmate's post.

**Acceptance Criteria:**

- [ ] IT-21 passes: the outsider is answered from the `## Kiosk` section, and a secret placed elsewhere in the fixture seed never appears in any prompt the runner receives (asserted on the captured prompt). [R-31, R-32]
- [ ] Neither does a crewmate's post in the same thread. [R-31, R-32]
- [ ] The kiosk's runner cannot run a tool, and a test asserts the invocation. If the CLI runner is used, it runs as `crewkiosk` and cannot read any chat or broker credential file (checked with `test -r` on the known paths). [R-31, R-08]
- [ ] Kiosk posts carry `props.crew_kiosk=1` and no @mentions. [R-31]
- [ ] Every answered question is recorded in the kiosk transcript. [R-31]

---

#### Story 3.6: Social-engage kiosk review (#20)

**Wave:** P3W4
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.5, 3.4

When the human partner engages social, the working agent reviews what the kiosk handled since the last review, and raises anything notable to the partner.

**Implementation Steps:**

1. In `crewchat/filters.py`, detect a mode transition to `social` in `SwitchCache`, comparing successive `effective_mode` values per (crew, server).
2. On that transition on a server, `kiosk.review_since_cursor(dev, server_id)` reads **only that server's** transcript, `/var/lib/crewchat/kiosk/<dev>-<server_id>.jsonl`, from the offset in `<dev>-<server_id>.cursor`. It enqueues one `{"type":"kiosk.review","outsider":true,"server":...,"entries":[...]}` event for the working agent, and advances that cursor only once the event is handed over (R-33). Other servers' transcripts and cursors are untouched. When an agent registers, the same review runs on each server where its crew's switch is already social (#99).
3. The event's text, as rendered by the shim, opens with `[from outside the crew] Kiosk review: review these exchanges and raise anything notable to your human partner.`

**Test Procedures:**

*Unit Tests:*

| Test Name | Purpose | File Location |
|-----------|---------|---------------|
| `test_review_on_social_transition_only` | Fires on crew/trusted→social, not on other changes | `tests/unit/daemon/test_kiosk_review.py` |
| `test_review_since_cursor_and_advance` | Only the new entries; the cursor advances | `tests/unit/daemon/test_kiosk_review.py` |
| `test_review_only_that_servers_transcript` | Social engaged on server A delivers only A's transcript; server B's cursor is unchanged | `tests/unit/daemon/test_kiosk_review.py` |
| `test_review_marked_outsider` | The event carries `outsider: true` | `tests/unit/daemon/test_kiosk_review.py` |

*Integration/E2E Coverage:*

- IT-22 is now runnable.
- E2E-02 is partially runnable; it completes in 3.8, which depends on this story.

**Acceptance Criteria:**

- [ ] IT-22 passes on all three CI legs. [R-33]
- [ ] No kiosk content reaches the working agent while social is off: a test asserts its queue stays empty during kiosk traffic. [R-31, R-33]

---

#### Story 3.7: Docs — runbook, partner guide, architecture (#21)

**Wave:** P3W4
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.3, 3.4

Delivers the operator runbook (with its prerequisites and install verification folded in), the human partner guide and the architecture document. CI runs the runbook's commands, so it can't drift.

**Implementation Steps:**

1. `docs/runbook.md` has these sections:
   - "Prerequisites": the OS users `crewchat`, `crewbroker` and (if the CLI kiosk runner is used) `crewkiosk` with an empty HOME, the group `crewchat-clients` (and `crewbroker` not in `crewchat`; the broker socket directory gets its group from tmpfiles.d), Python 3.12, uv, and the socket mount into containers;
   - "Install the daemon" and "Install the broker", as fenced `bash` blocks taken from §5.B, the broker as one `crewbroker@<crew>-<server_id>` instance per (crew, Mattermost server);
   - "Configure", listing every required field exactly as the spec defines it:
     - `/etc/crewchat/servers.toml`: the array of `mattermost` server tables (`id`, `url`, and from #117 the optional `kiosk`, default false, and `kiosk_daily_cap`, default 25), `[discord] enabled`, `default_crew`, `broker_timeout_s` (default 2), `kiosk_rate_per_hour` (default 10), `loop_guard_window` (default 20), `my_threads_retention_days` (default 90, D-104), `[crews.<crew>] needs_list`, and per crew and server `[crews.<crew>.<server_id>] broker_socket` and `broker_user_id` (one table for every local broker, including trusted crews' brokers), with from #117 the optional `agents_allow` and `agents_deny`: which agents `register` mints for there, by the shim's `project_key` (`repo_key()`) or `name:<dev_name>`. That is an accident guard, not a security boundary (D-055);
     - `/etc/crewbroker/<crew>-<server_id>.toml`: `crew`, `server_id`, `server_url`, `human_user_ids`, and `trusted = ["<crew>", …]` (names only); `broker_user_id` is filled at startup from `/users/me`;
   - "Verify the install": a fenced `bash` block that runs `make verify-install`. That target checks every known credential file with `test -e <file> && ! sudo -u agent test -r <file>`, and the same for `crewkiosk`;
   - "Rollback".
2. `tests/integration/test_runbook.py` (IT-20) extracts every fenced `bash` block under "Install …" and "Verify the install" from `docs/runbook.md` and runs them in order, in a fresh container in CI. Every block must exit 0.
3. `docs/partner-guide.md`: the broker DM commands (`social <dur>`, `trusted`, `crew`, `kiosk off`, `kiosk on`); what each mode delivers; that the switch is set **separately on each server** (DM that server's broker account); expiry and lapse notices; the kiosk and its review on social engage; and how to write a `## Kiosk` section.
4. `docs/architecture.md`: the §4.1 diagram, the components, the data paths for push, pull and kiosk, and the trust boundaries (the per-host socket, the `crewchat`, `crewbroker` and `crewkiosk` users), the membership rule (bot account `owner_id` **plus** the broker's `minted_bot_ids`, never username suffixes; trusted crews only through the `crew_info` the daemon fetches directly from each trusted crew's broker on this host, with no broker-to-broker calls, which means a trusted crew's humans are whoever that crew's own broker reports), the push and pull filter orders, and the per-backend rule (Discord crew-equivalent, squawkie exempt).

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

#### Story 3.8: E2E suite and remaining API tests (#22)

**Wave:** P3W5
**Repository:** Wave-Engineering/mattermost-crew
**Dependencies:** 3.5, 3.6

Runs the three end-to-end flows and the remaining API tests in CI.

**Implementation Steps:**

1. `tests/e2e/conftest.py`: two crews (`oaw` and `synth`), each with its own human test user, its own `crewbroker@<crew>-<server_id>` instance and two registered agents. Both brokers run on the CI host, each crew lists the other in `trusted = [...]` by name, and `servers.toml` has a `[crews.<crew>.<server_id>]` table for both brokers, so the daemon fetches each crew's `crew_info` directly from that crew's broker. A third, deliberately unresponsive fake broker socket is listed as a trusted crew, to prove a hung broker never stalls the others and that the other crews still resolve. There is also one unlisted outsider user, plus one outsider bot account named `x.oaw` but **not** owned by the `oaw` broker, to prove membership ignores username suffixes.
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

- [ ] E2E-01, E2E-02 and E2E-03 pass in CI on all three CI legs. [R-01, R-09, R-12, R-16, R-20, R-21, R-23, R-25, R-26, R-31, R-33]
- [ ] IT-19 passes. [R-01]

---

#### Story 3.9: Closing — VRTM, manifest, partner-name grep, dev-space smoke (#23)

**Wave:** P3W6
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

The sketchbook (`docs/SKETCHBOOK.md`) holds the shaping decisions. Plan #3's `[ledger D-NNN]` comments hold the Dev Spec decisions.

**Citation rule (D-092):** sketchbook decisions are cited **S-NN** (the sketchbook's own headings read D-NN, so S-18 is sketchbook D-18); Plan #3's ledger entries are cited **D-NNN**, three digits. D-18 and D-018 are different decisions, so never normalize the zero-padding. Older text that says "sketchbook D-NN" means S-NN; nothing is renumbered.

| Sketchbook | Subject | Plan #3 entries |
|-----------|---------|-----------------|
| S-01 | Cross-crew collaboration goal | §1.2; D-017 |
| S-02 | Control of data | D-002 |
| S-03 | Configured server instances | D-002, D-032 (R-10), D-016 |
| S-04 | Backend is a default | D-003, D-033 (R-11) |
| S-05 | Replies follow the thread | D-034 (R-12) |
| S-06 | Explicit backend parameter | D-035 (R-13) |
| S-07 | Forge-less home backend | D-033 (R-11 step 1) |
| S-08 | Abstract in the server | D-010 (CT-04), D-031 (R-09) |
| S-09 | Partnered, not bots | D-017 (CP-01) |
| S-10 | Partner direction is authority | D-004, D-019 (CP-03) |
| S-11 | Crew namespace | D-011 (CT-05), D-023, D-024 (R-01, R-02) |
| S-12 | Real accounts | D-023, D-026, D-051 (R-01, R-04, R-29) |
| S-13 | scream-hole stays for Discord | D-001, D-060 |
| S-14 | Upstream library | D-009 (CT-03) |
| S-15 | Discord parity | D-001, D-037 (R-15) |
| S-16 | Backend down is loud | D-049 (R-27) |
| S-17 | Wave order | §8 (superseded by D-064, D-065, D-089) |
| S-18 | Its own program | D-064 |
| S-19 | Per-crew broker (now one per crew, server: D-082) | D-008, D-027 to D-029 (R-05 to R-07) |
| S-20 | Read and write through our server | D-008 (CT-02), D-030 (R-08), D-043 |
| S-21 | Loop guard | D-047 (R-25) |
| S-22 | One doorbell core | D-015 (CT-09), D-048 (R-26), D-060 |
| S-23 | Social switch | D-005, D-018 (CP-02), D-042 to D-046 |
| S-24 | Addressing | D-020 (CP-04), D-038 to D-041 |
| S-25 | Prod scope | D-019 (CP-03) |
| S-26 | Order of work, dev space | D-006, D-022, D-066 |
| — | Constraints | D-007 to D-016 |
| — | Design | D-053 (superseded by D-064), D-054 to D-062, D-071 |
| — | Test policy (no MV) | D-063 |
| — | Agent and integration points | D-065 (phase landing amended by D-089) |
| — | Kiosk | D-067, D-068 (superseded by D-072), D-069, D-070, D-072 |
| — | PR #5 precheck review: membership, per-backend rule, loop guard, rotation, kiosk isolation, agent paths | D-073, D-074, D-075 (supersedes D-047), D-076, D-077, D-078 |
| — | Review round 2: `crew_info`, DMs addressed, R-30 by register, (crew, server) keying, no kiosk-to-kiosk | D-079 (supersedes D-073 in part), D-080 (supersedes D-041), D-081 (supersedes D-052), D-082, D-083 |
| — | Review round 3: same-host trust, pull filters, 401 reconnect, cache miss, per-server switch | D-084, D-085, D-086 |
| — | Review round 4: no broker-to-broker calls, broker timeouts, local cache-miss re-fetch, reactivation | D-087 (supersedes D-079 in part) |
| — | Approval | D-088 |
| — | Pre-kickoff amendments: one Plan per phase (#26, #27, #28), the spike without CI, the 11.8 floor (CT-11), the S-NN citation rule | D-089 (supersedes D-065 in part), D-090, D-091, D-092 |
| — | Spike 1.2 answers to §5.N (`docs/spikes/2026-10-spike-1.2.md`) | D-093 to D-101; D-096 resolves D-071 (the kiosk is the CLI runner); D-100 supersedes D-079 and D-087 in part (bot ownership is checked in the broker) |
| — | P2W1 flight findings (Story 2.4b): `register` carries team and avatar, per-backend team scope; persisted my-threads | D-103, D-104 |
| — | Membership on mint: the broker adds each bot it mints to its crew's team and default channels, not a greeter bot (#132) | D-109 |

### Appendix V: Verification Requirements Traceability Matrix (VRTM)

Story 3.9 fills in the Status column. Each status is the result of its verification items in CI
run [40200284162](https://github.com/Wave-Engineering/mattermost-crew/actions/runs/40200284162)
(2026-10-25, head `ad27c94`, the Phase 3 content through Story 3.8): all three legs
(Team 11.11.3, Enterprise 11.11.3, Team 11.8.5), the installed tests (IT-01) and the `install` job
(IT-20), with no failures and no IT or E2E item skipped. `main` does not carry Phase 3 until #28's
release PR, whose own CI re-runs every item on the final `main`.

| Req ID | Requirement (short) | Source | Verification Item | Verification Method | Status |
|--------|-------------------|--------|------------------|-------------------|--------|
| R-01 | Per-agent account `<dev>.<crew>` | Stories 1.3, 1.4, 2.1 | IT-01, IT-17, IT-19, E2E-01 | integration / e2e test | Pass |
| R-02 | Crew defaults to the partner's username | Story 1.3 | IT-17 | integration test | Pass |
| R-03 | Broker creates a missing bot account | Story 1.3 | IT-01 | integration test | Pass |
| R-04 | Echo filtered by user ID | Story 2.1 | IT-14 | integration test | Pass |
| R-05 | Mint on register, re-mint daily (daemon-driven rotation) | Stories 1.3, 1.4, 2.1 | IT-01, IT-02 | integration test | Pass |
| R-06 | Broker has no admin; patched role held only by configured broker accounts | Stories 1.3, 1.4 | IT-01, IT-20 | integration test | Pass |
| R-07 | Native expiry, else revocation | Stories 1.3, 1.4, 2.1 | IT-02 | integration test | Pass |
| R-08 | Token not readable by the agent | Stories 1.4, 3.7 | IT-01, IT-20 | integration test | Pass |
| R-09 | One tool contract | Story 2.4 | IT-16, E2E-01 | integration / e2e test | Pass |
| R-10 | Configured server instances | Stories 1.4, 2.1, 2.4 | IT-16 | integration test | Pass |
| R-11 | Default-backend resolution; headless fails loud | Story 2.4 | IT-04 | integration test | Pass |
| R-12 | Reply follows thread and backend | Story 2.4 | IT-05, E2E-01 | integration / e2e test | Pass |
| R-13 | Explicit backend used and logged | Story 2.4 | IT-05 | integration test | Pass |
| R-14 | Protocol marker; older versions parse | Stories 1.4, 2.1, 2.2 | IT-06 | integration test | Pass |
| R-15 | Discord parity | Stories 2.2, 2.4, 2.4c | IT-15, IT-25 | integration test | Pass |
| R-16 | `@agents` (and `@all` on Discord) | Story 2.3 | IT-07, E2E-01, E2E-03 | integration / e2e test | Pass |
| R-17 | Crew and team tokens by our filter | Story 2.3, Story 2.4b | IT-07, IT-24 | integration test | Pass |
| R-18 | `.` kept in `@name.crew` | Story 2.3 | IT-07 | integration test | Pass |
| R-19 | "Reply to me" by thread, or a DM to the agent | Story 2.3, Story 2.4b | IT-07, IT-24 | integration test | Pass |
| R-20 | Three switch settings, crew only by default | Story 3.1 | IT-08, E2E-01 | integration / e2e test | Pass |
| R-21 | Switch gates push and pull | Story 3.3 | IT-08, E2E-02 | integration / e2e test | Pass |
| R-22 | Unresolvable crew dropped; membership by `owner_id` plus `minted_bot_ids` | Stories 1.3, 3.3 | IT-08 | integration test | Pass |
| R-23 | Social marked, lapses per delivery, reverts, notifies | Stories 3.1, 3.3 | IT-09, IT-18, E2E-02 | integration / e2e test | Pass |
| R-24 | Only the partner flips; broker holds state | Story 3.1 | IT-09, IT-18 | integration test | Pass |
| R-25 | Loop guard | Story 3.3 | IT-10, E2E-03 | integration / e2e test | Pass |
| R-26 | One merged doorbell | Stories 3.2, 3.4 | IT-11, E2E-01 | integration / e2e test | Pass |
| R-27 | Backend down is loud; others flow | Story 3.2 | IT-12 | integration test | Pass |
| R-28 | Cursor replay: queued exactly once, handed over at least once | Story 3.2 | IT-13 | integration test | Pass |
| R-29 | Server mention markers | Story 2.1 | IT-14 | integration test | Pass |
| R-30 | Prune bot accounts with no register in 30 days | Stories 1.3, 1.4 | IT-03 | integration test | Pass |
| R-31 | Outsiders answered by a tool-less kiosk while social is off | Story 3.5 | IT-21, E2E-02 | integration / e2e test | Pass |
| R-32 | Kiosk sees only the `## Kiosk` section | Story 3.5 | IT-21 | integration test | Pass |
| R-33 | Kiosk review on social engage | Story 3.6 | IT-22, E2E-02 | integration / e2e test | Pass |
| R-34 | Kiosk can be turned off; outsiders then dropped | Stories 3.1, 3.3 | IT-18, IT-23 | integration test | Pass |
