import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { HEADINGS, missingHeadings, redact, runMeeting } from "./board.mjs";

const REPORT = HEADINGS.map((h) => `${h}\nx`).join("\n");
const PACKET = { board_question: "Renew the vendor?", decision_type: "renewal", background: "b", known_facts: ["f"] };
const MEMO = { recommendation: "renew", reasoning: ["r"], confidence: 70 };

function fakePost({ failSeat = [], packet = PACKET, seen = [] } = {}) {
  return async (body) => {
    seen.push(body);
    const sys = body.messages[0].content;
    const user = body.messages[1].content;
    if (sys.includes("Board packet") && sys.includes("JSON only")) return { status: 200, json: { choices: [{ message: { content: JSON.stringify(packet) } }] } };
    if (sys.startsWith("You are an independent board director")) {
      if (failSeat.some((s) => body.model.includes(s))) return { status: 500, json: null };
      return { status: 200, json: { choices: [{ message: { content: "```json\n" + JSON.stringify(MEMO) + "\n```" } }] } };
    }
    if (user.includes("Memoranda")) return { status: 200, json: { choices: [{ message: { content: REPORT } }] } };
    return { status: 400, json: null };
  };
}

test("a whole meeting: packet, three independent directors, anonymised, chair report, run record", async () => {
  const runsDir = mkdtempSync(join(tmpdir(), "board-"));
  const seen = [];
  const out = await runMeeting({ conversation: "We discussed the vendor. password: hunter2", question: "Renew?" },
    { key: "k", policyPath: "/nonexistent", runsDir, post: fakePost({ seen }) });
  assert.equal(out.status, "ok", JSON.stringify(out));
  assert.deepEqual(out.missing_headings, []);
  assert.equal(out.participation.length, 3);
  assert.ok(!JSON.stringify(seen).includes("hunter2"));                          // redacted before any model call
  const directors = seen.filter((b) => b.messages[0].content.startsWith("You are an independent"));
  assert.equal(new Set(directors.map((b) => b.model)).size, 3);                  // three different makers
  assert.equal(new Set(directors.map((b) => b.messages[1].content)).size, 1);    // identical prompt, no cross-talk
  const evalCall = seen.find((b) => b.messages[1].content.includes("Memoranda"));
  assert.ok(!/openai|anthropic|google/.test(evalCall.messages[1].content));     // the chair never sees who said what
  const rec = readFileSync(join(runsDir, readdirSync(runsDir)[0]), "utf8");
  assert.match(rec, /"event":"mapping"/);
  assert.ok(!rec.includes("renew") && !rec.includes("hunter2"));                // no content kept, only metadata
});

test("EHR or client material stops the meeting before any model call", async () => {
  const seen = [];
  const out = await runMeeting({ conversation: "about the client name and the therapy session" }, { key: "k", post: fakePost({ seen }) });
  assert.equal(out.status, "blocked");
  assert.equal(seen.length, 0);
});

test("sensitive content waits for the owner's approval, then the approved packet goes ahead", async () => {
  const seen = [];
  const out = await runMeeting({ conversation: "about my divorce" }, { key: "k", post: fakePost({ seen, packet: { ...PACKET, background: "divorce settlement" } }) });
  assert.equal(out.status, "approval_required");
  assert.equal(seen.length, 1);                                                  // only the chair, nothing to directors
  const again = await runMeeting({ approved_packet: out.packet }, { key: "k", post: fakePost() });
  assert.equal(again.status, "ok");
});

test("one director failing still meets; fewer than two does not", async () => {
  const one = await runMeeting({ conversation: "x" }, { key: "k", post: fakePost({ failSeat: ["gemini"] }) });
  assert.equal(one.status, "ok");
  assert.ok(one.participation.some((p) => p.startsWith("google: did not answer")));
  const two = await runMeeting({ conversation: "x" }, { key: "k", post: fakePost({ failSeat: ["gemini", "claude"] }) });
  assert.equal(two.status, "failed");
});

test("redaction and report checks", () => {
  assert.equal(redact("api_key: abc123 and 4111 1111 1111 1111"), "api_key: [REDACTED] and [REDACTED]");
  assert.deepEqual(missingHeadings(REPORT), []);
  assert.ok(missingHeadings("# Board Meeting").length > 5);
});
