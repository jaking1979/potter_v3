import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { classify } from "./lib/classify.js";
import { decide, grantCovers, trustOf } from "./lib/control.js";
import { tryHouse } from "./lib/fastpath.js";
import { jevDomains, plan, planText, requestText, tableDomains } from "./lib/governor.js";
import { append, mask, recent, verify } from "./lib/receipts.js";

const OWNER = { requester: { senderIsOwner: true } };

// ---------------------------------------------------------------- classification
test("reads, reversible writes and consequential actions are told apart", () => {
  assert.equal(classify("web_search", { q: "x" }).risk, "read");
  assert.equal(classify("write", { path: "notes/a.md" }).risk, "reversible");
  assert.equal(classify("exec", { command: "ls -la" }).risk, "reversible");
  assert.equal(classify("exec", { command: "merrick ask 'how warm is it'" }).risk, "reversible");
  const dep = classify("exec", { command: "merrick confirm-departure" });
  assert.deepEqual([dep.risk, dep.severity], ["consequential", "high"]);
  assert.equal(classify("exec", { command: "potter-cron on potter-mail-intake" }).risk, "consequential");
  assert.equal(classify("exec", { command: "curl -X POST https://api.example/x" }).risk, "consequential");
  assert.equal(classify("exec", { command: "curl -s https://example.com/page" }).risk, "reversible");
  assert.equal(classify("cron", { action: "list" }).risk, "read");
  assert.equal(classify("cron", { action: "add", name: "x" }).risk, "consequential");
});

test("messages to the owner are routine; to anyone else they are consequential", () => {
  const o = { ownerIds: ["8411000000"] };
  assert.equal(classify("message", { action: "send", to: "8411000000" }, o).risk, "reversible");
  assert.equal(classify("message", { action: "send", to: "telegram:8411000000" }, o).risk, "reversible");
  assert.equal(classify("message", { action: "send" }, o).risk, "reversible");
  assert.equal(classify("message", { action: "send", to: "5550001111" }, o).risk, "consequential");
});

test("connected-app tools are judged by their action, including inside a meta-tool", () => {
  assert.equal(classify("notion__notion-search", {}).risk, "read");
  assert.equal(classify("notion__notion-create-pages", {}).risk, "consequential");
  const send = classify("composio__COMPOSIO_MULTI_EXECUTE_TOOL", { tools: [{ tool_slug: "GMAIL_SEND_EMAIL", arguments: {} }] });
  assert.deepEqual([send.risk, send.capability], ["consequential", "mcp:composio:GMAIL_SEND_EMAIL"]);
  const del = classify("composio__COMPOSIO_MULTI_EXECUTE_TOOL", { tools: [{ tool_slug: "GMAIL_FETCH_EMAILS" }, { tool_slug: "GMAIL_DELETE_MESSAGE" }] });
  assert.deepEqual([del.risk, del.severity], ["consequential", "high"]);                      // worst one wins
  assert.equal(classify("composio__COMPOSIO_SEARCH_TOOLS", { query: "gmail" }).risk, "read");
});

test("local plugin tools are reversible unless they reach outside or move money", () => {
  assert.equal(classify("kg_search", {}).risk, "read");
  assert.equal(classify("kg_add", { name: "x" }).risk, "reversible");
  assert.equal(classify("agentmail_send_message", {}).risk, "consequential");
  assert.equal(classify("wallet_transfer", {}).severity, "high");
});

test("locks, alarms and valves through Home Assistant tools are forbidden; climate is not", () => {
  assert.equal(classify("homeassistant__HassTurnOn", { name: "front door lock" }).risk, "forbidden");
  assert.equal(classify("homeassistant__HassLock", {}).risk, "forbidden");
  assert.equal(classify("homeassistant__HassClimateSetTemperature", { temperature: 68 }).risk, "consequential");
  assert.equal(classify("homeassistant__GetLiveContext", {}).risk, "read");
});

// ---------------------------------------------------------------- decisions
test("the ratified matrix", () => {
  const c = (risk, severity = "normal", capability = "x") => ({ risk, severity, capability });
  assert.equal(decide(c("read"), "unknown").action, "allow");
  assert.equal(decide(c("reversible"), "other").action, "allow");
  assert.equal(decide(c("consequential"), "owner").action, "allow");
  assert.equal(decide(c("consequential", "high"), "owner").action, "approve");
  assert.equal(decide(c("consequential"), "other").action, "approve");
  assert.equal(decide(c("consequential"), "unknown").action, "approve");
  assert.equal(decide(c("consequential"), "schedule").action, "approve");
  assert.equal(decide(c("consequential", "normal", "exec:potter-cron"), "schedule", { standingGrants: ["exec:potter-*"] }).action, "allow");
  assert.equal(decide(c("consequential", "high", "exec:trade"), "schedule", { standingGrants: ["exec:*"] }).action, "approve");
  assert.equal(decide(c("forbidden", "high"), "owner").action, "block");
});

test("shadow mode never blocks or asks, but says what it would have done", () => {
  const d = decide({ risk: "consequential", severity: "normal", capability: "x" }, "other", { mode: "shadow" });
  assert.deepEqual([d.action, d.would], ["allow", "approve"]);
});

test("trust comes only from what the host proves", () => {
  assert.equal(trustOf(OWNER), "owner");
  assert.equal(trustOf({ trigger: "cron" }), "schedule");
  assert.equal(trustOf({ requester: { senderIsOwner: false } }), "other");
  assert.equal(trustOf({}), "unknown");
  assert.equal(trustOf({ trigger: "cron", requester: { senderIsOwner: true } }), "owner");
  assert.ok(grantCovers(["mcp:composio:GMAIL_*"], "mcp:composio:GMAIL_SEND_EMAIL"));
  assert.ok(!grantCovers(["mcp:composio:GMAIL_*"], "mcp:composio:SLACK_SEND"));
});

// ---------------------------------------------------------------- receipts
test("receipts are hash-chained, mask secrets and detect tampering", () => {
  const p = join(mkdtempSync(join(tmpdir(), "rc-")), "r.jsonl");
  const fakeKey = ["sk", "or", "v1", "abcdefghijklmnop"].join("-");          // built at runtime: no key shape in the repo
  append(p, { phase: "intent", capability: "exec:mail", target: `token ${fakeKey} here` });
  append(p, { phase: "result", capability: "exec:mail", ok: true, target: "" });
  assert.deepEqual(verify(p), { ok: true, count: 2, brokenAt: null });
  assert.ok(!readFileSync(p, "utf8").includes(fakeKey));
  assert.equal(recent(p, 5).length, 2);
  const lines = readFileSync(p, "utf8").split("\n");
  writeFileSync(p, lines[0].replace("exec:mail", "exec:none") + "\n" + lines.slice(1).join("\n"));
  assert.equal(verify(p).ok, false);
  assert.equal(mask("Bearer abc.def"), "(secret)");
});

// ---------------------------------------------------------------- governor
test("the table finds the domains and the plan points at rules, skills and apps", () => {
  assert.deepEqual(tableDomains("how warm is the basement?"), ["house"]);
  assert.deepEqual(tableDomains("did you reply to Sarah's email?"), ["email", "verify"]);
  const t = planText(["house"]);
  assert.match(t, /merrick ask/);
  assert.match(t, /homeassistant/);
  assert.match(planText(["email"]), /rules\/global\/email-pipeline\.md/);
  assert.equal(planText([]), "");
  assert.deepEqual(tableDomains("What should I order for dinner?"), ["food"]);
  assert.match(planText(["food"]), /preferences\/food-wine\.md/);
});

test("Jev decides only when the table finds nothing, and a slow or broken Jev never blocks", async () => {
  let calls = 0;
  const yes = async () => { calls++; return { ok: true, answers: { money: { value: true, confidence: 0.9 }, email: { value: true, confidence: 0.4 } } }; };
  const p1 = await plan("set the family room to 70", { decide: yes });
  assert.deepEqual([p1.source, calls], ["table", 0]);
  const p2 = await plan("what should I do about the Hendricks situation", { decide: yes });
  assert.deepEqual([p2.source, p2.domains], ["jev", ["money"]]);                // low-confidence email dropped
  const slow = () => new Promise((r) => setTimeout(() => r({ ok: true, answers: { money: { value: true, confidence: 1 } } }), 200));
  const p3 = await plan("what should I do about the Hendricks situation", { decide: slow, timeoutMs: 20 });
  assert.deepEqual([p3.domains, p3.error], [[], "timeout"]);
  const p4 = await plan("what should I do about the Hendricks situation", { decide: async () => { throw new Error("x"); } });
  assert.deepEqual(p4.domains, []);
  assert.deepEqual(jevDomains({ house: { choice: "true", probabilities: { true: 0.8 } } }), ["house"]);
  assert.deepEqual(jevDomains({ money: { noul: 0.83 }, email: { noul: 0.2 } }), ["money"]);            // v2's endpoint
  assert.deepEqual(jevDomains({ local: { probabilityTrue: 0.91 }, brief: { probabilityTrue: 0.5 } }), ["local"]);
});

test("the owner's words are found inside a prompt with channel context", () => {
  assert.equal(requestText({ currentUserMessage: " status " }), "status");
  assert.equal(requestText({ cleanedBody: "[Telegram Josh id:1 +2m]\nConversation info: x\n\nhow warm is it" }), "how warm is it");
});

// ---------------------------------------------------------------- fast path
test("Merrick's fast answers are used; anything else goes to the normal turn", async () => {
  const fast = async (_c, args) => (args[0] === "ask" ? { path: "fast", intent: "check_temperature", outcome: "ok", reply: "Observed: 64 F" } : null);
  assert.deepEqual((await tryHouse("how warm is it", { runner: fast })).text, "Observed: 64 F");
  const fb = async () => ({ path: "fallback", reply: "" });
  assert.equal(await tryHouse("why is the basement cold", { runner: fb }), null);
  assert.equal(await tryHouse("how warm is it", { runner: async () => null }), null);    // Merrick missing or broken
});

test("confirm departure runs only for a proven owner", async () => {
  const seen = [];
  const runner = async (_c, args) => { seen.push(args[0]); return { outcome: "done", reply: "Confirmed: locked." }; };
  assert.equal(await tryHouse("confirm departure", { runner, isOwner: undefined }), null);
  assert.equal(await tryHouse("confirm departure", { runner, isOwner: false }), null);
  assert.equal((await tryHouse("confirm departure", { runner, isOwner: true })).text, "Confirmed: locked.");
  assert.deepEqual(seen, ["confirm-departure"]);
});
