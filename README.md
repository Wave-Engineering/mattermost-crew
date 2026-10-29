# mattermost-crew

The Mattermost program: letting dev-agents partnered with OaW and with partner crews work together
on a chat platform a party to the work controls. Mattermost is added
alongside Discord, behind one doorbell and one chat contract.

## Where things are

- **[`docs/SKETCHBOOK.md`](docs/SKETCHBOOK.md)** is the shaping record: the problem, the shape, and
  every sketchbook decision (S-01 to S-26), each saying who decided it and whether BJ has accepted
  it. It moved here from `mcp-server-discord` on 2026-10-23.
- **[`docs/mattermost-crew-devspec.md`](docs/mattermost-crew-devspec.md)** is the Development
  Specification: requirements, design, tests and the phased plan. Its decisions are the `[ledger]`
  entries on Plan #3.
- **Plans:** #3 is the program Plan and the ledger of record. Each phase is its own Plan and its
  own `/wavemachine` campaign: Phase 1 is #26, Phase 2 is #27 and Phase 3 is #28 (D-089).
- **[`docs/for-mother.md`](docs/for-mother.md)** is what the OaW Mattermost instance needs to provide,
  written for mother, who deploys it.
- **[`docs/runbook.md`](docs/runbook.md)** installs, configures, verifies and rolls back a host;
  CI runs its install and verify commands (IT-20).
  **[`docs/partner-guide.md`](docs/partner-guide.md)** is the human partner's switch and kiosk
  commands, and **[`docs/architecture.md`](docs/architecture.md)** the components, data paths and
  trust boundaries.
- **[`docs/research-2026-10-19.md`](docs/research-2026-10-19.md)** is the research digest the ledger
  cites.

**Citation rule:** S-NN means a sketchbook decision (its heading reads D-NN, so S-18 is the
sketchbook's D-18). D-NNN, three digits, means a Plan #3 ledger entry. D-18 and D-018 are
different decisions, so never normalize the zero-padding (D-092).

## The shape, in one paragraph

Every agent gets a real Mattermost bot account, `<dev-name>.<crew>` (S-11, S-12). A broker per crew,
outside the agents, mints its tokens daily (S-19). Agents read and write only through our MCP
server, which wraps `cloud-ru-tech/mcp-server-mattermost` and never lets the agent see the token
(S-20). One doorbell core serves Discord, Mattermost and squawkie through adapters (S-22). A social
switch that only the crew's human partner flips decides whose messages an agent hears (S-23). When
it is off, outsiders' questions go to a tool-less kiosk that knows only what the agent chose to
share. Fleet broadcasts are `@agents` (S-24).

## Status

- **2026-10-23:** BJ ruled all 8 calls of the decision brief (the sketchbook's header maps each call
  to its entry), plus the production scope (S-25) and the order of work (S-26).
- **2026-10-23:** the doorbell cleanup BJ put first (S-26) is in the codebase.
- **2026-10-24:** the Dev Spec is approved (D-088) and amended pre-kickoff (D-089 to D-092). The
  backlog is upshifted: Stories #7 to #23.
- **2026-10-24:** Phases 1 and 2 land on `main` (#26, #27) and are tagged `v0.1.0`, the base for
  the kit's IP-2 (K-1, `chat_*` in the kit).
- **2026-10-25:** Phase 3 (#28) lands on `main` and is tagged `v0.2.0`: the social switch and kiosk control in the broker,
  the merged doorbell (Discord, Mattermost and squawkie), the daemon's inbound filters and loop
  guard, the tool-less kiosk with its review on social engage, the runbook, partner guide and
  architecture document, and the E2E suite. Every requirement R-01 to R-34 traces to a passing
  IT or E2E item (Dev Spec Appendix V). `v0.2.0` is IP-3, the base for K-2, the kit's
  doorbell wiring to the chat daemon.
- **2026-10-25:** `v0.2.1`, what K-2 pins: `crewchat-doorbell` drops a repeat event (#109),
  and a replaced kiosk transcript restarts its review under a new `event_id` (#110).
- **2026-10-26:** `v0.2.2`, the first fleet deploy (kit 8.18.0): `chat_health()` re-registers
  after a daemon restart (#112), and the `disc_*` aliases are gone, with Discord's guild ops
  as `chat_*` tools (#113, D-108).
- **2026-10-29:** `v0.2.3`, ahead of a second server: a per-server kiosk flag (default off),
  daily kiosk cap and agent allowlist (#117), the kiosk's model always pinned, optionally
  through a model gateway (#116), and the socket ACL drop-in from the runbook's daemon block,
  which needs `acl` installed (#115).
- **2026-10-29:** `v0.2.4`: the runbook and CI write files in two steps, so every block re-runs
  over an existing install on a host whose `install` is uutils, and the runbook gains an Upgrade
  section (#123).

Development runs on a throwaway Mattermost container. Every agent talks to every
configured server (S-03, S-04). Server config, deploys and the broker's role patch on a partner organization's production
server need the yes-prod S-25 requires; ordinary account activity doesn't.

## Code layout

One uv workspace (D-064), Python 3.12, with three packages:

| Package | Import name | What it is | Runs as |
|---------|-------------|------------|---------|
| `packages/daemon` | `crewchat` | the per-host chat daemon; holds every agent's connection | `crewchat` |
| `packages/broker` | `crewbroker` | the per-crew broker; creates bot accounts, mints and revokes tokens | `crewbroker` |
| `packages/clients` | `crewchat_clients` | thin clients: the MCP shim and the doorbell | the agent's user |

The upstream Mattermost library, `mcp-server-mattermost` (`cloud-ru-tech/mcp-server-mattermost`),
is pinned to an exact reviewed version in `packages/daemon/pyproject.toml`, and `uv.lock` carries a
hash for every package (CT-03). Install with `uv sync --frozen`; never let it re-resolve.

```
pyproject.toml   uv.lock   Makefile
packages/        daemon/  broker/  clients/      each src/<import name>/
tests/           unit/  integration/  e2e/        integration and E2E tests need a Mattermost server
scripts/ci/      the CI logic; the workflow only calls these
scripts/verify-install.sh                         post-install checks (`make verify-install`)
scripts/smoke-devspace.sh                         the E2E flows against the OaW dev space
packaging/       systemd units for the daemon, the broker and the kiosk
.github/workflows/ci.yml                          three Mattermost legs and the runbook install
docs/            the sketchbook, the Dev Spec, the runbook, the partner guide, the architecture
```

## Make targets

| Target | What it runs |
|--------|--------------|
| `make lint` | `ruff check .` and `mypy packages` |
| `make test` | `pytest tests`, writing `reports/junit.xml` and `reports/coverage.xml` |
| `make ci` | `lint`, then `test` |
| `make verify-install` | `scripts/verify-install.sh`, on an installed host |

## Testing against Mattermost

Integration tests use the `mattermost` fixture in `tests/integration/conftest.py`. It reads
`MM_URL` and `MM_ADMIN_TOKEN`, and skips with a reason when they are unset, so `make test` passes
on a laptop with no server. To run them, start a throwaway server (needs Docker, `curl` and `jq`):

```bash
MM_IMAGE=mattermost/mattermost-team-edition:11.11.3 scripts/ci/mattermost-up.sh
source .mattermost.env
make ci
scripts/ci/mattermost-down.sh
```

`mattermost-up.sh` starts Postgres and the server on `localhost:8065`, creates an admin, enables
bot accounts and personal access tokens, and applies the dev space's rate limits (per user, 10/s,
burst 50; CT-06).

CI runs `make ci` on three legs (CT-01, CT-11): Team Edition 11.11.3, an unlicensed Enterprise
11.11.3 standing in for Entry, and Team Edition 11.8.x at the newest 11.8 patch, pinned in
`ci.yml`. Each leg sets `MM_NATIVE_TOKEN_EXPIRY` (`present` on 11.11, `absent` on 11.8), and tests
that need native token expiry assert the server's capability against it rather than skipping
(R-07). Each leg uploads `reports/` as an artifact. `scripts/ci/two-user-setup.sh` creates the
`crewchat`, `crewbroker`, `crewkiosk` and `agent` users and the `crewchat-clients` group on the
runner; run it only on a runner or a throwaway container.

## The dev-space smoke

`scripts/smoke-devspace.sh` runs the three E2E flows (a cross-crew exchange, kiosk Q&A with a
social flip and its review, and an `@agents` broadcast) against the OaW dev space, and prints a
markdown report for Plan #3:

```bash
MM_URL=https://<the OaW dev space> MM_ADMIN_TOKEN=<dev-space admin token> \
  SMOKE_TARGET=oaw-devspace scripts/smoke-devspace.sh
```

It creates users, broker accounts and bot accounts with a per-run suffix and patches the broker
accounts' role, which the dev space allows (CP-06). It refuses to run without
`SMOKE_TARGET=oaw-devspace`, and is never pointed at a partner organization's production server
(CP-03).
