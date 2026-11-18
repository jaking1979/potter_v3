import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { cleanFacts, readScreen, route } from "../lib/capture.js";
import { decideStaged, run } from "../scripts/kg-capture.mjs";
import { seeded } from "./fixture.mjs";

// A fake Jev that answers by question name, and a fake extractor with fixed facts.
function fakeDecide({ remember = 0.9, owner = 0.9, supported = 0.9, same = 2, replaces = 0.1 } = {}) {
  return async ({ questions }) => {
    const a = {};
    for (const [k, q] of Object.entries(questions)) {
      if (k === "remember") a[k] = { noul: remember };
      else if (k === "owner_stated") a[k] = { noul: owner };
      else if (k === "sensitive") a[k] = { noul: 0.02 };
      else if (k === "kind") a[k] = { choice: "durable", confidence: 0.9 };
      else if (k === "supported") a[k] = { noul: supported };
      else if (k === "any_fits") a[k] = { noul: 0.9 };
      else if (k === "replaces") a[k] = { noul: replaces };
      else if (k === "predicate") a[k] = { choice: "none", confidence: 0.1 };
      else if (k.startsWith("same_")) a[k] = { score: same, confidence: 0.9 };
      else if (q.type === "noul") a[k] = { noul: 0.5 };
    }
    return { ok: true, answers: a, costUsd: 0.0001 };
  };
}
const extractWith = (facts) => async () => ({ ok: true, json: { facts }, costUsd: 0.0002 });
const F = (s, st, p, o, ot, quote, until = "") => ({ subject: { name: s, type: st, description: "" }, predicate: p, object: { name: o, type: ot, description: "" }, quote, until });

function setup(lines) {
  const dir = mkdtempSync(join(tmpdir(), "kgc-"));
  const queuePath = join(dir, "q.jsonl"), stagedPath = join(dir, "staged.jsonl");
  writeFileSync(queuePath, lines.map((t) => JSON.stringify({ ts: "2026-11-18T12:00:00Z", text: t }) + "\n").join(""));
  writeFileSync(stagedPath, "");
  return { queuePath, stagedPath };
}

test("quotes must really be in the message; unknown types and predicates are dropped", () => {
  const msg = "Bo started at the Hill School this fall.";
  const out = cleanFacts({ facts: [
    F("Bo Example", "person", "is_part_of", "Hill School", "place", "Bo started at the Hill School"),
    F("Bo Example", "person", "uses", "Hill School", "place", "Bo loves the Hill School"),        // invented quote
    F("Bo Example", "alien", "uses", "Hill School", "place", "Bo started"),
    F("Bo Example", "person", "teleports_to", "Hill School", "place", "Bo started"),
  ] }, msg);
  assert.equal(out.length, 1);
});

test("screen: questions, chit-chat, one-offs and clinical material never get past the first request", () => {
  assert.ok(readScreen({ remember: { noul: 0.9 }, sensitive: { noul: 0.1 }, kind: { choice: "durable" } }).proceed);
  assert.ok(!readScreen({ remember: { noul: 0.2 }, sensitive: { noul: 0.1 }, kind: { choice: "durable" } }).proceed);
  assert.ok(!readScreen({ remember: { noul: 0.9 }, sensitive: { noul: 0.8 }, kind: { choice: "durable" } }).proceed);
  assert.ok(!readScreen({ remember: { noul: 0.9 }, sensitive: { noul: 0.1 }, kind: { choice: "one_off" } }).proceed);
});

test("route: supported and owner-stated is written high; uncertain is staged; unsupported or no fit is dropped", () => {
  const f = F("A", "person", "uses", "B", "system", "x");
  assert.deepEqual(route(f, { supported: { noul: 0.9 } }, { ownerStated: 0.9 }).confidence, "high");
  assert.equal(route(f, { supported: { noul: 0.9 } }, { ownerStated: 0.2 }).confidence, "medium");
  assert.equal(route(f, { supported: { noul: 0.5 } }, {}).action, "stage");
  assert.equal(route(f, { supported: { noul: 0.1 } }, {}).action, "drop");
  assert.equal(route(f, { supported: { noul: 0.9 }, any_fits: { noul: 0.1 } }, {}).action, "drop");
  assert.equal(route(f, { supported: { noul: 0.9 }, predicate: { choice: "integrates_with", confidence: 0.8 } }, {}).predicate, "integrates_with");
});

test("a stated fact is written with its quote; I means the owner; a new place is created; the queue empties", async () => {
  const store = seeded();
  const { queuePath, stagedPath } = setup(["I'm now responsible for draining the cabin pipes before winter."]);
  const rec = await run({ store, queuePath, stagedPath, decide: fakeDecide(),
    extract: extractWith([F("Ada Example", "person", "responsible_for", "Draining the cabin pipes", "concept", "responsible for draining the cabin pipes")]) });
  assert.equal(rec.written, 1);
  const r = store.relationsOf("person:ada").find((x) => x.predicate === "responsible_for");
  assert.equal(r.confidence, "high");
  assert.match(r.provenance[0], /^chat 2026-11-18: "responsible for draining the cabin pipes"/);
  assert.equal(readFileSync(queuePath, "utf8"), "");
});

test("an existing entity is matched by name, not duplicated; a move replaces the old address", async () => {
  const store = seeded();
  const { queuePath, stagedPath } = setup(["Bo moved, he lives at the Hill Cabin now."]);
  store.upsertEntity({ id: "place:flat", name: "City Flat", provenance: ["seed"] });
  store.addRelation({ source_id: "person:bo", predicate: "lives_at", target_id: "place:flat", provenance: ["seed"] });
  await run({ store, queuePath, stagedPath, decide: fakeDecide({ replaces: 0.9 }),
    extract: extractWith([F("Bo Example", "person", "lives_at", "Hill Cabin", "place", "he lives at the Hill Cabin now")]) });
  const lives = store.relationsOf("person:bo").filter((x) => x.predicate === "lives_at" && x.source_id === "person:bo");
  assert.deepEqual(lives.map((x) => x.target_id), ["place:cabin"]);              // the old address is archived
  assert.equal(store.search("Hill Cabin", { type: "place" }).length, 1);
});

test("uncertain facts wait for the owner; approve writes them high, reject never writes", async () => {
  const store = seeded();
  const { queuePath, stagedPath } = setup(["I think Bo might like the new sleep course."]);
  const rec = await run({ store, queuePath, stagedPath, decide: fakeDecide({ supported: 0.5 }),
    extract: extractWith([F("Bo Example", "person", "interested_in", "Sleep Course", "resource", "Bo might like the new sleep course")]) });
  assert.equal(rec.staged, 1);
  const [row] = readFileSync(stagedPath, "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.match(row.text, /Bo Example interested in Sleep Course/);
  assert.deepEqual(decideStaged(store, stagedPath, [row.id], true), [{ id: row.id, status: "approved" }]);
  assert.ok(store.relationsOf("person:bo").some((x) => x.predicate === "interested_in" && x.confidence === "high"));
  assert.deepEqual(decideStaged(store, stagedPath, [row.id], false), []);           // already decided
});

test("nothing to remember costs one screening call and writes nothing; the cost cap stops a run", async () => {
  const store = seeded();
  const { queuePath, stagedPath } = setup(["what's the weather?", "thanks"]);
  let extracted = 0;
  const rec = await run({ store, queuePath, stagedPath, decide: fakeDecide({ remember: 0.1 }), extract: async () => { extracted++; return { ok: true, json: { facts: [] } }; } });
  assert.equal(extracted, 0);
  assert.equal(rec.written + rec.staged, 0);
  const q2 = setup(["a", "b", "c"]);
  const capped = await run({ store, ...q2, decide: async () => ({ ok: true, answers: {}, costUsd: 1 }), extract: extractWith([]), maxUsd: 1 });
  assert.ok(capped.stoppedAtCostCap);
  assert.equal(readFileSync(q2.queuePath, "utf8").trim().split("\n").length, 2);    // unprocessed lines stay
});
