# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

**Upgrading:**

- On a host whose `install` is Ubuntu's Rust coreutils (uutils, `install --version` says
  `uutils coreutils`), v0.2.3's daemon block fails over the existing drop-in with
  `install: No such file or directory`. Check out this release and re-run its blocks, which
  write in two steps: the daemon's, then the broker's (from v0.2.2 or earlier, that also applies
  v0.2.3's `/etc/crewbroker` 0755 fix), then "Verify the install". Install `acl` first if the
  host doesn't have it.
- Rewriting a Configure file by hand, use the two steps Configure now shows: never
  `install … /dev/stdin` onto a file that exists.

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

### Added

- The runbook's Upgrade section: check out the tag, re-run the install blocks, verify. IT-20 now
  runs every block a second time over the finished install, as an upgrade does, and a unit lint
  fails on any `install` reading a pipe in the runbook's blocks or in a shell script: CI's GNU
  coreutils accept it, so only the lint and a test on a uutils host can catch it (#123).
- Verify's description names the kiosk's `model.conf` mode check.

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
