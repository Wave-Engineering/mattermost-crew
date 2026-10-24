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
