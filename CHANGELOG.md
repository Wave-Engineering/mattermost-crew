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
