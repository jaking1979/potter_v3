import test from "node:test";
import assert from "node:assert/strict";
import { curate, DEFAULTS, scan } from "../lib/curate.js";
import { seeded, T0 } from "./fixture.mjs";

const DAY = 86400000;
const later = (days) => new Date(T0 + days * DAY);

/** A decider that answers from a table keyed by pass, and records what it was asked. */
function scripted(answers, log = []) {
  return async (req) => {
    log.push(req);
    const kind = req.purpose.split(".").pop();
    const a = typeof answers[kind] === "function" ? answers[kind](req) : answers[kind];
    return a ? { ok: true, answers: { decision: { type: "choice", ...a } } } : { ok: false };
  };
}

function withDuplicate(clock = { t: T0 }) {
  const store = seeded(clock);
  store.upsertEntity({ id: "concept:sleep-therapy", name: "Sleep therapy" });
  store.addRelation({ source_id: "person:bo", predicate: "interested_in", target_id: "concept:sleep-therapy" });
  return store;
}

test("scan: orphans only after the grace period, same-name duplicates, weak relations", () => {
  const store = withDuplicate();
  assert.deepEqual(scan(store, { now: later(1) }).orphans, [], "a day-old entity is not yet an orphan");
  const s = scan(store, { now: later(8) });
  assert.deepEqual(s.orphans.map((e) => e.id), ["interest:baking"]);
  assert.deepEqual(s.aliasConflicts.map((c) => c.ids), [["concept:cbti", "concept:sleep-therapy"]]);
  assert.equal(s.weakRelations.length, 8);
  assert.deepEqual(s.stats, { entities: 12, relations: 9, archivedEntities: 0, orphanCount: 1 });
});

test("without a decider: deterministic run, nothing asked, nothing changed, ok", async () => {
  const store = withDuplicate();
  const before = store.stats();
  const run = await curate(store, { now: later(8), apply: true });
  assert.equal(run.mode, "deterministic");
  assert.deepEqual(run.found, { orphans: 1, aliasConflicts: 1, weakRelations: 8 });
  assert.deepEqual([run.calls, run.errors, run.decisions, run.actions, run.ok], [0, 0, [], [], true]);
  assert.deepEqual(store.stats(), before);
  assert.deepEqual(run.after, run.before);
});

test("propose mode asks but writes nothing, not even review marks", async () => {
  const store = withDuplicate();
  const before = store.stats();
  const run = await curate(store, {
    now: later(8),
    decide: scripted({ orphan: { choice: "archive", confidence: 0.99 }, alias: { choice: "merge", confidence: 0.99 }, relation: { choice: "reject", confidence: 0.99 } }),
  });
  assert.equal(run.mode, "propose");
  assert.equal(run.calls, 10);
  assert.equal(run.decisions.length, 10);
  assert.deepEqual(run.actions, []);
  assert.deepEqual(store.stats(), before);
  assert.equal(store.getMeta("curator:orphan:interest:baking"), null);
});

test("apply: archives a confident orphan, merges a confident duplicate onto the better-connected record, upgrades and rejects relations", async () => {
  const store = withDuplicate();
  const asked = [];
  const run = await curate(store, {
    now: later(8), apply: true,
    decide: scripted({
      orphan: { choice: "archive", confidence: 0.9 },
      alias: { choice: "merge", confidence: 0.8 },
      relation: (req) => req.state.relation.predicate === "married_to" ? { choice: "upgrade", confidence: 0.9 }
        : req.state.confidence === "low" ? { choice: "reject", confidence: 0.85 } : { choice: "keep", confidence: 0.9 },
    }, asked),
  });
  assert.equal(run.mode, "apply");
  assert.deepEqual(run.actions.map((a) => a.action).sort(), ["archive-entity", "archive-relation", "merge"]);
  assert.equal(store.getEntity("interest:baking").status, "archived");
  assert.deepEqual(run.actions.find((a) => a.action === "merge"), { action: "merge", kept: "concept:cbti", archived: "concept:sleep-therapy" });
  assert.equal(store.getRelation("person:bo", "interested_in", "concept:cbti").status, "active");
  assert.equal(store.getRelation("concept:overthinking", "connects_to", "concept:stoicism").status, "archived");
  assert.ok(run.ok);
  assert.equal(run.after.entities, 10);
  // What the decision model saw is bounded: one question per call, named by pass.
  for (const req of asked) assert.deepEqual(Object.keys(req.questions), ["decision"]);
  assert.deepEqual([...new Set(asked.map((r) => r.purpose))], ["potter-kg.curate.orphan", "potter-kg.curate.alias", "potter-kg.curate.relation"]);
});

test("an upgrade sets high confidence, after which the relation is no longer asked about", async () => {
  const store = seeded();
  const run = await curate(store, { now: later(1), apply: true, decide: scripted({ relation: { choice: "upgrade", confidence: 0.7 } }) });
  assert.equal(run.actions.length, 7);
  assert.equal(store.weakRelations().length, 0);
  assert.ok(store.getRelation("person:ada", "lives_at", "place:cabin").provenance.includes("curator-upgrade"));
  const again = await curate(store, { now: later(1), apply: true, decide: scripted({}) });
  assert.equal(again.calls, 0);
});

test("answers below their threshold change nothing and are not asked again until the review period ends", async () => {
  const store = withDuplicate();
  const weak = { orphan: { choice: "archive", confidence: 0.59 }, alias: { choice: "merge", confidence: 0.69 }, relation: { choice: "reject", confidence: 0.79 } };
  const first = await curate(store, { now: later(8), apply: true, decide: scripted(weak) });
  assert.deepEqual([first.calls, first.actions.length], [10, 0]);
  assert.equal(store.getMeta("curator:alias:concept:cbti|concept:sleep-therapy"), later(8).toISOString());
  const second = await curate(store, { now: later(9), apply: true, decide: scripted(weak) });
  assert.equal(second.calls, 0, "v2 asked the same questions every six hours for ever");
  const third = await curate(store, { now: later(8 + DEFAULTS.reviewDays), apply: true, decide: scripted(weak) });
  assert.equal(third.calls, 10);
});

test("confidence may come from the probabilities map; an answer outside the rubric counts as an error", async () => {
  const store = seeded();
  const run = await curate(store, {
    now: later(8), apply: true, maxCalls: 2,
    decide: scripted({ orphan: { choice: "archive", probabilities: { archive: 0.95, keep: 0.05 } }, relation: { choice: "delete-everything", confidence: 1 } }),
  });
  assert.equal(store.getEntity("interest:baking").status, "archived");
  assert.deepEqual([run.calls, run.errors, run.stopped], [2, 1, "call-budget"]);
  assert.equal(store.stats().relations, 8);
});

test("the call budget is a hard cap", async () => {
  const run = await curate(seeded(), { now: later(8), apply: true, maxCalls: 3, decide: scripted({ orphan: { choice: "keep", confidence: 1 }, relation: { choice: "keep", confidence: 1 } }) });
  assert.deepEqual([run.calls, run.stopped, run.ok], [3, "call-budget", true]);
  const zero = await curate(seeded(), { now: later(8), maxCalls: 0, decide: scripted({}) });
  assert.deepEqual([zero.calls, zero.stopped, zero.ok], [0, "call-budget", true]);
});

test("a failing decider: stops after three errors in a row, changes nothing, and the run is not ok", async () => {
  const store = withDuplicate();
  const before = store.stats();
  for (const decide of [scripted({}), async () => { throw new Error("network down"); }, async () => null]) {
    const run = await curate(store, { now: later(8), apply: true, decide });
    assert.deepEqual([run.calls, run.errors, run.stopped, run.ok], [3, 3, "decision-errors", false]);
    assert.deepEqual(run.actions, []);
  }
  assert.deepEqual(store.stats(), before);
});

test("one failure among successes is tolerated", async () => {
  let n = 0;
  const flaky = async (req) => (++n === 2 ? { ok: false } : scripted({ orphan: { choice: "keep", confidence: 1 }, relation: { choice: "keep", confidence: 1 } })(req));
  const run = await curate(seeded(), { now: later(8), apply: true, decide: flaky });
  assert.deepEqual([run.errors, run.stopped, run.ok], [1, null, true]);
  assert.equal(run.calls, 8);
});

test("the run record holds ids and counts only: no names, descriptions or aliases", async () => {
  const store = withDuplicate();
  const run = await curate(store, { now: later(8), apply: true, decide: scripted({ orphan: { choice: "archive", confidence: 1 }, alias: { choice: "merge", confidence: 1 }, relation: { choice: "keep", confidence: 1 } }) });
  const text = JSON.stringify(run);
  for (const leak of ["Ada Example", "Weekend house", "the owner", "Rumination"]) assert.equal(text.includes(leak), false, leak);
  assert.equal(run.at, later(8).toISOString());
});
