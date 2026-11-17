// v3.3 broker, v3.4 memory and responsibilities, v3.5 durability.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { chooseClass, recordSpend, spentToday, toOverride } from "./lib/broker.js";
import { classify } from "./lib/classify.js";
import { alreadyDone, effectKey, enqueue, inDoubt, isOutboxRetry, outboxPending, settle } from "./lib/durable.js";
import * as memory from "./lib/memory.js";
import { append } from "./lib/receipts.js";
import { buildTools } from "./lib/tools.js";

const tmp = () => mkdtempSync(join(tmpdir(), "pc35-"));

// ---------------------------------------------------------------- v3.3 broker
test("ordinary turns stay on chat; depth, long or money-decision turns escalate", () => {
  assert.equal(chooseClass("how warm is the house?").cls, "chat");
  assert.equal(chooseClass("what should I order for dinner").cls, "chat");
  assert.equal(chooseClass("Think hard about the trade-offs of selling the cabin").cls, "escalation");
  assert.equal(chooseClass("x".repeat(1600)).cls, "escalation");
  assert.equal(chooseClass("should I move the IRA into bonds", { domains: ["money"] }).cls, "escalation");
});

test("the daily escalation budget keeps turns on chat once spent", () => {
  const p = chooseClass("analyze the root cause", { escalationSpentUsd: 3.2, escalationCapUsd: 3 });
  assert.equal(p.cls, "chat");
  assert.match(p.reason, /budget/);
});

test("policy classes become run-scoped overrides", () => {
  assert.deepEqual(toOverride({ model: "openrouter/deepseek/deepseek-v4-pro", fallbacks: ["openrouter/deepseek/deepseek-v4.1-flash"] }),
    { providerOverride: "openrouter", modelOverride: "deepseek/deepseek-v4-pro", fallbacksOverride: ["openrouter/deepseek/deepseek-v4.1-flash"] });
  assert.equal(toOverride({ model: "nonsense" }), null);
  assert.equal(toOverride(undefined), null);
});

test("the spend ledger adds today's costs, per class", () => {
  const p = join(tmp(), "spend.jsonl");
  const now = new Date("2026-11-17T20:00:00Z");
  recordSpend(p, { cls: "escalation", costUsd: 0.4 }, now);
  recordSpend(p, { cls: "chat", costUsd: 0.01 }, now);
  recordSpend(p, { cls: "escalation", costUsd: 1.0 }, new Date("2026-11-16T20:00:00Z"));
  assert.equal(spentToday(p, { now, cls: "escalation" }), 0.4);
  assert.equal(Number(spentToday(p, { now }).toFixed(2)), 0.41);
});

// ---------------------------------------------------------------- v3.4 memory
test("nothing is remembered until the owner approves; corrections replace; provenance is kept", () => {
  const d = tmp();
  const staged = join(d, "staged.jsonl");
  const mem = join(d, "MEMORY.md");
  memory.stage(staged, { text: "Dinner delivery is usually from the Thai place on 7th", kind: "preference", source: "owner, 17 Nov" });
  memory.stage(staged, { text: "Prefers to be asked one question at a time", kind: "pattern", confidence: 0.55, examples: ["asked twice to slow down"] });
  memory.stage(staged, { text: "Molly's ski races are on Saturdays", kind: "fact" });
  assert.equal(memory.stage(staged, { text: "molly's ski races are on saturdays", kind: "fact" }).status, "already staged");
  const review = memory.reviewText(memory.pending(staged));
  assert.match(review, /1\. \[preference\]/);
  assert.match(review, /confidence 55%/);
  const done = memory.apply(staged, mem, [{ n: 1, action: "approve" }, { n: 2, action: "reject" }, { n: 3, action: "edit", text: "Molly's NYSEF races are on Saturdays" }]);
  assert.equal(done.length, 3);
  let m = readFileSync(mem, "utf8");
  assert.match(m, /## Preferences\n- Dinner delivery/);
  assert.match(m, /Molly's NYSEF races are on Saturdays <!-- m[0-9a-f]{8} \| conversation \| approved/);
  assert.ok(!m.includes("one question at a time"));
  memory.stage(staged, { text: "Molly's NYSEF races are on Sundays", kind: "correction", replaces: "Molly's NYSEF races are on Saturdays" });
  memory.apply(staged, mem, [{ n: 1, action: "approve" }]);
  m = readFileSync(mem, "utf8");
  assert.ok(!m.includes("on Saturdays") && m.includes("on Sundays"));
  assert.equal(memory.pending(staged).length, 0);
});

test("approving memory is owner-only; staging is not", () => {
  assert.equal(classify("memory_review_apply", {}).risk, "consequential");
  assert.equal(classify("memory_stage", {}).risk, "reversible");
});

test("responsibilities: tracked with a cadence, listed when due, moved on when done", () => {
  const p = join(tmp(), "resp.json");
  const now = new Date("2026-11-17T12:00:00Z");
  memory.track(p, { name: "Drain the outdoor spigots", cadence: "yearly", next_due: "2026-11-20", checklist: ["shut valve", "open spigot"] }, now);
  memory.track(p, { name: "Car registration", cadence: "every 730 days", next_due: "2027-03-01" }, now);
  assert.deepEqual(memory.due(p, 7, now).map((i) => i.name), ["Drain the outdoor spigots"]);
  assert.equal(memory.done(p, "drain the outdoor spigots", now).next_due, "2027-11-20");
  assert.throws(() => memory.track(p, { name: "x", cadence: "sometimes" }, now));
});

// ---------------------------------------------------------------- v3.5 durability
test("the same consequential call that already succeeded asks before repeating; a failed one does not", () => {
  const r = join(tmp(), "receipts.jsonl");
  const params = { to: "a@example.com", subject: "x" };
  const key = effectKey("composio__GMAIL_SEND_EMAIL", params);
  assert.equal(effectKey("composio__GMAIL_SEND_EMAIL", { subject: "x", to: "a@example.com" }), key);   // order-free
  assert.notEqual(effectKey("composio__GMAIL_SEND_EMAIL", { ...params, subject: "y" }), key);
  const i1 = append(r, { phase: "intent", capability: "c", risk: "consequential", decision: "allow", effect: key, target: "" });
  append(r, { phase: "result", intent: i1.hash, ok: false, target: "" });
  assert.equal(alreadyDone(r, key), null);
  const i2 = append(r, { phase: "intent", capability: "c", risk: "consequential", decision: "allow", effect: key, target: "" });
  append(r, { phase: "result", intent: i2.hash, ok: true, target: "" });
  assert.ok(alreadyDone(r, key));
  assert.equal(alreadyDone(r, key, { now: new Date(Date.now() + 8 * 86400000) }), null);   // older than a week
});

test("an action that started but has no result is listed as in doubt", () => {
  const r = join(tmp(), "receipts.jsonl");
  const old = new Date(Date.now() - 30 * 60000);
  const i = append(r, { phase: "intent", capability: "mcp:composio:GMAIL_SEND_EMAIL", risk: "consequential", decision: "allow", target: "" }, old);
  append(r, { phase: "intent", capability: "write", risk: "reversible", decision: "allow", target: "" }, old);
  assert.deepEqual(inDoubt(r).map((x) => x.hash), [i.hash]);
  append(r, { phase: "result", intent: i.hash, ok: true, target: "" });
  assert.equal(inDoubt(r).length, 0);
});

test("a failed Notion write waits in the outbox, its retry is recognised, and it settles or gives up", () => {
  const p = join(tmp(), "outbox.json");
  const call = { tool: "notion__notion-update-page", params: { page_id: "p1", content: "x" }, error: "502" };
  const key = enqueue(p, call);
  enqueue(p, call);                                                          // same write: one item, two attempts
  assert.equal(outboxPending(p).length, 1);
  assert.equal(outboxPending(p)[0].attempts, 2);
  assert.ok(isOutboxRetry(p, effectKey(call.tool, call.params)));
  assert.equal(settle(p, key, true).status, "done");
  assert.equal(isOutboxRetry(p, key), false);
  const k2 = enqueue(p, { ...call, params: { page_id: "p2" } });
  for (let n = 0; n < 6; n++) settle(p, k2, false);
  assert.equal(outboxPending(p).length, 0);
  assert.equal(JSON.parse(readFileSync(p, "utf8")).items.find((i) => i.key === k2).status, "gave_up");
});

test("the tools run end to end on real files", async () => {
  const d = tmp();
  const tools = Object.fromEntries(buildTools({ staged: join(d, "s.jsonl"), memory: join(d, "MEMORY.md"),
    responsibilities: join(d, "r.json"), outbox: join(d, "o.json") }).map((t) => [t.name, t]));
  assert.deepEqual(Object.keys(tools).sort(), ["memory_review", "memory_review_apply", "memory_stage", "notion_outbox_list",
    "notion_outbox_settle", "responsibility_done", "responsibility_due", "responsibility_track"]);
  await tools.memory_stage.execute("1", { text: "Owner's dog is Archie", kind: "fact" });
  assert.match((await tools.memory_review.execute("2", {})).content[0].text, /1\. \[fact\] Owner's dog is Archie/);
  await tools.memory_review_apply.execute("3", { decisions: [{ n: 1, action: "approve" }] });
  assert.match(readFileSync(join(d, "MEMORY.md"), "utf8"), /Archie/);
  const bad = await tools.responsibility_track.execute("4", { name: "x", cadence: "never" });
  assert.equal(bad.details.ok, false);                                        // errors come back, never thrown
  writeFileSync(join(d, "o.json"), JSON.stringify({ items: [] }));
  assert.deepEqual((await tools.notion_outbox_list.execute("5", {})).details, []);
});
