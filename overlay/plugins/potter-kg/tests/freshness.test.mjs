import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { plan, tags } from "../lib/freshness.js";
import { answer, run } from "../scripts/kg-freshness.mjs";
import { seeded } from "./fixture.mjs";

const T = "2026-11-18";
const rel = (id, p, prov, created = "2026-01-01") => ({ id, source_id: "person:a", predicate: p, target_id: `x:${id}`, provenance: prov, created });

test("tags: until, kind, confirmed and the capture date come from provenance", () => {
  assert.deepEqual(tags(['chat 2026-05-01: "x" until:2026-06-30 kind:situational', "confirmed:2026-07-01", "confirmed:2026-08-01"]),
    { until: "2026-06-30", kind: "situational", confirmed: "2026-08-01", captured: "2026-05-01" });
});

test("plan: expired facts go; old temporary facts and old preferences are asked; recent confirmations and re-asks wait", () => {
  const relations = [
    rel(1, "is_part_of", ['chat 2026-01-01: "on the team until June" until:2026-06-30 kind:situational']),
    rel(2, "is_part_of", ['chat 2026-06-01: "on the U14 team" kind:situational']),
    rel(3, "prefers", ['chat 2025-06-01: "I like Pinot" kind:preference']),
    rel(4, "married_to", ['chat 2025-01-01: "married" kind:durable']),
    rel(5, "is_part_of", ['chat 2026-06-01: "x" kind:situational', "confirmed:2026-10-01"]),
    rel(6, "is_part_of", ['chat 2026-06-01: "x" kind:situational']),
  ];
  const p = plan({ relations, today: T, asked: new Map([["person:a|is_part_of|x:6", "2026-11-01"]]) });
  assert.deepEqual(p.expire.map((x) => x.r.id), [1]);
  assert.deepEqual(p.ask.map((x) => x.r.id).sort(), [2, 3]);
});

test("plan: a used imported fact with no kind is judged by Jev first; only time-bound ones are asked; at most five a week", () => {
  const relations = [rel(7, "works_on", ["v2"]), rel(8, "married_to", ["v2"])];
  const usage = new Map([["person:a|works_on|x:7", "2026-11-10"], ["person:a|married_to|x:8", "2026-11-10"]]);
  assert.equal(plan({ relations, usage, today: T }).needJudgement.length, 2);
  const timeBound = new Map([["person:a|works_on|x:7", 0.9], ["person:a|married_to|x:8", 0.05]]);
  assert.deepEqual(plan({ relations, usage, timeBound, today: T }).ask.map((x) => x.r.id), [7]);
  const many = Array.from({ length: 9 }, (_, i) => rel(20 + i, "is_part_of", ['chat 2026-01-01: "x" kind:situational']));
  assert.equal(plan({ relations: many, today: T }).ask.length, 5);
});

test("the job archives expired facts, writes questions, and the owner's answers confirm or archive", async () => {
  const store = seeded();
  store.addRelation({ source_id: "person:bo", predicate: "interested_in", target_id: "interest:baking", provenance: ['chat 2026-01-01: "baking this winter" until:2026-03-01 kind:situational'] });
  store.addRelation({ source_id: "person:ada", predicate: "uses", target_id: "resource:sleep-course", provenance: ['chat 2026-02-01: "using the sleep course" kind:situational'] });
  const dir = mkdtempSync(join(tmpdir(), "fr-"));
  const reviewPath = join(dir, "review.jsonl");
  writeFileSync(reviewPath, "");
  const rec = await run({ store, reviewPath, decide: null, today: T });
  assert.equal(rec.expired, 1);
  assert.ok(!store.relationsOf("person:bo").some((r) => r.predicate === "interested_in"));
  const q = readFileSync(reviewPath, "utf8").trim().split("\n").map((l) => JSON.parse(l)).find((x) => x.status === "pending");
  assert.match(q.text, /Ada Example uses Sleep Course/);
  assert.deepEqual(answer(store, reviewPath, [q.id], true, T), [{ id: q.id, status: "kept" }]);
  const r = store.getRelationById(q.relId);
  assert.equal(r.confidence, "high");
  assert.ok(r.provenance.includes(`confirmed:${T}`));
  const again = await run({ store, reviewPath, decide: null, today: T });
  assert.equal(again.asked, 0);                                                  // just confirmed: not asked again
});
