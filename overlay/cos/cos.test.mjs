import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { cleanForecast, confidenceOf, outcomeRequest, patterns, readOutcomes, weekSummary } from "./lib.js";
import { main } from "./score.mjs";
import { seeded } from "../plugins/potter-kg/tests/fixture.mjs";

const day = (date, preds, outcomes, evShape = "normal", shape = "normal") =>
  ({ date, shape, predictions: preds, evening: { text: "x", shape: evShape }, outcomes });
const P = (id, item, type = "will_happen", confidence = "medium") => ({ id, claim: `${item} gets done`, item, type, confidence });

test("forecasts are cleaned: bad dates rejected, unknown fields defaulted, empty evenings ignored", () => {
  assert.equal(cleanForecast({ date: "tomorrow" }), null);
  const f = cleanForecast({ date: "2026-11-19", shape: "packed", predictions: [{ claim: "Order the pork loin", item: "Pork loin order!", type: "weird", confidence: "very" }],
    evening: { text: "  " } });
  assert.deepEqual(f.predictions[0], { id: "p1", claim: "Order the pork loin", item: "pork loin order", type: "will_happen", confidence: "medium" });
  assert.equal(f.evening, null);
});

test("outcomes: one narrow pair of questions per prediction; not mentioned is unknown, never a miss", () => {
  const f = cleanForecast({ date: "2026-11-19", predictions: [P("a", "pork loin order"), P("b", "taxes")], evening: { text: "Ordered the pork loin." } });
  const req = outcomeRequest(f);
  assert.deepEqual(Object.keys(req.questions), ["came_true_0", "mentioned_0", "came_true_1", "mentioned_1"]);
  const o = readOutcomes(f, { came_true_0: { noul: 0.9 }, mentioned_0: { noul: 0.95 }, came_true_1: { noul: 0.1 }, mentioned_1: { noul: 0.1 } });
  assert.deepEqual(o, { a: "hit", b: "unknown" });
});

test("patterns: an item that keeps slipping, a weekday that runs packed; one-offs and successes are not patterns", () => {
  const mk = (date, preds, outcomes, evShape) => ({ ...cleanForecast(day(date, preds, null, evShape)), outcomes });
  const fs = [                                                                                    // all Thursdays
    mk("2026-10-29", [P("a", "pork loin order", "will_happen", "high")], { a: "miss" }, "packed"),
    mk("2026-11-05", [P("a", "pork loin order", "will_happen", "high")], { a: "miss" }, "packed"),
    mk("2026-11-12", [P("a", "pork loin order"), P("b", "gym")], { a: "hit", b: "hit" }, "packed"),
    mk("2026-11-19", [P("a", "gym"), P("b", "bills", "at_risk")], { a: "hit", b: "miss" }, "packed"),
  ];
  const ps = patterns(fs, { today: "2026-11-19" });
  const names = ps.map((p) => p.name);
  assert.ok(names.includes("Pork loin order keeps slipping"), names.join("; "));
  assert.equal(ps.find((p) => p.name === "Pork loin order keeps slipping").confidence, "medium");   // 2 of 3
  assert.equal(ps.find((p) => p.key === "weekday-thursday-packed").confidence, "high");            // 4 of 4
  assert.ok(!names.some((n) => /gym|bills/i.test(n)));
  assert.ok(!ps.some((p) => p.key === "calibration-high"));                                       // too few high forecasts
  assert.equal(confidenceOf(1, 1), null);
});

test("week summary: accuracy over scored forecasts and what slipped", () => {
  const fs = [day("2026-11-17", [P("p1", "a"), P("p2", "b")], { p1: "hit", p2: "miss" }), day("2026-11-18", [P("p1", "b")], { p1: "miss" })].map((f) => ({ ...cleanForecast(f), outcomes: f.outcomes }));
  const w = weekSummary(fs, { today: "2026-11-19" });
  assert.deepEqual({ hit: w.hit, miss: w.miss, accuracy: w.accuracy }, { hit: 1, miss: 2, accuracy: 33 });
  assert.deepEqual(w.slipped[0], { item: "b", times: 2 });
});

test("the job scores evenings with Jev, writes patterns to the graph, and archives patterns that lose their evidence", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cos-"));
  const fdir = join(dir, "forecasts"); mkdirSync(fdir);
  const db = join(dir, "g.sqlite");
  const s0 = seeded(); s0.db.exec(`VACUUM INTO '${db}'`); s0.close();
  for (const d of ["2026-11-03", "2026-11-10", "2026-11-17"]) {
    writeFileSync(join(fdir, `${d}.json`), JSON.stringify({ date: d, shape: "normal", predictions: [{ claim: "Bills get paid", item: "bills" }],
      evening: { text: "Didn't get to the bills, packed day.", shape: "packed" } }));
  }
  const decider = join(dir, "decider.mjs");
  writeFileSync(decider, `export function createDecider() { return async ({ questions }) => ({ ok: true, answers: Object.fromEntries(Object.keys(questions).map((k) => [k, { noul: k.startsWith("mentioned") ? 0.9 : 0.1 }])) }); }`);
  const out = [];
  const io = { out: (x) => out.push(x), err: (x) => out.push(x) };
  assert.equal(await main(["--forecasts", fdir, "--db", db, "--decider", decider, "--policy", "x", "--owner", "person:ada", "--report", "--today", "2026-11-18"], {}, io), 0);
  const rep = JSON.parse(out.pop());
  assert.equal(rep.scored, 3);
  assert.ok(rep.patterns.some((p) => p.startsWith("Bills keeps slipping (medium, 3/3)") || p.startsWith("Bills keeps slipping (high, 3/3)")), rep.patterns.join("; "));
  assert.ok(rep.patterns.some((p) => p.startsWith("Tuesdays run packed")), rep.patterns.join("; "));
  assert.equal(JSON.parse(readFileSync(join(fdir, "2026-11-10.json"), "utf8")).outcomes.p1, "miss");
  const { openStore } = await import("../plugins/potter-kg/lib/store.js");
  let st = openStore(db);
  assert.ok(st.relationsOf("person:ada").some((r) => r.predicate === "has_pattern" && r.target_id === "pattern:slip-bills"));
  st.close();
  // far in the future the evidence window is empty: the patterns are archived, not deleted
  assert.equal(await main(["--forecasts", fdir, "--db", db, "--owner", "person:ada", "--report", "--today", "2027-06-01"], {}, io), 0);
  assert.equal(JSON.parse(out.pop()).archived, 2);
  st = openStore(db);
  assert.equal(st.getEntity("pattern:slip-bills").status, "archived");
  st.close();
});
