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
- **Next:** hailing-frequencies runs `/wavemachine` on #26 first.

No code yet. Development runs on a throwaway Mattermost container. Every agent talks to every
configured server (S-03, S-04). Server config, deploys and the broker's role patch on a partner organization's production
server need the yes-prod S-25 requires; ordinary account activity doesn't.
