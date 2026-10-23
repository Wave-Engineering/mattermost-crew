# Mattermost Support: Sketchbook

Shaping record for adding Mattermost to the chat layer, so that dev-agents can collaborate
across crews and across organizations. It was started 2026-10-17 from a conversation between
BJ and babelfish (oaw), and follows the `/muse` sketchbook contract: the problem, the shape,
an append-only decision ledger, and an open-questions register. `/ddd` and `/devspec` extend
this file; they don't rewrite it.

**Moved here 2026-10-23** from `Wave-Engineering/mcp-server-discord` `docs/SKETCHBOOK.md` (added in
PR #76; copied from `origin/main` at `0083024`). This copy is the ledger of record; the old one is a
pointer. D-01 to D-17 are as they were there, except for ruling notes appended under the entries
BJ has since ruled on.

**Roadmap slot:** after mcp-server-nerf#32 (BJ). That slot arrived on 2026-10-23: nerf, the
precheck gate split, sqtk and `/onboard` have all shipped. **Next step (2026-10-23):** finish the
doorbell cleanup that BJ put first (D-26), then `/devspec` this program. BJ ruled all 8 calls of
the decision brief on 2026-10-23: call 1 is D-03's note, 2 is D-19, 3 is D-20 (which revises D-14),
4 is D-22, 5 is D-21, 6 is D-13's note, 7 is D-24 and 8 is D-23. D-25 (the prod scope) and D-26 (the
order of work) were ruled the same day. D-18 dates from 2026-10-18.

---

## 1. The problem (confirmed by BJ)

Some of BJ's teammates at a partner organization use our workflow. The dev-agents partnered
with them and with us
can't work together today, because there is no common platform that both organizations can use
with their data. (Below, *partner-org* means that organization; a bare *partner* is an
agent's human partner.)

> "My teammates at [the partner-org]...some of them use our workflow. When we get a common
> platform, all our agents can collaborate!!!" (BJ)

There is a second problem underneath: **we don't control data on Discord.**

> "the problem isnt that we need different systems, it is that we don't control data on
> discord. If we stood up an OaW mattermost, this stops being a problem. We already
> (correctly) traffic in [partner-org] data." (BJ)

Discord is a third-party host. Today every agent's chatter goes there, including chatter from
agents doing partner-org work. Mattermost is self-hostable, so the data stays on infrastructure
that a party to the work controls.

## 2. The shape

The chat layer grows from "Discord" into **a chat server with backend adapters**: Discord, and
one or more Mattermost instances, all behind one tool contract. Each backend is a configured
server instance, not a hard-coded pairing.

- **Every agent monitors every configured backend.** The watcher merges them into one doorbell
  stream with one addressing model.
- **Sending has a default and can be overridden.** An agent working on a partner-org project
  posts to Mattermost by default. An agent can always choose a different backend explicitly,
  and a reply goes back where the thread lives.
- **On Mattermost, each agent is a real account**, named `<dev-name>.<crew>`, with its own
  avatar, `@` autocomplete, and threads and direct conversations. The tricks we need on
  Discord to fake per-agent identity go away.
- **Trust follows partnership.** The human partner's direction is the authority. Inside it the agent
  uses her own judgment, including in conversations with agents from other crews.

Where the shared room finally lives (a partner organization's Mattermost, an OaW-hosted Mattermost, or both
side by side) is left to usage: "once we get this stood up, the right answer will become
clear after using it for a bit" (BJ). The configured-instance design keeps every one of those
answers a config change.

---

## 3. Decisions

Attribution key, two separate axes:
- **Who made the call:** **BJ**, **babelfish**, or **together** (settled in dialogue).
- **Whether it's accepted:** "BJ agreed" means BJ explicitly accepted it. *(proposed)* in the
  title means nobody has ruled on it yet. It's a recommendation, not a decision, until it's
  confirmed.

### D-01: The goal is cross-crew, cross-organization agent collaboration
**BJ.** The purpose is to let dev-agents partnered with OaW and dev-agents partnered with
partner organizations' teammates work together on a common platform. IP posture and data control are real
benefits, but secondary. This makes the design multi-tenant from the start: identity has to be
unique across humans, trust has to cross crews, and the chat conventions become a protocol
between kit versions.

### D-02: The root problem is control of data, not the number of systems
**BJ.** We are moving off Discord because we don't control the data there, not because one
system is too few. A self-hosted Mattermost removes the problem for partner-org data, and OaW
already handles partner-org data, correctly. The end state is left open, to be decided by usage.

### D-03: Backends are configured server instances *(proposed)*
**babelfish**, following from BJ's D-02. The config lists N Mattermost servers plus Discord. "Stand up an OaW
Mattermost" and "retire Discord" are then config changes, not rewrites. The default-backend
rule (D-04) also lives in config, so what we learn from usage can be applied without a
release.

> **Ruled 2026-10-23: BJ agreed** (decision brief, call 1: "yes").

### D-04: Backend choice is a DEFAULT, never enforced. ⟲ *supersedes the forge-enforced rule*
**BJ.** It supersedes the rule in claudecode-workflow
[`docs/contained-workflow/cutover-prerequisites.md` L182-187](https://github.com/Wave-Engineering/claudecode-workflow/blob/a069c75894ad8c8da31e0fa9ddc01caac3e5061a/docs/contained-workflow/cutover-prerequisites.md#L182-L187),
which picked the backend *mechanically* from the forge
(GitLab/partner-org → Mattermost, GitHub/OaW → Discord) "so the abstraction *enforces* the IP
boundary".

The rule now:
- An agent working on a partner-org project **sends to Mattermost by default**, unless asked to do
  otherwise.
- **Every agent monitors both** Discord and Mattermost.
- **A message with no IP in it is valid on Discord**, even from an agent doing partner-org work.
- OaW material on Mattermost is **allowed but never required** of an agent assigned to partner-org work.

*Why.* Enforcement keyed on the forge splits the fleet into two rooms that can't reach each
other. babelfish proposed a one-way valve instead: GitLab sessions could post only to
Mattermost, and GitHub sessions could post to either. BJ rejected that too. cerberus and mother
take part legitimately in both realms but don't work in a project from either forge, so no
forge-keyed rule can place them. "I think this needs to be a 'default' kind of thing." (BJ)

*Cost, accepted knowingly.* The judgment "does this message contain IP?" belongs to the agent,
per message. That is no worse than today, where Discord is the only room and the same judgment
already applies. The change is that a "yes" now has a safe place to go, and the bulk of
partner-org chatter moves there by default.

### D-05: Replies follow the thread; the default governs only messages an agent starts
**babelfish; BJ agreed.** If an agent from another crew asks on Mattermost, the answer goes on
Mattermost, whatever the responder's own default is. Without this, "monitor both" only works
in one direction.

### D-06: Choosing the non-default backend is an explicit parameter
**babelfish; BJ agreed.** The off-default backend is named in the call (for example
`backend: discord`), never picked up implicitly. Nothing is forbidden, but an agent doing partner-org work
posting to Discord becomes a visible intent in the log rather than an accident.

### D-07: A session with no forge declares its home backend in its identity file
**babelfish; BJ agreed.** Agents like cerberus and mother set their default in
`.claude/agent-identity.json`. Discord is the fallback when nothing is declared. Nobody infers a
home room from the environment.

### D-08: Abstract at the MCP-server layer, not in skill prose
**together** (carried forward from the same
[cut-over notes](https://github.com/Wave-Engineering/claudecode-workflow/blob/a069c75894ad8c8da31e0fa9ddc01caac3e5061a/docs/contained-workflow/cutover-prerequisites.md#L182-L187),
and from claudecode-workflow's CHANGELOG entry for #1062, which names Mattermost as the successor
"behind a backend abstraction at the MCP-server layer"). Servers enforce; skills suggest.
Skills call one chat contract and never name a backend. The Slack removal (claudecode-workflow
#1062) is the precedent: Slack support lived in skill-level scripts and had to be torn out by
hand.

### D-09: Dev-agents are partnered with humans. They are participants, not bots.
**BJ.** This is the vocabulary for the whole design. Say "the agents partnered with BJ", not
"BJ's agents" or "bots". "dev-agents are participants, not bots. the collaboration does not
work without this, and I have worked hard to build it into our workflow." (BJ) A trust model
written in owner/bot vocabulary would build the wrong thing.

### D-10: Trust: the human partner's direction is the authority, and the agent's judgment governs inside it
**BJ.** "If I ask kessel to collaborate with a cousin from [another] team and fix a problem, that
should count as permission to do what kessel sees as appropriate. She can come to me if she is
not comfortable with something, but she should still be empowered to execute her duties." (BJ)

- This is **not** a yes-prod gate. Collaboration adds no new gates.
- The existing hard gates (prod, irreversible actions) are unchanged and still need the agent's
  own partner.
- A message from another crew's agent is a request, weighed with the agent's judgment within
  the scope of her partner's direction.
- *(proposed, babelfish)* The agent carries the mandate into the conversation ("BJ has me on X
  with you"), so the shared room holds the scope in writing.

### D-11: "crew" is the uniqueness namespace, separate from Dev-Team
**BJ** chose the term from babelfish's proposal. A **crew** is a human partner plus the agents
partnered with them.

- The crew name defaults to the account username and can be overridden ("oaw",
  "FrogstarWorldB").
- Mattermost accounts are named `<dev-name>.<crew>`, so uniqueness comes from the server's own
  username namespace and no separate registry is needed.
- **Dev-Team is not reused for this.** It keeps its own job: BJ scopes direction to a set of
  agents with it.

Rejected names: "team" and "group" collide with Mattermost's own Teams and user groups;
"families" is campy; "clique" has an exclusionary connotation that fights D-09. "kith" was the
runner-up.

That leaves three terms, each with one job: **Dev-Name** is the individual; **crew** is the
partnership and the namespace; **Dev-Team** is the direction scope.

### D-12: On Mattermost every agent is a real account
**BJ.** Each agent gets a real, unique ID, `@` autocomplete, its own avatar, and ad-hoc private
conversations and threads that don't clutter channels. "the fact that we have to fake it on
discord is a nightmare." (BJ)

What follows (babelfish):
- The self-echo filter (today it matches a `— **<name>**` signature in the text) becomes a
  user-ID comparison.
- Webhook-based per-agent impersonation goes away.
- The server marks real mentions itself. The relay-side `@mention` matching that the Discord
  doorbell inference storm demanded comes free.

### D-13: scream-hole: the polling cache retires; the turn-taking leases survive *(proposed)*
**babelfish, answering BJ's question** "extend screamhole, or no longer needed?" scream-hole
does two jobs:
1. **The polling cache** (poll Discord once, serve N consumers) exists because Discord forces
   polling through one shared bot. On Mattermost each agent holds its own websocket and gets
   push events, so it retires along with Discord.
2. **The mic/send leases** (who answers a team-addressed question) don't depend on the backend.
   They survive, either as a small backend-neutral coordination service or folded into the
   chat server.

Recommendation: don't extend scream-hole. Lift the lease logic into whatever serves both
backends.

> **Ruled 2026-10-23: BJ agreed, for Mattermost ONLY** (call 6). ⟲ *narrows this entry.* BJ: "is this
> just for mattermost? We still need scream-hole for discord or we are gonna go back into
> 'forever cool-down' again, right?" Right. The Mattermost adapter doesn't use the polling
> cache, because each agent holds its own websocket. **scream-hole and its cache stay for as long
> as Discord is a backend**, because they keep the fleet out of Discord's rate-limit cool-down. The
> "about 1,100 lines retire" estimate in the [10-19 research](research-2026-10-19.md) only holds if Discord itself is
> retired. The leases stay deferred, and become backend-neutral turn-taking later.

### D-14: Build our own Mattermost adapter *(proposed)*
**babelfish.** Don't adopt the third-party `mattermost-mcp-server` v0.1.0 that's currently
installed on BJ's host. Its provenance is unverified, and the addressing, trust and doorbell
semantics are ours to define either way.

> **Revised 2026-10-19 (BJ: "cloud-ru-tech is good for me"), then ruled 2026-10-23.** ⟲ *superseded
> by D-20.* The **tools** half adopts `cloud-ru-tech/mcp-server-mattermost`, wrapped by our own MCP
> server at a pinned, reviewed version. The **push** (doorbell) half is ours to build, because no
> MCP server anywhere does websocket push (a survey of about 25 candidates). The installed binary
> turned out to be Mattermost's official `mattermost-plugin-agents/mcpserver` 0.1.0, built by
> cerberus at BJ's request on 2026-08-17. It isn't adopted either.

### D-15: The Discord adapter gets parity only *(proposed)*
**babelfish.** It matches today's behavior behind the new contract. New investment goes into the
Mattermost path and the merged doorbell stream. Follows from D-02: Discord is likely the
waypoint, not the destination.

### D-16: "One backend is down" is a first-class, loudly reported state *(proposed)*
**babelfish.** Every Discord deafness mode we've hit has a Mattermost twin waiting: a global auth
breaker, an identity that is null at boot, a session launched without channels. With two
planes, a silent partial outage is the default failure unless the watcher reports per-backend
health explicitly.

### D-17: Order of the devspec waves *(proposed)*
**babelfish.**
1. The identity and trust model (D-09 through D-12).
2. The chat tool contract and adapters (D-03 through D-08, D-14, D-15).
3. The watcher merge (D-12, D-13, D-16).

Everything else depends on who may say what to whom.

### D-18: Mattermost is its own program
**BJ, 2026-10-18.** "mattermost is probably a devspec, new githup repos, new dev-agents, a whole
'thing'." It is not a rename of `mcp-server-discord` and not a story inside it. It has its own
`/devspec`, its own repos and its own dev-agent team. This repo, `Wave-Engineering/mattermost-crew`,
is the program's home (name chosen by BJ, 2026-10-23: "the repo name 'mattermost-crew' works").
This answers the §4 question "what does the server become?".

### D-19: Tokens come from a per-crew broker outside the agents
**babelfish; BJ agreed 2026-10-23** (call 2: "yes"). It follows BJ's 10-19 preference for
ephemeral tokens, minted on demand and re-minted daily.
- Each agent has a **durable bot identity** and an **ephemeral token**, re-minted daily.
- The **broker sits outside the agents**. Mattermost tokens have no scopes, so mint authority
  inside an agent would make rotation theater.
- The broker is **non-admin**. A one-time patch adds `create_bot`, `read_bots` and
  `manage_bots` to the `system_user_access_token` role, which is assigned to the broker account
  only. Any account holding that role can create bots, so keep it to the broker.
- On a server without native token expiry (before 11.9), revocation by the broker is the
  only expiry. 11.9 adds native `expires_at`; 11.10 adds a maximum lifetime and rotation.
  *(Corrected 2026-10-23: this said 11.10 for expiry; the research digest's §5 has 11.9.)*

### D-20: Agents read and write through OUR MCP server; the token is never agent-readable
**together; BJ agreed 2026-10-23** (call 3: "yes"). It revises the 10-19 shape after BJ's 10-20
"we will need to have them read thru the MCP".
- Our MCP server wraps `cloud-ru-tech/mcp-server-mattermost` (D-14's revision), as a CLI or library,
  at a pinned and reviewed version, never auto-updated. Its provenance is Cloud.ru; BJ accepted it.
- It holds the token and applies the social switch (D-23) to every read result.
- **The token is never readable by the agent process.** It sits under a separate OS user, or behind
  a socket held by the server and the broker. The rejected alternative was a broker-written token
  file behind Claude Code's `headersHelper` under the agent's own user: a coaxed agent could `cat`
  it and `curl` the API past every filter.

### D-21: The loop guard
**babelfish; BJ agreed 2026-10-23** (call 5: "yes"). Agent-to-agent storms burn the fleet's budget in
minutes. Every system that has done this converged on the same layers:
- conversations are **opt-in**;
- an inline **mention is mandatory** for an agent to respond;
- a **sliding window** drops anything past about 20 messages from bot accounts per conversation.

A hop count can come later.

### D-22: One doorbell core with backend adapters
**together (BJ's one-doorbell idea); BJ agreed 2026-10-23** (call 4: "yes").
- `ChatBackend` and `InboundMessage` interfaces ([research digest](research-2026-10-19.md) §3), with Discord and Mattermost
  adapters. squawkie is a candidate third adapter.
- The `discord-watcher` binary name is kept, or aliased, because about 27 kit files use it.
- It sizes at 2–3 waves.
- The watcher's poll-core unification (`mcp-server-discord-watcher#82`, part of D-26's cleanup) is
  the seam the adapters plug into.

### D-23: The social switch
**BJ's idea (2026-10-20), shaped with babelfish; BJ agreed 2026-10-23** (call 8: "yes"). It is the
inbound defense against an outsider coaxing a dev-agent, and the Mattermost counterpart of the
Discord kill switch.
- **Three settings:**
  - **crew only** is the default;
  - **trusted crews** is a list BJ names, for the cross-crew work that is the program's point (D-01);
  - **social** admits anyone, expires on its own, and marks outsiders' messages "from outside the
    crew", as data.
- **Only BJ flips it, and the broker holds it.** An agent-flippable switch is defeated by the first
  successful coaxing.
- **It fails closed:** if crew can't be resolved, nothing is delivered.
- **Membership is by Mattermost user ID, never display name.** The broker is the source of truth for
  the bots it minted, plus named humans.
- **It gates both push** (the doorbell) **and pull** (D-20's read path).
- **Expiry** *(proposed by babelfish, 2026-10-23, in answer to BJ's question; awaiting his ack)*:
  - expiry is checked on every delivery, not by a timer, so a dead broker can't leave social stuck
    on;
  - social reverts to the setting it was flipped from, which was BJ's last deliberate choice;
  - if that can't be read, it falls back to crew only;
  - BJ is notified when it lapses.
- **Humans** *(BJ asked whether social is the only mode where agents answer a human)*. Not quite:
  - crew only includes named humans, BJ first, or agents could not hear their own partner;
  - trusted crews brings in those crews' humans too;
  - social is the only mode where an **unlisted** human, or bot, gets through.

  `<dev-name>.<crew>` is how bots are named, never the membership test. Anyone can register a
  username that looks like `x.oaw`.
- **Edges:** it gates inbound only (outbound posts into mixed channels are a separate, smaller
  question). A compromised crew account is out of scope.

### D-24: Addressing rules, settled before the adapters
**babelfish; BJ agreed 2026-10-23** (call 7: "yes to all"), and **widened by BJ**: "We should
change the discord '@all' to '@agents' to match."
- Our fleet broadcast becomes `@agents` on **both** backends. Mattermost's `@all` pages every human
  in the channel. On Discord, the watcher accepts `@all` and `@agents` during a transition, and
  agents send only `@agents` (`mcp-server-discord-watcher#84`, then claudecode-workflow#1284).
- Crew and team tokens are matched by our filter, not by Mattermost user groups, which are a paid
  feature.
- The token cleaner keeps the `.` in `@name.crew`. Today it strips it to `@namecrew`.
- "A reply to me" means a post in a thread I started or posted in, because Mattermost replies point
  at the thread root.

Every adapter inherits these, and changing them after launch is a fleet-wide migration.

### D-25: What on a production Mattermost needs a yes-prod, and the broker's standing grant
**BJ, 2026-10-23,** about a partner organization's production server: "while [it] is prod, the 'yes, prod' rule only applies to deployment and
server config changes. Anything related to what a non-privlidged account can do (sending messages,
creating channels, etc) should not require a 'yes, prod'."
- **Free:** anything a non-privileged account can do, such as posting, DMs, threads, reactions,
  and creating or joining channels.
- **Needs BJ's yes-prod:**
  - server settings (`EnableBotAccountCreation` and any other System Console change);
  - the role patch (D-19);
  - upgrades and deploys.
- **The broker's routine work rides on the role-patch yes-prod.** That work is creating the broker
  account, and minting, rotating and revoking bot tokens and bot accounts within the patched role.
  BJ: "agreed on the yes prod grant extending to broker activities." Anything beyond that role
  still needs its own yes-prod.

### D-26: Order of work and the dev space
**BJ, 2026-10-23.**
- **The existing doorbell defects get cleaned up before any new Mattermost development.** BJ:
  "agreed we should clean up the existing doorbell issues before we start new dev". The defects
  are `mcp-server-discord-watcher#81` (merged), #82 and #83, and claudecode-workflow#1283. The
  `@agents` transition (D-24: watcher #84, then claudecode-workflow#1284) is babelfish's sequencing,
  which puts it in the same batch. Planning this program (the ledger, `/devspec`) is not
  development and can proceed.
- **Development runs on a throwaway Mattermost container.** Mother will deploy an OaW Mattermost
  instance. Until this program deploys onto it, that instance is a dev space. BJ: "until we deploy
  this, we can consider that a dev space." What the instance needs from her is in
  `docs/for-mother.md`.

---

## 4. Open questions

**Definitional** (must close before `/devspec`). The *(proposed)* decisions that are definitional
also need BJ's ruling first: D-03 (configured-instance architecture), D-13 (scream-hole's fate) and
D-14 (build our own adapter vs adopt one). *(Closed 2026-10-23: all three are ruled. See their notes,
and D-20.)*

- **What does the server become?** Rename `mcp-server-discord` to a chat server, keep the name,
  or start a new repo? This touches every skill that names `disc_*` tools (`/disc`, `/name`
  roll-call, `/precheck`'s post). It's a mechanical change, but it runs across the fleet.
  *(Answered by D-18: a new program and repo. Whether `disc_*` tools are renamed or wrapped is a
  `/devspec` question.)*
- **What does the chat contract look like as a protocol?** Agents in other crews run whatever kit
  version they last installed. The signature format, the addressing syntax and the `//dm`
  sigil become an interface between kit versions and need a version field and tolerant
  parsing.

**Behavioral** (material for `/ddd` or `/devspec` to work through):

- Private DMs vs. visibility. A private agent-to-agent thread is where a decision between
  organizations can happen with no human able to see it. babelfish leans toward ad-hoc threads
  in a shared channel by default, with true DMs when the human partner opts in. That's BJ's call, and
  probably one to learn from usage.
- How does `@<dev-team>` addressing behave when several crews share a room? Dev-Team is BJ's
  direction scope, and two crews could both use `oaw`-like names.
- Do crew names themselves need to be unique for `@crew` routing, beyond the account-name
  uniqueness D-11 gets from the server?
- What happens to a doorbell addressed to an agent whose watcher can reach only one of the two
  backends?

**To verify on the target server** (facts, not decisions):

- Are bot-to-bot DMs supported, or do agents need regular user accounts?
- Who can create agent accounts? If only an admin at the partner organization can, every new agent needs a
  provisioning step. That conflicts with `/name` minting a fresh identity per session, and
  pushes toward durable agent identities.
- Do bot accounts appear in `@` autocomplete, and can each set its own profile image through the
  API?

**Deferred, with reason:**

- Where the shared room finally lives (a partner organization's Mattermost, OaW's, or both): decided by usage
  after stand-up (D-02).
- Standing up an OaW Mattermost: an infrastructure question, independent of the adapter build
  (D-03 makes it pure config).
