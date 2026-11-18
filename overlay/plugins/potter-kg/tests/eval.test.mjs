import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { findOwner, metrics, readV3, sample, v3Questions } from "../lib/eval.js";
import { loadGraph, main, runEval, summaryText } from "../scripts/kg-eval.mjs";
import { openStore } from "../lib/store.js";
import { seeded } from "./fixture.mjs";

function graphOf(store) {
  const entities = store.db.prepare("SELECT id, type, name, description, status FROM entities").all();
  const relations = store.db.prepare("SELECT source_id, predicate, target_id, status, confidence FROM relations").all();
  return { entities, relations };
}

// A fake decision model that "knows" the real edges: yes for the true predicate, no otherwise.
function oracle(graph) {
  const truth = new Map(graph.relations.map((r) => [`${r.source_id}|${r.target_id}`, r.predicate]));
  return async ({ state, questions }) => {
    const p = truth.get(`${state.a.id}|${state.b.id}`) ?? "none";
    const answers = {};
    for (const [k, q] of Object.entries(questions)) {
      if (q.type === "choice") answers[k] = { choice: p, confidence: 0.9, probabilities: { [p]: 0.9 } };
      else if (k.startsWith("is_")) answers[k] = { noul: k === `is_${p}` ? 0.9 : 0.05 };
      else answers[k] = { noul: p === "none" ? 0.1 : 0.9 };
    }
    return { ok: true, answers, costUsd: 0.0001, latencyMs: 5 };
  };
}

test("sampling spreads positives across predicates, keeps negatives truly unlinked, and is reproducible", () => {
  const g = graphOf(seeded());
  const s = sample({ ...g, pairs: 4, negatives: 3, seed: 3 });
  const pos = s.filter((x) => x.expectRelated);
  assert.equal(pos.length, 4);
  assert.equal(new Set(pos.map((x) => x.expectPredicate)).size, 4);              // round robin: 4 different predicates
  const edge = new Set(g.relations.map((r) => [r.source_id, r.target_id].sort().join("|")));
  for (const n of s.filter((x) => !x.expectRelated)) assert.ok(!edge.has([n.a, n.b].sort().join("|")));
  assert.deepEqual(sample({ ...g, pairs: 4, negatives: 3, seed: 3 }), s);
});

test("the owner is the most connected person", () => {
  const g = graphOf(seeded());
  assert.equal(findOwner(g.entities, g.relations).id, "person:ada");
});

test("v3 asks one narrow question per predicate plus the direct-link, choice and companion questions", () => {
  const q = v3Questions();
  assert.ok(q.direct_link && q.predicate && q.any_fits && q.is_interested_in && q.is_parent_of);
  assert.equal(q.is_lives_at.type, "noul");
  const r = readV3({ direct_link: { noul: 0.2 }, is_uses: { noul: 0.8 }, is_runs: { noul: 0.4 } });
  assert.equal(r.related, 0.8);                                                   // composed in code: strongest signal
  assert.equal(r.predicate, "uses");
});

test("a perfect model scores perfectly on both arms; the summary reads cleanly", async () => {
  const g = graphOf(seeded());
  const res = await runEval(g, oracle(g), { pairs: 8, negatives: 4 });
  for (const arm of ["v2", "v3"]) {
    const m = res.summary.arms[arm];
    assert.equal(m.at05.recall, 1, arm);
    assert.equal(m.at05.precision, 1, arm);
    assert.equal(m.predicateTop1, 1, arm);
  }
  assert.match(summaryText(res.summary), /^KG eval: \d+ pairs/);
});

test("the cost cap stops the run; a failing model is counted, not thrown", async () => {
  const g = graphOf(seeded());
  const capped = await runEval(g, async () => ({ ok: true, answers: {}, costUsd: 1 }), { pairs: 8, negatives: 4, maxUsd: 1, concurrency: 1 });
  assert.ok(capped.summary.stoppedAtCostCap && capped.summary.calls === 1);
  const down = await runEval(g, async () => ({ ok: false, reason: "http:500" }), { pairs: 2, negatives: 1 });
  assert.equal(down.summary.failures, down.summary.calls);
  assert.equal(metrics([]).answered, 0);
});

test("the program reads the graph read-only and refuses to run without a decider", async () => {
  const dir = mkdtempSync(join(tmpdir(), "kgeval-"));
  const path = join(dir, "g.sqlite");
  const s = openStore(path);
  s.upsertEntity({ id: "person:ada", name: "Ada", provenance: ["t"] });
  s.close?.();
  const g = await loadGraph(path);
  assert.equal(g.entities.length, 1);
  const errs = [];
  assert.equal(await main(["--db", path], {}, { out: () => {}, err: (x) => errs.push(x) }), 3);
  assert.equal(await main([], {}, { out: () => {}, err: () => {} }), 2);
});
