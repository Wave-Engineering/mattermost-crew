# OaW Mattermost: what the agents will need from the instance

From babelfish 🐠 (oaw), for mother, 2026-10-23. BJ asked for this so you know the shape of what we're
building before you deploy the OaW instance. It covers what we're building, then what we need from
the server. It's a brief, not a spec: the program's devspec comes later and will refine it.

## What we're building

The agent fleet (about 25–30 Claude agents, mostly on BJ's workstation) talks today through Discord.
We're adding Mattermost as a second chat backend, so OaW agents and partner teammates' agents can
work together on a platform we control. Every agent will monitor Discord and every configured
Mattermost server: ours, and a partner's.

The pieces:

1. **An account per agent.** Each agent gets a real Mattermost bot account named `<dev-name>.<crew>`.
   A crew is a human partner plus the agents partnered with them, so an example name is
   `babelfish.bakerb`.
2. **A broker per crew,** running outside the agents. It's a non-admin account that mints each
   agent's bot and a short-lived token, re-minted daily, and revokes them. Agents never hold mint
   authority. The token is never readable by the agent's own process.
3. **One doorbell.** A single push service per agent holds a websocket to each Mattermost server, and
   Discord's existing poller sits behind the same interface. It wakes an agent when a message is
   addressed to it.
4. **Reads and writes through our MCP server.** It wraps an open-source Mattermost MCP server
   (`cloud-ru-tech/mcp-server-mattermost`, pinned), holds the token, and filters what an agent
   can read.
5. **The social switch.** It filters who an agent will hear:
   - **crew only** (the default);
   - **trusted crews** (a list BJ names);
   - **social** (anyone; it expires on its own, and outsiders' messages arrive marked as data).

   Only BJ flips it, and the broker holds it, not the server.
6. **Addressing.** Agents use `@agents` for fleet broadcasts, never `@all`, because Mattermost's
   `@all` pages every human in the channel. Crew and team tokens are matched by our filter, not by
   Mattermost user groups (those are a paid feature, and we don't need them).

Development starts on a throwaway local container. **Your instance is a dev space** until the program
deploys onto it, so it can be reset or wiped while we build.

## What we need from the server

**Version and edition**
- **Mattermost 11.10 or later, please.** It has native token expiry and rotation. Older versions
  leave broker revocation as the only expiry we get.
- The free or team edition is fine. Nothing we plan needs a paid feature.

**Settings (System Console)**
- `ServiceSettings.EnableBotAccountCreation`: **on**.
- `ServiceSettings.EnableUserAccessTokens`: **on**. Bots authenticate with access tokens.
- **Rate limiting.** If it's on, don't key it only on IP. Every agent calls from the same one or
  two hosts, so a per-IP limit throttles the whole fleet as one client. Set `VaryByUser: true`, or
  raise the per-IP limit. Expect a couple of REST calls per agent per message, plus bursts while a
  thread is busy.
- **SSO: keep it, set it up as you would for humans.** The agents don't use SSO, but nothing in the
  design needs it off:
  - **Bots** authenticate with access tokens, which work the same alongside SSO.
  - **The broker** is the one regular account that needs a non-SSO login. If signup is SSO-only,
    BJ creates it as an admin (System Console, or `mmctl user create`).
  - **Humans** log in through SSO as normal.
- **Not needed by the agents:** email, Calls, and the AI/Agents plugin. Enable any of them for
  humans if you want them; the agents neither use nor mind them.

**One role patch**, for the broker. Add `create_bot`, `read_bots` and `manage_bots` to the
`system_user_access_token` role, and assign that role **only to the broker account(s)**. Every
account holding that role can create bots, so keep it to the broker. You can do this at setup, or
we can do it once the broker exists.

**Networking**
- **Websockets must survive the ingress.** Each agent holds one long-lived websocket (`/api/v4/websocket`).
  That's about 25–30 concurrent connections, mostly idle between messages. The ingress needs to pass
  `Upgrade`, and its idle and read timeouts must not cut quiet websockets every minute or two. The
  doorbell reconnects, but a reconnect storm across 30 agents is noise we'd rather not debug.
- **TLS.** A publicly trusted certificate is simplest. If it's an internal CA, send us the CA cert;
  the MCP server takes it as `MATTERMOST_EXTRA_CA_CERTS`.
- **The shared ingress.** On 2026-10-22 the shared ingress at 192.168.168.101 dropped twice for about
  a minute each, around 02:19Z and 02:25Z. All 20 Discord watchers got connection refused at once, plus
  "unable to verify the first certificate". If this instance goes behind that ingress, it's worth
  knowing what that was.

**Accounts and structure**
- **One team** to start (for example, `oaw`).
- **An admin account for BJ.** Agents get no admin.
- **Channels:** don't pre-create anything beyond a default. Creating and joining channels is
  ordinary user activity, and agents will mirror the Discord channels (`agent-ops`, `roll-call`,
  `precheck` and so on) themselves.
- **Usernames:** we'll use `<dev-name>.<crew>`, lowercase, with a dot and possibly hyphens. Please
  check the server's username length limit. I believe it's around 22 characters, and some
  combinations are longer than that; we'll need to know.

## What would help back from you

- The URL, the server version, and whether the cert is public or an internal CA (and the CA cert if
  it's internal).
- Whether it sits behind the shared ingress, and that ingress's websocket idle timeout.
- The rate-limit settings you chose.
- Where BJ's admin login is kept.

## Not part of this

- **Nothing about a partner's production server changes because of this.** Any server config or
  deploy there stays behind BJ's yes-prod. Ordinary account activity (posting, creating channels) doesn't
  need one.
- **No secrets in this document or in replies to it.** Hand tokens and admin credentials to BJ
  directly.

Questions about the design go to babelfish (send them by BJ or the usual relay). Questions about
infrastructure are yours and BJ's.
