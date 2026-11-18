// v3.6 Brain v1: the working context built before the model thinks.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { assemble, buildIndex, fresh, names, passages, search, stateBlock, tokens } from "./lib/context.js";

function workspace() {
  const ws = mkdtempSync(join(tmpdir(), "brain-"));
  const w = (rel, text) => { mkdirSync(join(ws, rel, ".."), { recursive: true }); writeFileSync(join(ws, rel), text); };
  w("USER.md", "# Owner\nAlready injected every turn: never recalled.");
  w("preferences/food-wine.md", "# Food\n\n## Wines We Like\nA California Pinot Noir both of us like.\n\n## Retrieval Triggers\nwine dinner food restaurant order eat cook meal");
  w("rules/people/owner.md", "# People\n\n## Key People\nWife Dana is a dance teacher. Twins Ava and Mia; Mia races on the ski team.\n\n## Work\nThe owner runs a clinic.");
  w("rules/domains/email.md", "# Email\n\n## Tool usage\n```\ntool name: agentmail__send_message\nparameters: to: dana@example.com\n```\n\n## Purpose\nHow email is handled for the owner.");
  w("rules/global/email-pipeline.md", "# Email pipeline\nOne inbox; urgent mail goes to the queue.");
  w("rules/INDEX.md", "| trigger | file |\n| wine | food-wine |");
  w("reference/v2/old.md", "# Old\n\n## Wines\nThe previous assistant's notes about wine and dinner.");
  w("state/responsibilities.json", JSON.stringify({ items: [{ name: "Drain spigots", next_due: "2026-11-19" }, { name: "Taxes", next_due: "2027-04-01" }] }));
  w("state/memory/staged.jsonl", '{"id":"m1","status":"pending"}\n{"id":"m2","status":"approved"}\n');
  return ws;
}

test("tokens and names", () => {
  assert.deepEqual(tokens("What wines does Dana like?"), ["wine", "dana", "like"]);
  assert.deepEqual([...names("How is Mia doing with skiing? Ask Dana.")], ["mia", "ask", "dana"].filter((x) => x !== "ask"));
});

test("passages split by heading and skip near-empty sections", () => {
  const p = passages("x.md", "# T\n\n## A\nshort but long enough to keep here\n\n## B\n\n## C\nanother passage with words in it");
  assert.deepEqual(p.map((x) => x.head), ["A", "C"]);
});

test("only the owner's own files are indexed: not what is injected anyway, not machinery or v2 reference", () => {
  const idx = buildIndex(workspace());
  const paths = new Set(idx.docs.map((d) => d.path));
  assert.ok(paths.has("preferences/food-wine.md") && paths.has("rules/people/owner.md"));
  for (const no of ["USER.md", "rules/INDEX.md", "rules/global/email-pipeline.md", "reference/v2/old.md"]) assert.ok(!paths.has(no), no);
  assert.ok(!idx.docs.some((d) => /Retrieval Triggers/.test(d.head)));          // keyword lists match everything
});

test("names carry the meaning; code and tool syntax rank below knowledge", () => {
  const idx = buildIndex(workspace());
  assert.equal(search(idx, "How is Mia doing with ski racing?")[0].head, "Key People");
  const dana = search(idx, "what does Dana do for work");
  assert.equal(dana[0].head, "Key People");                                    // not the email code sample naming her
  assert.equal(search(idx, "zebra quantum").length, 0);
});

test("the index notices a changed file", () => {
  const ws = workspace();
  const idx = buildIndex(ws);
  assert.ok(fresh(idx));
  writeFileSync(join(ws, "preferences/new.md"), "# New\n\n## Section\nSomething entirely new about kayaks.");
  assert.ok(!fresh(idx));
});

test("state: local time, what is due soon, what waits for the owner", () => {
  const s = stateBlock(workspace(), { now: new Date("2026-11-18T01:00:00Z"), tz: "America/New_York" });
  assert.match(s, /Tuesday, Nov 17, 2026, 8:00 PM \(America\/New_York\)/);
  assert.match(s, /Due soon: Drain spigots \(2026-11-19\)\./);
  assert.ok(!s.includes("Taxes"));
  assert.match(s, /1 proposed memory waits/);
});

test("assemble: recall plus the domain's rules in full, inside the budget; nothing relevant adds nothing but state", () => {
  const ws = workspace();
  const idx = buildIndex(ws);
  const a = assemble({ text: "What should I order for dinner?", expand: ["wine", "food"], rules: ["global/email-pipeline.md"], index: idx, ws, tz: "UTC", budget: 3000 });
  assert.match(a.context, /Wines We Like/);
  assert.match(a.context, /--- rules\/global\/email-pipeline\.md ---\n# Email pipeline\nOne inbox/);
  assert.ok(a.chars <= 3300, a.chars);
  assert.deepEqual(a.sources.map((s) => s.path), ["preferences/food-wine.md"]);
  const none = assemble({ text: "zebra quantum", index: idx, ws, tz: "UTC" });
  assert.equal(none.sources.length, 0);
  assert.ok(!none.context.includes("What you already know"));
  const tiny = assemble({ text: "What wines do we like?", index: idx, ws, tz: "UTC", budget: 200 });
  assert.equal(tiny.sources.length, 0);                                         // over budget: left out, never cut mid-way
});
