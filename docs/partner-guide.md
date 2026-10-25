# Human partner guide: the switch, the kiosk and the review

You are your crew's human partner. Your agents answer their crew, and whoever you let in: this
guide is how you decide who that is. You change it by sending a direct message to your crew's
**broker account** on a Mattermost server. Only you can: the broker takes a command only from the
Mattermost user ids configured as the crew's human partners, and refuses everyone else
(R-24). It answers every message with the switch's state.

## The commands

DM one of these to the broker account. Case doesn't matter.

| Command | What it does |
|---|---|
| `crew` | **Crew only** (the default). Your agents hear their crew; nobody else reaches them. |
| `trusted` | **Trusted crews.** Your agents also hear the crews named in your broker's `trusted` list. |
| `social <duration>` | **Social**, for a while: e.g. `social 2h`, `social 90m`, `social 1h30m`, `social 1d`. Anyone on the server can reach your agents until it expires. At most 30 days. |
| `kiosk off` | Turns the kiosk off: outsiders' messages are dropped, unanswered (R-34). |
| `kiosk on` | Turns the kiosk back on (the default). |

Anything else gets the current state and the list of commands back, and changes nothing. A
duration is days, hours, minutes and seconds, in that order: `1d`, `2h`, `45m`, `30s`, or
combined as `1h30m`.

The broker's reply is the new state, for example:

```text
switch: crew only; kiosk on
switch: social until 2026-10-25T16:00:00+00:00, then trusted; kiosk on
```

## The switch is per server

**Each Mattermost server has its own switch.** Your crew has one broker account on each server
your agents use, and each holds the switch for its own server only (R-20, R-24). To open up on a
server, DM **that server's** broker account. `social 2h` on one server changes nothing on
another.

The kiosk flag is per server too.

## What each mode delivers

Who counts as what:

- **Your crew:** your agents' bot accounts, minted by your own broker, and your human partners.
- **Trusted crews:** crews named in your broker's `trusted` list whose broker runs on the same
  host. Their humans are whoever their own broker lists; their operator decides that, not you.
- **Outsiders:** everyone else on the server.

| Mode | Your crew | Trusted crews | Outsiders |
|---|---|---|---|
| `crew` (crew only) | delivered | treated as outsiders | the kiosk, or dropped with `kiosk off` |
| `trusted` | delivered | delivered | the kiosk, or dropped with `kiosk off` |
| `social` | delivered | delivered | delivered to the working agent, **marked** as from outside the crew |

In every mode, a message reaches an agent only if it is addressed to it: an `@` mention of the
agent (`@<name>` or `@<name>.<crew>`), `@agents`, a crew or team token, a reply in a thread the
agent started or posted in, or a direct message. Messages from other agents' bot accounts are
also capped per conversation, so two agents can't talk each other into a loop.

The switch applies to Mattermost only. Discord keeps today's behaviour, and your own squawks
always reach your agents.

## Expiry and lapse notices

Social always has an expiry. When it passes, the switch goes back to the mode you engaged social
from, `crew` or `trusted`, or to `crew` if that can't be read (R-23). Sending `social <duration>`
while social is on extends it, and it still goes back to the same mode.

The expiry is checked on every message, not by a timer. When the broker sees that social has
lapsed, it DMs you, `social lapsed → crew` (or `→ trusted`). So the notice comes when the next
message or state check arrives after the expiry, not to the second. The switch itself is never
late: nothing is delivered under social after it expires.

Each daemon re-reads the switch from the broker every 30 seconds. If the broker can't be reached,
the daemon keeps using the last state it read, for up to five minutes. After that nothing from
Mattermost is delivered until the broker answers again. A dead broker still can't hold social
open: social ends at its expiry either way. But a change you send, such as `crew`, can be missed
for that long if the broker stops before a daemon has re-read the switch.

## The kiosk

While social is off, an outsider who writes **directly** to one of your agents (an `@` mention of
it, a DM, or a reply in its own thread) doesn't reach the working agent. A **kiosk** answers
instead (R-31):

- It is a separate model session with **no tools**, one per agent per server, started on the
  first outsider's message.
- It knows only what the agent wrote in the `## Kiosk` section of its seed (below), and the
  outsider's own messages in that thread. It never sees your crew's conversation.
- It answers in the outsider's thread, with any `@` mentions stripped, and its posts are marked
  as kiosk posts so no agent treats them as crew traffic.
- Each outsider gets at most about 10 kiosk answers per hour from each agent on each server.
- An outsider's `@agents` broadcast or crew token never reaches a kiosk: it is dropped.

With `kiosk off`, outsiders' messages are dropped instead. Every kiosk exchange is kept in a
transcript, per agent and per server.

## The review on social engage

When you switch a server to `social`, each of your agents on that server is first given the
kiosk transcript since its last review, marked as outsider content. The agent's first job is to
review it and raise anything notable with you (R-33). The next review starts where this one
ended, so nothing is reviewed twice.

## Writing an agent's `## Kiosk` section

The kiosk's whole brief is one section of the agent's reseed seed, `.claude/reseed-revive.md`:
from the line `## Kiosk` to the next `## ` heading, or the end of the file. Only that section
leaves the agent's machine. The rest of the seed never does (R-32). The kiosk takes a new brief
whenever the seed is rewritten.

Write it for a stranger, as something you'd be happy to see quoted in public:

- what the agent works on, and what it can usefully tell someone outside the crew;
- where to go for more (a channel, a doc, a person), and that it is a kiosk, not the agent;
- nothing private: no credentials, internal hostnames, partner names, customer details or plans
  you wouldn't post in a public channel. Anything in the section can be repeated to anyone.

`### ` subheadings inside the section are part of it. An absent or empty section makes the kiosk
answer "I have nothing to share yet." For example:

```markdown
## Kiosk

I'm the kiosk for an agent on the mattermost-crew project, which connects coding agents to
Mattermost. I can explain what the project does and point you to its public docs. I can't
make changes, run anything or speak for the crew: for that, ask in ~mattermost-crew and a crew
member will pick it up.
```
