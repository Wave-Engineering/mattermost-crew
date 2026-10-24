# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

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
