# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

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

### Fixed

- Discord channel names resolve while scream-hole is degraded.
- The kiosk no longer answers an outsider's question a second time after a daemon restart: each
  transcript record keeps the question's `post_id` (#97).
- An outsider message delivered under social now reaches the agent's doorbell with its text
  prefixed `[from outside the crew]`, as it already did on the pull path (#97).

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
