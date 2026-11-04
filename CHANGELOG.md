# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.1] — 2026-11-04

A new agent's chat now comes up on its own. `crewchat-mcp` used to exit when the agent had no
name yet, which is every new agent at the moment its session starts, and stayed dead until a
person typed `/mcp` to reconnect.

**Rollback.** No config change is needed to go back to v0.4.0: v0.4.1 adds no setting and no
file, and changes no daemon, hook or settings. To return to v0.4.0, install it again from the
`v0.4.0` tag with the same lock constraints. A session already running keeps the code it started
with until it reconnects or restarts, so installing v0.4.1 does not change a running session
either way.

### Fixed
- **`crewchat-mcp` starts without an identity file (#226).** A newly minted agent has no
  `.claude/agent-identity.json` until `/name` runs, and `/name` runs after the MCP server has
  started. The shim raised at construction, so `crewchat-mcp` exited 1 and the agent had no
  chat for the whole session, until a `/mcp` reconnect or a restart. The shim now starts
  without an identity, reads the file when it is needed until it names a `dev_name`, and
  registers on the first call or timer tick after `/name`, in the same process. Before that, a
  tool call fails with an error that names the file and says to run `/name`, and the timer
  logs one INFO line instead of an error per tick. An unreadable or malformed file is still
  reported with its path. No daemon, hook or settings change.

## [0.4.0] — 2026-11-02

The waiter batches. `crewchat-waiter` now wakes an agent once with every event that queued while it
was busy, as a numbered list, instead of one wake per event. The rest is CI: `main` requires a
`ci-gate` check, and the CI legs no longer flake on a fresh runner's apt lock.

**Rollback.** No config change is needed to go back to v0.3.0: v0.4.0 adds no required setting and
no file, and changes no daemon, hook or settings. To keep v0.4.0 but get one event per wake again,
set `CREWCHAT_WAITER_BATCH_WINDOW_S=0` where the hook runs. Installing v0.4.0 on a host replaces
`crewchat-waiter` for the next arm, so no daemon restart is needed for it.

### Added
- **`crewchat-waiter`: one wake delivers every queued event (#212).** While an agent was busy,
  further events queued in the daemon and each cost a wake of its own (a hook round-trip, a
  context re-read, a turn), and the agent never saw them together. After the first event the
  waiter now keeps asking for the next with a short budget until the queue is empty, and wakes
  once with all of them as a numbered list, oldest first, under a header giving the count; a
  single event is the text it always was. `CREWCHAT_WAITER_BATCH_WINDOW_S` (default 1.0 s; `0`
  turns batching off) and `CREWCHAT_WAITER_BATCH_MAX` (default 20, plus a 16 KB text budget)
  bound it; past either the text says more may be queued and the rest arrive on the next wake.
  Ids go into the seen set only after the whole text is written, and a daemon outage mid-collection
  delivers what was already received. No daemon, hook or settings change. A lone event now wakes
  an idle agent about one window later than before; `CREWCHAT_WAITER_BATCH_WINDOW_S=0` restores
  that. Events the waiter holds while it waits out the window are in its memory only: a waiter
  killed in that window, or a failed write to stderr, loses them (up to 20, where it was one), and a
  reply landing exactly as the window times out can be acked and lost; the runbook says so.
- **`main` now requires CI to pass (#213).** It had no required status checks: the org-level ruleset
  applied, but the per-repo branch protection every other Wave-Engineering repo carries was never set,
  so the platform would have let a red PR merge. A new `ci-gate` job in `ci.yml` needs every other
  job and passes only if all succeeded (`scripts/ci/gate.sh`), and branch protection requires
  `ci-gate` alone, with `strict: true`. The `ci` matrix legs are deliberately not named in the
  protection: an image bump renames a leg, and a required check that stops reporting blocks every PR.

### Fixed
- **`host-prereqs.sh` retries `apt-get` on the lists lock (#220).** About one CI leg in three failed
  with "Could not get lock /var/lib/apt/lists/lock": a fresh ephemeral VM still has the image's own
  `apt-get` running for its first minute, and `DPkg::Lock::Timeout` waits for dpkg's lock, not that
  one. `apt_retry` retries up to `HOST_PREREQS_APT_ATTEMPTS` times (12, 10 s apart) only when apt's
  output says it could not get a lock (not on "Permission denied", not on any other failure), and the
  dpkg wait inside each try is cut to 30 s so a lock that never clears stays bounded.

## [0.3.0] — 2026-11-01

Messaging stability. A Discord or scream-hole blip no longer wakes any agent (one alarm for an
outage past ten minutes), `crewchat-waiter` delivers chat with nothing for the agent to arm, and
an agent's own squawk edits no longer ring it. Also an agent can edit its own post, and the
kiosk and Discord fixes that were waiting for a release.

**Rollback.** Going back to v0.2.8 needs no config change: v0.3.0 adds no required setting and no
file. Clients first: a host on v0.2.8 has no `crewchat-waiter`, and the kit's hook guard then
exits 0 silently, so a registered hook does no harm, but chat reaches that agent only through
`crewchat-doorbell` until the hook entries are removed. The daemon's restart drops every armed
doorbell for a moment, so do it with the fleet stopped.

### Added

- **`crewchat-waiter`: a doorbell nobody has to arm** (#204). A persistent, single-instance
  process (one `flock` per Dev-Name) meant to run from Claude Code `SessionStart` and `Stop`
  hooks with `asyncRewake: true`, so the harness starts and restarts it and an idle agent is
  woken by a message with no agent action. It reuses the doorbell's wait (restart grace, seen
  set, repeat backoff), and a daemon that stays down does not end it. It exits 2 on an event
  with a pointer plus the event on stderr, and nothing else reaches the model; diagnostics go to
  `crewchat/waiter/<dev>.log` under the state dir. No identity, a waiter already holding the
  lock, or no lock to be had on the host (it will not run unguarded) is an exit 0 that wakes
  no one. `crewchat-doorbell` now exits 5 when a waiter holds its Dev-Name, rather than become
  a second consumer that doubles every wake. The hook snippet is in `docs/runbook.md`
  ("Arm-free delivery").
- **An agent can edit its own post** (#149): the `edit` daemon op, `chat_edit(post_id, text,
  embed?, backend?)` and `crewchat-send --edit <post_id>`, so a status board is updated in place
  instead of reposted. Mattermost patches the post as its author. On Discord, where every agent
  posts as the one bot, the daemon edits only a message it sent for that agent, and re-signs it
  once. Another agent's post is `forbidden`, and `crewchat-send` exits 6 for a post that is
  gone or not the agent's: the caller posts a new one.

- **`scripts/smoke-devspace.sh --cleanup <run> [--yes]`** (#157). The smoke picks its run suffix,
  prints it, and the E2E world writes a creation record of every user, bot and team it makes.
  `--cleanup` dry-runs by default, and with `--yes` disables that run's bots, deactivates its
  users and archives its team: only ids in the record, still on the server under the recorded
  name, active, and carrying the suffix.

### Changed

- **Docs: the `crewchat-waiter` hook timeout is 1814400 s, and "in effect" is stated** (#208).
  The runbook ("Arm-free delivery") and the `waiter.py` docstring showed `"timeout": 86400`. The
  harness kills a waiting hook silently at its timeout and only the next `Stop` restarts it, so
  the timeout is how long an idle agent stays reachable; both now show 21 days and say why.
  Both also say delivery is in effect only where the hook is registered AND the waiter is
  installed, and that installing the package changes no settings. No behaviour change.
- **An agent's own squawk edits, and its own tag, no longer ring it** (#206). A `squawk.updated`
  whose last editor is the agent (squawkie stores the editor's `initials` on every change, and
  `sqtk` signs with the Dev-Name's: uppercase alphanumerics, first 3) rings nothing, live or in
  the catch-up, so retiring or editing one's own squawk no longer costs a wake. A change by
  anyone else after it rings as before. An answer made only of the last editor's own
  `(INITIALS)` tag is no answer (the kit's `sqtk-answer.jq`, claudecode-workflow#1421, which
  `scripts/ci/kit-ref` now pins and the vendored copy matches byte for byte); `(ok)` from a
  human, or any text, still is. Known limit: editors whose first three letters coincide can't be
  told apart, and a `SQUAWK_INITIALS` override is invisible to the daemon.
- **A backend blip wakes no agent** (#202). `backend.down` and `backend.up` no longer queue on
  every transition. A backend that is still down after `DEAF_ALARM_S` (600 s) raises one
  `backend.down` (carrying `since`) and, once back, one `backend.up`; an outage that ends sooner
  queues nothing. `Doorbell(deaf_alarm_s=0)` raises it at once, as before. The Discord adapter
  no longer marks scream-hole unhealthy on one failed 2 s `/health` probe: it re-probes after
  1 s and flips after 3 failures in a row or 10 s of failing, and the first probe waits 5 s.

- **`crewchat serve` is `build()` plus `run()`** (#157). The E2E world runs the same code
  instead of a hand copy, so it now has the pool manifest, the upload store and the daily
  rotation too. On stop, the daemon cancels a switch refresh blocked on a hung broker instead of
  waiting on it.

### Fixed

- **An agent's DM to its broker no longer loops through the kiosk** (#153). The daemon took
  the broker account for an outsider, so the broker's refusal went to the agent's kiosk, the
  kiosk answered, the broker refused again, and so on for about ten rounds, each spending a
  model call and a unit of the server's daily kiosk cap. A post by any configured broker
  account is now dropped, on push and pull, and the broker no longer answers a kiosk answer.

- **A kiosk question is answered once across a daemon crash** (#153). A daemon that died
  between posting an answer and recording it answered the same question again after the
  restart. Each answer now names its question (`props.crew_kiosk_answers`), and a replayed
  question whose answer is already in its thread is recorded, not answered again.

- **A failed kiosk brief push after a re-register is retried** (#153). A forced push that
  failed was never sent again, so the kiosk answered "I have nothing to share yet." until the
  seed changed. It now stays owed until it lands, and a failed brief push no longer skips the
  squawk ids.

- **A deactivated agent's websocket stops reconnecting** (#155). When the daemon retired an
  agent's token on one server, because its bot was deactivated or the register left that server
  off the allowlist, the agent's websocket there kept reconnecting with backoff, and the doorbell
  kept reporting the backend down. Retiring a token now closes that agent's stream on that server
  only. A websocket that the server closes before its first frame also checks the agent's
  account, so a deactivated bot is found and retired instead of retried.

- **Discord replaces an agent's own inline signature** (#155). A message that ended with the
  agent's signature on the same line as the text, as in `... done. — **babelfish** 🐠 (oaw)`,
  got a second signature appended. The agent's own signature at the end of the text is now
  replaced. Another agent's signature, a code-quoted one and one mid-text are kept.

- **`chat_read` and `chat_thread` set `dm` on Mattermost** (#155). They always reported
  `dm: false`, so an agent reading a DM back could not tell it was one. A post in a DM channel
  with the reading agent, its own posts included, is now `dm: true`.

### Security

- **The MCP shim's log file is opened `O_NOFOLLOW` and set to 0600** (#157). A symlink at
  `CREWCHAT_SHIM_LOG_FILE` is refused, and an existing file is tightened: the open's mode had
  applied only to a file it created.

## [0.2.8] — 2026-10-31

An agent that a 429 left off a server gets back on it within minutes instead of a day, and
`dm` takes a bare Dev-Name on Mattermost.

**Rollback.** Going back to v0.2.7 needs no config change: v0.2.8 adds no setting and no file.
An agent a retry put back on a server stays registered there.

### Fixed

- **A partial register retries its failed servers** (#186). A register that minted on some
  servers and not others counted as a success, so the shim did not register again for a day and
  the agent stayed off the failed servers all that time. A daemon restart could not fix it,
  because the restart's own burst of re-registers is what overran the broker account's rate
  budget. The daemon now mints again on only the failed servers, in the background, with capped
  and jittered backoff, so a restarted fleet's retries spread out. It never mints on a squatted
  server or one that worked, since a mint there rotates a good token. A recovered server gets the
  same doorbell attach and kiosk review as a fresh register, and a newer register cancels a
  pending retry.

- **`chat_dm` takes a bare Dev-Name on Mattermost** (#186). A name that is no username resolves
  to the crewmate `<dev>.<crew>`, as inbound addressing already did. A bare username that exists
  wins, and `not_found` names both usernames tried.

- **The broker reads a caller's deadline against epoch time** (#189). The daemon stamps each
  call's `deadline` with `time.time()`, and since #158 the broker read it against its injected
  clock instead. In production the two agree. In the E2E world, which moves that clock, every
  mint after E2E-02 could be refused as `expired`, so E2E-03 was a race on every leg.

## [0.2.7] — 2026-10-31

Research kiosks answer from their server's corpus, each confined to its server. The broker
keeps its bots in tagged channels, adopts orphans and sets avatars. A switch command or DM
sent while the broker was offline is no longer lost, an agent's DM wakes its recipient, and
the daemon's journal is no longer flooded by httpx request lines.

**Rollback.** Going back to v0.2.6 needs no change to the broker's config: v0.2.6 ignores
`channel_tag`, `membership_reconcile_s` and the broker's `dm_cursor.json`. **On the daemon, set
`kiosk_research` off first if it is on**, because v0.2.6 has no research runner. Avatars the
broker set stay set.

### Added

- **The broker keeps its bots in every channel tagged `crew-<crew>`** (#167, D-116, which extends D-109).
  Put the tag in a channel's header or purpose, and within `membership_reconcile_s` (default
  300 s; 0 turns it off) every bot the crew's broker owns is a member. A bot minted later joins
  at once. The pass also re-checks the team and `default_channels`, so a channel added there
  reaches the bots already registered. The broker account joins a tagged public channel itself;
  a private one needs the broker added first. Nothing is ever removed. `channel_tag` overrides
  the tag.

- **Kiosks research their server's corpus, confined to that server** (#142, the last part of
  #136; D-110 to D-113). R-31 is amended.
  - **Framing.** On a `kiosk_research = true` server, the daemon frames each answer to that
    server's own socket, `/run/crewkiosk/research-<server>.sock`. The header line comes first,
    `crewkiosk/1 server=<id> post=<post_id> answer=<32 hex>`, under a research prompt: cite the
    repo, path and commit; say what a repo declares, not what runs; no answer means a human.
  - **The units.** Each research server has a socket and unit of its own, written by
    `crewkiosk-research-units <server>`. Each unit is linked to one shared body,
    `/usr/local/share/crewkiosk/crewkiosk-research@.service`, which must sit outside every
    unit search path: the script refuses otherwise. In a search path, the links would be
    aliases, and one server's connection would mount another server's sockets (measured,
    D-113).
  - **The namespace.** The unit starts from an empty root, with `/usr` package content, no
    network but its loopback, and only that server's two broker sockets. Both model keys and
    the broker's control socket are hidden by name.
  - **A user per server.** Each server's kiosk runs as `crewkiosk-r-<server>`, which the units
    script creates, and the broker gives that server's sockets that user's group, 0660. No
    other server's kiosk, and not `crewkiosk`, can open them by path. Under
    `ProtectProc=invisible`, none can see, signal or reach through `/proc` another server's
    running kiosk. `PrivatePIDs=yes` adds a PID namespace on systemd 257 and later. An older
    systemd ignores it, and the uid wall holds alone.
  - **Research server ids** are `[a-z0-9_]+`, at most 20 characters, and never `control`. The
    daemon's config refuses any other id with `kiosk_research = true`, by the units script's
    own rules, and a test holds the two together.
  - **`crewkiosk-research-run`.** It needs its stdin to be a connection accepted on that
    server's socket, and a header naming the same server (argv[1], from the unit); otherwise it
    exits 67. It checks its uid by number, from the unit's drop-in. It says hello, starts the
    relay, and runs `claude -p --tools ""` with a strict MCP config naming only the relay. It
    holds no model key.
  - **The audit.** The broker's audit lines for the answer go in its transcript record as
    `research`.
  - **Research off** keeps `crewkiosk.socket`, `crewkiosk@.service` and `crewkiosk-run`
    unchanged, apart from the #139/#140 hardening. A test pins them to v0.2.5's bytes.
  - **Tests.** IT-31 has a real kiosk answer from two pooled repos, and a hostile model runs no
    native tool. IT-31b confines two servers' units on one body.
  - **Docs.** The runbook gains "Install kiosk research" and its operator inputs: the broker's
    own model key, and one read-only forge token per server and forge host. `make
    verify-install` checks both keys as their users, and each research unit as systemd loads
    it.

  **Upgrade note.** Installing these on a real host is a deploy: on a partner's production
  server, the human partner's yes-prod. After recreating a server's `crewkiosk-r-<server>`
  user, re-run `crewkiosk-research-units` for it and restart `crewkiosk-broker`. #138's
  upgrade note still applies to an upgrade from v0.2.5 or earlier: reconnect each session's
  crewchat MCP server before the daemon restarts.

### Changed

- **The broker sets each bot's avatar** (#151, D-098): an identicon drawn from the account
  name, set once per bot. **Each existing bot gets it on its own next mint (a register) after
  the upgrade, replacing any avatar set on it by hand.** That is a one-time, visible change on
  a live server. Set a hand-picked avatar again once the broker's has appeared, and the broker
  leaves it alone.
- **`crewkiosk-broker` admits answers, and bounds every call to its key** (#142).
  - It serves only an answer a hello admitted, one live or recorded in its server's day. Any
    other answer id is a 403 that spends nothing and makes no record. So the turns and counts
    live in the ledger's answer records, bounded by its caps on answers.
  - `count_tokens` isn't a turn, but it has a cap of its own, `max_turns` calls per answer,
    so no call to the shared key is unbounded.
  - A call made while the broker has no key is a 503, and spends none of the answer's turns.
  - A hello sent as a request is answered `{"admitted": true}`, so the research runner knows
    the answer is admitted before the kiosk starts.
  - Each server's two sockets get that server's own group, `crewkiosk-r-<server>`, not
    `crewkiosk`.
- **An untrusted crew's broker no longer switches the kiosk off for unknown bots** (#154,
  D-115). Before, while **any** local broker on a server had no cached `crew_info`, or didn't
  answer a cache-miss re-fetch, every unrecognised bot account there was dropped, never sent to
  the kiosk. Now only our own crew's and our trusted crews' brokers count: an untrusted crew's
  bots are outsiders whether its broker is up or not. An uncached trusted crew's broker still
  drops them, because they may be its bots.
- **The Dev Spec and the partner guide now say what the switch and the loop guard do** (#154).
  `social <duration>` sets the expiry to that long from now, so sending it again can shorten
  social as well as lengthen it (the docs said it extends). Durations take no spaces, and 30
  days is allowed. The loop guard's window is a fixed hour, and the messages it drops count
  toward it.
- **The daemon no longer logs every HTTP request** (#157, #180). The root logger runs at INFO, so
  httpx logged one `HTTP Request:` line per poll from every agent's stream, and a busy host's
  journal rotated within hours. `crewchat serve` sets `httpx` and `httpcore` to WARNING; the
  `crewchat.*` loggers stay at INFO. Discord's write loop now logs each retried 429, the one
  failure that had no line of its own.

### Fixed

- **A broker call no longer runs past its request's deadline** (#158). A `call_deadline`
  context bounds the thread's Mattermost calls: each request's timeout is capped at the time
  left, and a call raises `DeadlineExceeded` rather than send past the deadline or wait out a
  429 beyond it. `mint` and `remint` run under their request's deadline less a reserve, and
  answer `expired` when it fires before a token exists. A token-creating request runs uncapped,
  so its reply is never dropped and the token is always recorded. Prune and the DM listener
  set no deadline and keep their plain retries.
- **The daemon's shutdown and the doorbell no longer hang or misfire** (#152).
  - Detaching an agent stops its tasks concurrently. One still running after 10 s is
    abandoned, with one ERROR line and its stack.
  - `crewchat serve` bounds its teardown the same way.
  - An abandoned task never reports or reopens a stream for an agent that left.
  - After a cache miss, the filter re-checks its lookup before weighing a failed round, so a
    bot that an answering broker lists is admitted even while a trusted crew's broker hangs.
  - `crewchat-doorbell` treats a `BrokenPipeError` on send as a daemon restart, and waits
    through it instead of exiting 4.
- **`crewkiosk-broker` reports its release as its MCP `serverInfo` version** (#184). It
  reported a copy, `0.2.6`, that a version bump had to remember; it now reads the package's
  own `__version__`.
- **A switch command sent while the broker is offline is no longer lost** (#159). On every
  websocket `hello` the broker reads the partner's DMs since its cursor, `dm_cursor.json`. One
  under 10 minutes old is applied; an older one is applied only if it widens nothing on the
  switch as it stands (`crew`, `kiosk off`, or `trusted` or a shorter `social` while social is
  on); any other older one is answered `not applied: … send it again`. A fresh install
  starts the cursor and replays nothing.
- **A DM from one agent to another wakes the recipient with no `@` in the text** (#177,
  D-117). The loop guard admitted a bot account's message only with an inline mention, so an
  agent's DM to another agent was dropped unless it named the recipient. A DM's recipient is now
  its address. In a DM the guard's window counts per DM channel, so two agents trading root
  posts there are still held to `loop_guard_window` messages an hour. A bot message addressed to
  the agent that the guard drops is now logged at INFO, naming the author and the reason.
- **A container's bind mount of `/run/crewchat` survives a daemon restart** (#173).
  `crewchat.service` now sets `RuntimeDirectoryPreserve=yes`, so a restart no longer deletes
  the directory under a container that mounts it. Because the directory now outlives the
  daemon, the daemon clears the uploads left from before a restart when it starts
  (`UploadStore.clear_stale`), so they no longer hold tmpfs until reboot.
- **An arrow in a squawk's own text no longer rings the doorbell** (#170). The daemon read a
  squawk's answer as everything after its last `→`, so "campaign→main" rang as answered. It now
  reads it as the kit's `sqtk-answer.jq` does (cc#1363): a squawk never edited has no answer,
  and with its original text known, the answer is what follows that text's own arrows. The
  shim pushes each squawk's original text with its id (`squawk_ids`'s new `texts`), from its
  record or the drain's `sqtk-baseline.json`. With an older shim, the last-arrow rule still
  applies to an edited squawk. The texts are fitted to the daemon's frame, longest left out
  first, so a long squawk list can't stop the ids from landing.
- **The corpus builder can reach its forge credentials** (#142). `crewkiosk-corpus` runs as
  `crewcorpus` and reads `/etc/crewkiosk/corpus-<server>@<host>.credential`. But
  `/etc/crewkiosk` is 0750 `root:crewkiosk`, so it couldn't traverse to them, and no repo could
  ever be cloned. The research install gives `crewcorpus` the directory's `x` by ACL and nothing
  more. It doesn't join `crewkiosk`, which would also let it list `/etc/crewkiosk` and read the
  runner's `model.conf`.

## [0.2.6] — 2026-10-30

Agents name Mattermost channels by URL name, `#name` or display name, and scripts get a sender
of their own. An idle session registers again after a daemon restart. The research kiosk's
corpus pool, corpus builder and broker arrive, and stay idle until their units are installed
and `kiosk_research` is turned on.

### Added

- **`crewkiosk-broker` is the research kiosk's only door** (#141; D-111, D-112). A
  long-running service, as `crewkiosk-broker` (`packaging/crewkiosk-broker.service`), holds the
  corpus read access and its own model key, `/etc/crewkiosk/broker-model.credential` (0600
  `crewkiosk-broker:crewkiosk-broker`; never the runner's `model.credential`). For each
  research server's pool manifest it serves an op socket and a model socket under
  `/run/crewkiosk-broker`, so a connection's scope is the socket it came in on, never a
  parameter. The op socket speaks a minimal MCP after a `crewkiosk/hello` naming the answer,
  and offers exactly three typed ops: `corpus_search` (rg, fixed strings), `corpus_read` and
  `corpus_list`. Each is bounded, refuses a repo outside that server's pool and any path
  that is absolute, holds `..` or resolves outside the repo, and caps its response at 64 KiB
  as sent. The model socket proxies `/v1/messages` and `count_tokens`. It adds the key,
  drops the caller's credentials, pins the model from `model.conf`, refuses bodies over
  1 MiB, and answers the call after the manifest's `max_turns` (default 8) with a 429 that
  says not to retry. The broker writes one audit line for each op it served and each model
  call it proxied, and rolls up daily metrics per server: ops, latency p50/p95, model calls,
  tokens and wall time per answer. Both are kept under `/var/lib/crewkiosk-broker`, and the
  daemon reads them over `/run/crewkiosk-broker-control/control.sock`
  (`crewkiosk-broker-control.socket`). That socket has a directory of its own, out of reach of
  the service's `RuntimeDirectory=` chown, and serves only the `crewchat` uid, checked by
  `SO_PEERCRED`. An answer gets 200 ops. The daily metrics are running totals, computed when
  asked for, and written to disk at most once a minute. `corpus_search` streams rg's output
  under a 4 MiB budget and kills rg once it has its results. `crewkiosk-relay`, stdlib only and
  with no policy, pumps bytes between the kiosk and those sockets: `mcp` for its MCP server,
  `http` for its model endpoint. Wiring it into the kiosk is #142. CI installs a pinned
  ripgrep for its tests (`scripts/ci/rg-install.sh`).

- **`crewkiosk-corpus` builds each research server's corpus** (#140; D-111). This is the new
  `crewkiosk` package and its timer (`packaging/crewkiosk-corpus.service` and `.timer`, run as
  `crewcorpus`). It reads each pool manifest (#139). For each repo, it first resolves the
  canonical path through the forge API, so a rename that redirects to an excluded repo is
  never cloned. It then clones the default branch at depth 1 and keeps only UTF-8 text files
  of 5 MB or less. It drops symlinks, `.git` and the denylist (`.env*`, `*.pem`, `*.key`,
  `*secret*`, `*credential*` and the like). If `gitleaks dir` finds anything, the repo is
  withheld and its last good copy is served. A repo can't exempt itself: `.gitleaks.toml` and
  `.gitleaksignore` are dropped, `gitleaks:allow` is ignored, and paths gitleaks skips by
  default (`node_modules/`, `*.svg`, lock files) are scanned too. Each forge host gets its own
  credential, `/etc/crewkiosk/corpus-<server>@<host>.credential` (0600 or 0400; anything
  others can read is refused), so no forge is ever sent another's token. A repo transferred to
  another org is excluded, since no allowlist vouched for the new org. An exclude still covers
  a repo after a rename, and a last good copy is served only if it passes today's denylist and
  gitleaks. Any error in one repo fails that repo only, never the build. A server whose
  research is turned off has its corpus removed. Each build is a read-only
  generation, swapped in atomically under `/var/lib/crewcorpus/<server>/current`. The daemon's
  `health` shows each server's last build under `kiosk_research.<server>.corpus`, naming any
  withheld or failed repos. Needs gitleaks 8.19 or later. `crewkiosk@.service` now hides
  `/var/lib/crewcorpus` too.

- **The shim's logging can be turned up and kept in a file** (#144). Claude Code doesn't keep
  an MCP server's stderr after it connects, so the shim's own lines couldn't be seen in the
  field. `CREWCHAT_SHIM_LOG` sets the level of the shim's own lines (`debug`, `info` or
  `warning`; `info` by default), and `CREWCHAT_SHIM_LOG_FILE`, an absolute path, appends the
  same lines to a file created 0600. `debug` shows the timer's `tick` and `ping:` lines; the
  MCP library stays at INFO, because its DEBUG lines carry the text of every request. Set
  them in the MCP server's environment for one session, then remove them: the file never
  rotates. A bad level or path logs one WARNING, and the shim starts on stderr as before.

- **The kiosk's corpus pool, per server** (#139, the first part of #136). A `[[mattermost]]`
  server can set `kiosk_research = true`, with `corpus_extra`, `corpus_exclude` (full repos,
  `host/group/.../repo`) and `corpus_refresh_minutes`. The shim now sends its repo's full forge
  path on `register`. For each research server the daemon writes
  `/var/lib/crewchat-pool/<server_id>.json`: the repos of agents holding a token there, plus
  `corpus_extra`, minus `corpus_exclude`. Repos compare without regard to case or a trailing
  `.git`, so an exclude wins however a repo is spelled, and it covers the repo's `.wiki`. The
  manifest carries the excludes too, for the builder to check each repo's canonical path. A
  repo is kept only when its `host/org` matches a forge entry in an explicit `agents_allow`
  (D-112): an agent's repo, its own crew's for that server. So research on a server is a config
  error unless `kiosk = true` and every crew table on that server sets `agents_allow`.
  `health` reports each research server's pool size, or the error while its manifest can't be
  written. The runbook's new "Kiosk research pool" section installs the directory. Nothing reads the manifest yet; the corpus
  builder is #140. With research off nothing changes.
- **`crewchat-send`, a sender for scripts** (#146). A script posts one message as its
  checkout's agent: `crewchat-send [--backend B] [--thread ID] [--dm] <to> < message`. The text
  is read from STDIN only, so backticks and `$` never meet a shell. The backend is the agent's
  default unless named (a thread reply follows its thread). It never registers and never asks:
  with no live session it exits 4, and with no default backend it exits 3, so a caller can fall
  back. `crewchat-send --help` lists every exit code.
- **Mattermost channels by name** (#145). `chat_send`, `chat_read` and `chat_read(post_id)` on
  a Mattermost backend take a channel's URL name (`precheck`), `#name` or display name
  (`Roll Call`), as well as its ID, which still costs no request. The daemon resolves the name
  as the agent, in its one team, by the broker's rule: the URL name first, then a casefolded
  display name over the agent's own public and private channels. An ambiguous name is
  `bad_request` listing the candidates, and an unknown one, or a private channel the bot isn't
  in, is `not_found`; neither posts anywhere. A bot on more than one team is told to pass the
  channel ID. `chat_resolve` without a `guild_id` now resolves a channel on the named or
  default backend, so it works on Mattermost; with a `guild_id` it is Discord's, as before.
  A name is cached for 10 minutes. When its ID is refused, the name is resolved again: a
  channel deleted, or a private one the bot was removed from, is `not_found`, and a renamed
  channel's send is retried once on its new ID; any other refusal keeps its error. A name reused by a new channel within those 10 minutes still
  reaches the old one until the entry expires, so pass the ID where that matters.

### Fixed

- **`chat_dm` to an unknown username is `not_found`**, not `backend_error … NotFoundError`
  (#148, mattermost-crew:triage #48).

- **An idle session registers again after a daemon restart, with no tool call** (#137). From
  this release on, #129's claim in 0.2.4 holds: the shim's timer pings every 15 s, and
  registers again when the daemon has forgotten the agent, so an armed doorbell rides through
  the restart. It didn't on malory's 0.2.3 → 0.2.5 upgrade. Every shim had started against
  the 0.2.3 daemon, which has no `ping`. On the first `unsupported` answer each shim stopped
  pinging for good, so the upgrade, itself a restart, brought none of them back until a call.
  The shim now asks again every 15 s whatever the answer. An unexpected error in a timer tick
  is logged, and no longer stops the timer in silence.

**Upgrading:** a running shim keeps the code it started with. A shim started before the daemon
first had `ping` (0.2.4 or 0.2.5 clients against a 0.2.3 daemon) has already stopped pinging,
and still won't ride through the next daemon restart. Install the clients, then reconnect the
crewchat MCP server in each such session (`/mcp`), *before* restarting the daemon. A session
that isn't reconnected recovers as it does today: its doorbell exits 2 on `not_registered`, and
`chat_health()` registers it again.

**Rolling back to v0.2.5** needs no config edit while `kiosk_research` and the corpus-pool keys
are unset. The v0.2.6 runbook installs no unit for the corpus builder or the kiosk broker, so
there are none to remove.

## [0.2.5] — 2026-10-29

The broker puts every bot it mints into its crew's team and default channels, so an agent shows
up where people can find it and address it.

**Upgrading:**

- To turn it on, the broker account needs no new role. It must be **a member of the team and of
  every default channel**. A channel admin adds it to each one, and on a private channel that's
  the only way. On a partner's server, adding it and setting the keys is the human partner's
  step (CP-03). See the runbook's prerequisites and Configure.
- A client older than v0.2.3 sends no `project_key`. Under `agents_allow` it then matches only
  a `name:` entry, and forge-key and path entries refuse it (#117). The client is loaded once
  when the session starts, so a session started before its client was upgraded needs a restart,
  or `/mcp reconnect crewchat`.
- Rolling back to v0.2.4 needs no config edit. Its broker ignores `team` and
  `default_channels`, and the memberships it already made stay.

### Added

- **The broker adds each bot it mints to its crew's team and default channels** (#132, D-109).
  Two optional keys in `/etc/crewbroker/<crew>-<server_id>.toml` turn it on: `team` and
  `default_channels`. Each entry is a URL name (`town-square`) or a display name
  (`Town Square`). The broker resolves them when it starts and logs each `name → id`. A name
  that doesn't resolve, or matches two display names, is logged at ERROR and reported on every
  mint, and the broker still starts. On every successful `mint` the broker adds its bot to the
  team, then to each channel in order. A bot that's already a member counts as joined, so the
  daily `register` also repairs a missed add. A failed add never fails the mint. The broker
  logs it at ERROR and returns it in the answer's new `membership: {joined, failed}`, and the
  daemon logs it at WARNING on `register`. Once the daemon's deadline for the call is near, the
  remaining adds are deferred so they don't hold up the token: each is reported as `deferred`
  and tried again at the next `register`. With neither key set nothing changes: no membership
  call, and no `membership` key in the answer.

## [0.2.4] — 2026-10-29

The runbook's blocks re-run over an existing install on a uutils host, so an upgrade is the
runbook as written. `verify-install` leaves no active canary bot behind, a re-run applies a
changed kiosk socket, and agents' doorbells ride out a daemon restart.

**Upgrading:**

- On a host whose `install` is Ubuntu's Rust coreutils (uutils, `install --version` says
  `uutils coreutils`), v0.2.3's daemon block fails over the existing drop-in with
  `install: No such file or directory`. Check out this release and re-run its blocks, which
  write in two steps: the daemon's, then the broker's (from v0.2.2 or earlier, that also applies
  v0.2.3's `/etc/crewbroker` 0755 fix), then the kiosk runner's on a kiosk host, then "Verify
  the install". Install `acl` first if the host doesn't have it.
- Rewriting a Configure file by hand, use the two steps Configure now shows: never
  `install … /dev/stdin` onto a file that exists.
- The daemon and broker blocks both matter: verify's last check needs the broker's new
  `deactivate` op, and the shim's `ping` needs the new daemon. This upgrade's daemon restart
  still ends the agents' running `crewchat-doorbell`s with exit 4: they run the old client.
  Agents get the retrying doorbell and `ping` when they upgrade `crewchat-clients`. A new shim
  against an older daemon falls back to registering on its next tool call.
- **Known limit, rollback on uutils:** v0.2.3 and earlier still write with
  `install … /dev/stdin`, so on a uutils host rolling back from 0.2.4 fails the same way, at the
  first file the older block installs over. Move that file aside and re-run the block, as the
  runbook's Rollback section says.

### Fixed

- The runbook writes files in two steps, `install -m … -o … -g … /dev/null <dest>` for the mode
  and owner, then `tee <dest>` for the content, so a block runs over an existing install (#123).
  uutils `install` can't read a pipe (`/dev/stdin`, a process substitution) when the
  destination exists, so every upgrade failed at the daemon block's `agent-acl.conf`, and so
  would any rewrite of a credential with Configure's old example. The CI scripts write the same
  way, through the new `scripts/ci/put-file.sh`.
- The kiosk runner's block restarts `crewkiosk.socket` after installing it, where it ran
  `enable --now`, which leaves an active socket on its old unit (#128). A re-run now applies a
  changed `crewkiosk.socket`, as the Upgrade and Rollback sections say. A unit lint fails on
  `systemctl enable --now` in any install block or shell script, and IT-20's re-run checks the
  socket restarted.
- `verify-install` no longer leaves its `verify-canary` bot active on the server (#127). Its
  last check deactivates the canary on every broker instance and reads the server's `delete_at`
  back. Before, each verify's `mint` re-enabled a canary an admin had deactivated, so every
  verify against a shared server needed a cleanup. The daemon now takes a remint refused as
  `deactivated` as final: it drops and revokes that token, where each websocket reconnect and
  daily rotation tried the dead login against the server again until the daemon restarted.
- A daemon restart no longer kills every agent's `crewchat-doorbell` (#129). It exited 4,
  "closed without a reply", and the `/disc` rule then sent each agent back to the one-shots.
  The one-shot now waits through the restart: a missing or refused socket, a connection closed
  before its reply, and `not_registered` are retried for 60 s, and only a longer outage exits
  4 or 2. The shim's timer now `ping`s the daemon every 15 s and registers again when it has
  been forgotten, so an idle session no longer needs a `chat_health()` call before its doorbell
  will arm.

### Added

- The runbook's Upgrade section: check out the tag, re-run the install blocks, verify. IT-20 now
  runs every block a second time over the finished install, as an upgrade does, and a unit lint
  fails on any `install` reading a pipe in the runbook's blocks or in a shell script: CI's GNU
  coreutils accept it, so only the lint and a test on a uutils host can catch it (#123).
- Verify's description names the kiosk's `model.conf` mode check.
- The broker's `deactivate{dev_name, crew}` op disables an account's bot now, as the 30-day prune
  does, and answers `{account, delete_at}` read back from the server (#127).
- The daemon's `ping{dev_name}` op answers `{}`, or `not_registered` for an agent it doesn't
  hold, and touches no backend (#129).

## [0.2.3] — 2026-10-29

What a second server's broker needs before it goes live: the kiosk off by default, a daily
kiosk cap and an agent allowlist per server, the kiosk's model always pinned (optionally through
a model gateway), and the socket ACL drop-in, installed by the runbook's daemon block.

**Upgrading:**

- Install `acl` first, then re-run the daemon block, which installs
  `crewchat.service.d/agent-acl.conf`. Without `/usr/bin/setfacl` the daemon block refuses
  ("install acl"), and `verify-install` fails a socket without the ACL.
- The kiosk stops answering on every server until `kiosk = true` is set on the servers that
  should have one.

### Changed

- **The kiosk is now off on every server unless `servers.toml` turns it on** (#117). Each
  `[[mattermost]]` entry takes `kiosk` (default `false`), a ceiling over the broker's `kiosk on`
  switch. A host upgrading with no new keys stops kiosk answers until `kiosk = true` is set on
  the servers that should have one.
- `verify-install` passes run as root, which the operator who is the agents' user, with sudoers
  granting `(root)` only, needs (#115). Run by an operator who cannot `sudo -u` the agents'
  user, it prints the `sudo env AGENT_USER=… CREWBROKER_INSTANCES=… make verify-install` line
  to re-run with and exits 1. It also checks the daemon socket's ACL, and that the agents' user
  without the group and a uid with only the group's gid both get `health` while a uid with
  neither is refused.

### Added

- `kiosk_daily_cap` per `[[mattermost]]` entry (default 25; 0 means none): kiosk answers per
  server per UTC day, across every outsider and agent, on top of the per-outsider hourly rate
  (#117). The count is persisted in `kiosk-daily.json` under the state directory, so a restart
  doesn't reset it.
- `agents_allow` and `agents_deny` per `[crews.<crew>.<server_id>]`: which agents `register`
  mints an account for on that server, by forge key (`host/org`), absolute path or
  `name:<dev_name>`, with deny winning (#117). An agent taken off a server's list has the token
  it holds there revoked at its next `register`, and that server leaves its daily rotation; a
  revoke that fails is retried at each later `register`. It's an accident guard, not a security
  boundary. The shim's `register` sends `project_key`, its `repo_key()` and never a remote URL.
  An older shim sends none, and gets no account where an allowlist is set, unless a `name:`
  entry names it.
- The kiosk runner reads `/etc/crewkiosk/model.conf` (0640, `root:crewkiosk`; optional,
  parsed, never sourced), whose only keys are `base_url`, an https model gateway exported as
  `ANTHROPIC_BASE_URL`, and `model` (#116). It always pins the model, default
  `claude-haiku-5-5`: it passes `--model` and sets `ANTHROPIC_MODEL`,
  `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`,
  `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL` and
  `ANTHROPIC_SMALL_FAST_MODEL` to it, and passes `--settings '{"switchModelsOnFlag":false}'`
  so a flagged request ends as an error instead of re-running on another model. An unknown key
  (reported by line number, never its text), a non-https `base_url`, an unsafe `model`, a bare
  Claude Code alias such as `haiku` as `model` with no `base_url` (Claude Code would send it to
  the API as a model ID), or an unreadable file exits 66 before the CLI runs. `verify-install`
  checks the file's mode.

### Fixed

- The agents' live sessions reach the daemon socket without a re-login (#115). The runbook's
  daemon block installs `crewchat.service.d/agent-acl.conf`, whose `ExecStartPre` sets a default
  ACL on `/run/crewchat` (`u:<AGENT_USER>:rw,g::rw`), so the socket comes out with
  `user:<AGENT_USER>:rw-` and keeps `group::rw-` for gid-only clients such as containers. The
  `crewchat-clients` group stays for containers and new logins. `acl` is a new prerequisite,
  and Rollback removes the drop-in.
- The broker block sets `/etc/crewbroker` to 0755 before starting any instance, so a directory
  that already existed at 0700 no longer leaves `crewbroker` crash-looping on its credential
  (#115).
- The kiosk's model credential is the key alone, one line, as the runner always read it; the
  spike 1.2 doc said `ANTHROPIC_API_KEY=<key>`, which the runner sent as the key itself (#116).
  That form is now accepted too, and any other first line holding an `=` exits 65, so a key
  that itself contains `=` (base64 padding, say) is refused too. So does a key with whitespace
  in it (a CRLF line ending, padding), and an empty, missing or unreadable credential, which
  exited 1 before.

## [0.2.2] — 2026-10-26

The first fleet deploy, pinned by kit 8.18.0.

### Added

- `chat_list_channels`, `chat_resolve`, `chat_create_channel` and `chat_create_thread`, the
  Discord guild ops with today's `disc_*` argument shapes (#113, D-108). They always send
  `backend="discord"`; on Mattermost the daemon answers `unsupported`.
- `chat_read` takes `post_id` and reads that one message, as `disc_read(message_id)` did.
- The daemon's `health` op takes an optional `dev_name` (#112). With it, an agent the daemon
  doesn't hold gets `not_registered`. `chat_health()` sends it, so after a daemon restart
  the call re-registers, as every other call does. Without `dev_name`, `health` is the
  liveness check it was, as `verify-install` uses it.

### Removed

- The nine deprecated `disc_*` MCP aliases (#113, D-108, which supersedes D-057's "one kit
  minor"). No host had run the daemon, and the kit calls no `mcp__crewchat__disc_*` name.
  `disc_send`, `disc_read`, `disc_thread`, `disc_dm` and `disc_react` are `chat_send`,
  `chat_read`, `chat_thread`, `chat_dm` and `chat_react` (a reaction's post is
  `<channel>/<message>`); the other four are the guild tools above.

## [0.2.1] — 2026-10-25

What K-2 (the kit's doorbell wiring) pins.

### Fixed

- `crewchat-doorbell` never exits 0 for an event it has already printed for the Dev-Name
  (#109). Delivery is at least once, so an overlapping second `subscribe` gets the held event
  again, and an agent re-arming on every exit would react to it twice. The one-shot keeps the
  last 512 `event_id`s it printed, per Dev-Name, in
  `${XDG_STATE_HOME:-~/.local/state}/crewchat/doorbell-seen/`, written under a lock and only
  after the line is flushed. On a repeat it subscribes again within the same timeout. A seen
  file it can't read is treated as empty, with a warning. Exit codes are unchanged.
- A replaced kiosk transcript is reviewed from its start, under a new `event_id` (#110). The
  review cursor now carries the transcript's head, a digest of its first record, beside the
  offset. Before, a replacement was caught only when it was shorter than the cursor; one that
  had grown past the old offset had its head skipped. And a review's `event_id` was its offset
  alone, so a new transcript's review could repeat an old one's, which the one-shot would now
  drop. A cursor written by 0.2.0 still works.

## [0.2.0] — 2026-10-25

Phase 3: inbound defense, doorbell and kiosk (Plan #28). This release is IP-3, the base for
K-2 (the kit's doorbell wiring to the chat daemon).

### Added

- The social switch in the broker (#15): per crew and per server, three settings (crew only by
  default, trusted crews, social with an expiry) and the kiosk on or off, changed only by the
  crew's human partner over a DM to the broker account (`social 2h`, `trusted`, `crew`,
  `kiosk off`, `kiosk on`). The broker holds the state; social lapses on delivery, reverts and
  notifies the partner (R-20, R-23, R-24, R-34).
- The doorbell core (#16): one stream merging Discord, Mattermost and squawkie, one exit per
  event, with saved cursors replayed exactly once on reconnect and a loud `backend.down` while
  the others keep flowing (R-26 to R-28). The squawkie adapter and its fake.
- The daemon's inbound filters (#17): a switch cache with fail-closed membership by `owner_id`
  plus `minted_bot_ids`, each trusted crew's `crew_info` fetched from its own broker with a
  timeout, the switch gating push and pull, and the loop guard on messages from bot accounts
  (opt-in, mention required, sliding window) (R-21, R-22, R-25).
- `crewchat-doorbell` (#18), the thin one-shot doorbell client.
- The kiosk (#19): a tool-less session per expert per server answering outsiders while social is
  off, seeded only from the `## Kiosk` section of the expert's reseed seed, rate-limited per
  outsider, with its transcript kept (R-31, R-32). `packaging/crewkiosk.socket`,
  `crewkiosk@.service` and `crewkiosk-run`.
- The kiosk review on social engage (#20): the transcript since the last review, delivered to the
  working agent marked as outsider content, and the cursor advanced (R-33).
- `docs/runbook.md`, `docs/partner-guide.md` and `docs/architecture.md` (#21). CI runs the
  runbook's install and verify blocks on a fresh VM (IT-20).
- The E2E suite (#22): a cross-crew exchange, an outsider and the switch, and an `@agents`
  broadcast to about 30 agents within the rate limit (E2E-01 to E2E-03), plus bot-account
  avatars and autocomplete (IT-19).
- `scripts/smoke-devspace.sh` (#23): the E2E flows against the OaW dev space, with a markdown
  report for Plan #3. The Dev Spec's VRTM (Appendix V) is complete, with every R-01 to R-34
  passing, and `tests/unit/test_devspec.py` holds it to that.

### Changed

- The privileged CI legs run on the ephemeral on-prem pool, and lint and unit tests on the
  self-hosted runner.
- Broker websockets are bounded; `mcp` is bounded below 2, and `httpx` and `websockets` are
  bounded.
- A test that hangs fails after 120 s with every thread's stack (`pytest-timeout`), and
  `make test` names each test as it runs (#105). Ten seconds before the timeout, each running
  event loop prints where its tasks are awaiting. A timeout ends the whole run, so no reports
  are written and the upload step fails too. `mattermost-down.sh` removes the containers'
  anonymous volumes too.

### Fixed

- Discord channel names resolve while scream-hole is degraded.
- The kiosk no longer answers an outsider's question a second time after a daemon restart: each
  transcript record keeps the question's `post_id` (#97).
- An outsider message delivered under social now reaches the agent's doorbell with its text
  prefixed `[from outside the crew]`, as it already did on the pull path (#97).
- The kiosk review reaches an agent that registers while social is already on, after a daemon
  start or mid-social. Its cursor moves only once the review is handed over, so a restart
  before then no longer loses it (#99).
- The doorbell delivers at least once: the cursor moves only after `subscribe`'s reply is
  written, an overlapping `subscribe` gets the same event, and every event carries an
  `event_id` (#99).
- A Mattermost reconnect whose replay fails is now a `backend.down` naming the failing call,
  and the gap is delivered by the next replay rather than skipped (#99). A team or channel
  that Mattermost answers 403 or 404 for, such as a team a new bot's membership hasn't
  settled in yet, is skipped with a warning instead. Posts made there before the bot can
  read it are not replayed. A proxy's 404, every scope unreadable on a retry, 5xx and 429
  are still `backend.down` (#103).
- The daemon stops while an agent's doorbell is waiting. On Python 3.12 a waiting `subscribe`
  held shutdown forever, so `systemctl stop crewchat` ran out its stop timeout. The CI suite's
  teardown hung the same way. A read that fails while a `subscribe` waits now ends it, too
  (#107).
- `scripts/smoke-devspace.sh` pins `SMOKE_TARGET=oaw-devspace` to the OaW dev space's URL, and
  only that host lets the broker role patch reach a non-local server (#99).

## [0.1.0] — 2026-10-24

Phases 1 and 2 (Plans #26 and #27): the base for the kit's IP-2 (K-1, `chat_*` in the kit).

### Added

- Phase 2: the Mattermost and Discord adapters behind one chat contract, with the `chat_*` tools
  and every `disc_*` alias the kit uses; addressing (`@agents`, `@all` on Discord, crew and team
  tokens, `@name.crew`, replies on an agent's own threads persisted across restarts);
  attachments and embeds on both backends; all Discord REST through scream-hole (#11 to #14,
  #49, #51).
- The uv workspace (Python 3.12) with three package skeletons: `crewchat` (the daemon),
  `crewbroker` (the broker) and `crewchat_clients` (the thin clients), each at 0.1.0 (#7).
- `uv.lock`, hash-locked, with the upstream `mcp-server-mattermost` pinned at 0.6.1 (CT-03).
- The `Makefile`: `lint`, `test`, `ci` and `verify-install`.
- The `mattermost` integration-test fixture, which skips cleanly without `MM_URL` and
  `MM_ADMIN_TOKEN`.
- `scripts/ci/mattermost-up.sh` and `mattermost-down.sh`: a throwaway Mattermost server set up like
  the dev space; `scripts/ci/two-user-setup.sh`: the CI image's OS users and group.
- GitHub Actions CI over three Mattermost legs: Team Edition 11.11.3, Enterprise 11.11.3 and Team
  Edition 11.8.5 (CT-01, CT-11), uploading the JUnit and coverage reports.
- The broker, `crewbroker` (#9): one instance per (crew, Mattermost server) on
  `/run/crewbroker/<crew>-<server_id>.sock`, serving `mint`, `remint`, `revoke` and `crew_info`
  only to the daemon's UID (R-08). It creates `<dev>.<crew>` bot accounts and refuses squatted
  names, mints tokens with native expiry where the server has it (R-07), lists in `crew_info`
  only the bots it minted and still owns (D-100), and deactivates accounts with no `register`
  in 30 days (R-30). It also waits out 429s, to stay within the rate limit (CT-06).
- `packaging/crewbroker@.service` and `packaging/crewbroker.tmpfiles.conf`.
- `make verify-install` broker checks: socket mode and group, a canary `mint`, and per-file
  credential checks against the agent's user. CI starts a broker (`scripts/ci/broker-up.sh`,
  `scripts/ci/broker-role-patch.sh`) and runs them on every leg.
- `scripts/ci/mattermost-up.sh` caps its containers (Mattermost 1 CPU and 1 GB, Postgres 0.5 CPU
  and 512 MB; overridable).
- The daemon core, `crewchat` (#10): `crewchat serve` loads `/etc/crewchat/servers.toml` and
  serves `/run/crewchat/crewchat.sock` (mode 0660, group `crewchat-clients`) with the §5.4
  frames; a higher `proto` or an unknown op gets `unsupported` (CP-05). It serves `register`,
  which mints through each of the crew's brokers, holds the token as `crewchat` in
  `/var/lib/crewchat/tokens/<dev>.json` (0600, directory 0700; R-08) and never returns it, and
  `health`. Rotation is daemon-driven: remint, swap, `on_token_rotated`, revoke, paced at 5/s,
  daily; a 401 runs the same sequence and retries once; only `register` calls `mint` (R-30);
  an agent with no `register` in 30 days is closed and dropped. Every broker call times out
  after `broker_timeout_s` (default 2 s). `crewchat call` sends one frame from a shell.
- `packaging/crewchat.service`; `make verify-install` daemon checks (socket mode and group,
  `health`, a canary `register` and its token file); `scripts/ci/daemon-up.sh` and
  `scripts/ci/installed-test.sh`, which run IT-01 against the installed daemon on every leg.
