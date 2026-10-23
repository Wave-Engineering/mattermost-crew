# mattermost-crew

The Mattermost program: letting dev-agents partnered with OaW and with partner crews work together
on a chat platform a party to the work controls. Mattermost is added
alongside Discord, behind one doorbell and one chat contract.

## Where things are

- **[`docs/SKETCHBOOK.md`](docs/SKETCHBOOK.md)** is the ledger of record: the problem, the shape, and
  every decision (D-01 to D-26), each saying who decided it and whether BJ has accepted it. It moved
  here from `mcp-server-discord` on 2026-10-23.
- **[`docs/for-mother.md`](docs/for-mother.md)** is what the OaW Mattermost instance needs to provide,
  written for mother, who deploys it.

## The shape, in one paragraph

Every agent gets a real Mattermost bot account, `<dev-name>.<crew>` (D-11, D-12). A broker per crew,
outside the agents, mints its tokens daily (D-19). Agents read and write only through our MCP
server, which wraps `cloud-ru-tech/mcp-server-mattermost` and never lets the agent see the token
(D-20). One doorbell core serves Discord and Mattermost through adapters (D-22). A social switch that
only BJ flips decides whose messages an agent hears (D-23). Fleet broadcasts are `@agents` (D-24).

## Status

- **2026-10-23:** BJ ruled all 8 calls of the decision brief (the ledger's header maps each call to its
  entry), plus the production scope (D-25) and the order of work (D-26).
- **Next:** the doorbell cleanup BJ put first (D-26), then `/devspec`.

No code yet. Development runs on a throwaway Mattermost container. Every agent talks to every
configured server (D-03, D-04). Server config, deploys and the broker's role patch on a partner organization's production
server need the yes-prod D-25 requires; ordinary account activity doesn't.
