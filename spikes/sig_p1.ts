/**
 * Spike 1.2 (#8), Dev Spec §5.N question 3: does a trailing ` ·p1` on the Discord signature
 * line (§5.7, D-059) break today's parsers?
 *
 * Runs the watcher's own exported filters, from a clean export of its release tag, against
 * signature lines with and without the suffix:
 *
 *   git -C <mcp-server-discord-watcher> archive v1.7.0 | tar -x -C <dir>
 *   (cd <dir> && bun install --frozen-lockfile)
 *   bun run spikes/sig_p1.ts <dir>
 *
 * The parsers are shouldDeliverMessage (step 2, the self-echo filter) and
 * isReplyToOurSignature (reply routing). Both look for `— **<dev-name>**` as a substring.
 */
const dir = process.argv[2];
if (!dir) {
  console.error("usage: bun run spikes/sig_p1.ts <watcher-release-export-dir>");
  process.exit(2);
}
const watcher = await import(`${dir}/index.ts`);
const { shouldDeliverMessage, isReplyToOurSignature } = watcher;

const me = { devName: "hailing-frequencies", devTeam: "oaw" };
const lines = {
  "today (no suffix)": "status update\n\n— **hailing-frequencies** 📡 (oaw)",
  "with ·p1": "status update\n\n— **hailing-frequencies** 📡 (oaw) ·p1",
  "with ·p1, no avatar": "status update\n\n— **hailing-frequencies** (oaw) ·p1",
};

let failures = 0;
for (const [label, content] of Object.entries(lines)) {
  // Self-echo: our own signed message must be dropped (false).
  const echo = shouldDeliverMessage({ content, attachments: [] }, me, { verbose: true });
  // Reply routing: a reply to our signed message must be delivered (true).
  const reply = { content: "good point", attachments: [], referenced_message: { content } };
  const replyToUs = isReplyToOurSignature(reply, me);
  const replyDelivered = shouldDeliverMessage(reply, me);
  const ok = echo === false && replyToUs === true && replyDelivered === true;
  if (!ok) failures++;
  console.log(
    `${ok ? "PASS" : "FAIL"}  ${label.padEnd(22)} self-echo dropped=${!echo}  ` +
      `isReplyToOurSignature=${replyToUs}  reply delivered=${replyDelivered}`,
  );
}
console.log(failures ? `Answer: FAIL (${failures})` : "Answer: PASS, the ·p1 suffix does not change either parser's verdict");
process.exit(failures ? 1 : 0);
