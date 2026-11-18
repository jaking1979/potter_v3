import test from "node:test";
import assert from "node:assert/strict";
import * as v from "../lib/vocab.js";
import * as a from "../lib/alias.js";
import { resolveDbPath } from "../lib/paths.js";

test("vocabulary: 28 predicates, each with a definition, plus 'none' for the decision rubric", () => {
  assert.equal(v.PREDICATES.length, 28);
  assert.equal(Object.keys(v.PREDICATE_CRITERIA).length, 29);
  assert.ok(v.PREDICATE_CRITERIA.none);
  assert.ok(!v.PREDICATES.includes("none"));
  for (const p of v.PREDICATES) assert.ok(v.PREDICATE_CRITERIA[p].length > 5, p);
  assert.equal(new Set(v.ENTITY_TYPES).size, v.ENTITY_TYPES.length);
});

test("entity ids are '<known type>:<slug>' and the type must agree with the id", () => {
  assert.deepEqual(v.parseEntityId("person:ada"), { type: "person", slug: "ada" });
  for (const bad of ["ada", "person:", ":ada", "Person:ada", "person:Ada", "wizard:ada", "person:a b", 7, null, "person:" + "a".repeat(300)]) {
    assert.equal(v.parseEntityId(bad), null, String(bad));
  }
  assert.ok(v.assertValidEntity({ id: "person:ada", name: "Ada" }));
  assert.throws(() => v.assertValidEntity({ id: "person:ada", type: "place", name: "Ada" }), /does not match/);
  assert.throws(() => v.assertValidEntity({ id: "person:ada", name: " " }), /name is required/);
  assert.throws(() => v.assertValidEntity({ id: "person:ada", name: "Ada", confidence: "certain" }), /confidence/);
  assert.throws(() => v.assertValidEntity({ id: "person:ada", name: "Ada", aliases: "Ada" }), /aliases/);
  assert.throws(() => v.assertValidEntity({ id: "person:ada", name: "Ada", status: "deleted" }), /status/);
});

test("relations: fixed predicates, no self-loops, valid confidence", () => {
  const r = { source_id: "person:ada", predicate: "lives_at", target_id: "place:cabin" };
  assert.ok(v.assertValidRelation(r));
  assert.throws(() => v.assertValidRelation({ ...r, predicate: "likes" }), /predicate must be one of/);
  assert.throws(() => v.assertValidRelation({ ...r, target_id: "person:ada" }), /cannot relate to itself/);
  assert.throws(() => v.assertValidRelation({ ...r, confidence: "sure" }), /confidence/);
  assert.throws(() => v.assertValidRelation({ predicate: "uses" }), /source_id and target_id/);
  assert.equal(v.strongerConfidence("low", "high"), "high");
  assert.equal(v.strongerConfidence("medium", "medium"), "medium");
  assert.equal(v.strongerConfidence("high", undefined), "high");
});

test("alias keys ignore case and spacing; lists merge without duplicates and keep first spelling", () => {
  assert.equal(a.aliasKey("  The   Cabin "), "the cabin");
  assert.deepEqual(a.mergeLists(["Ada", "the owner"], ["ada", " Boss ", "", "THE OWNER"]), ["Ada", "the owner", "Boss"]);
  assert.deepEqual(a.mergeLists(["Ada", "Ada Example"], [], ["ada example"]), ["Ada"]);
  assert.deepEqual(a.mergeLists(undefined, undefined), []);
});

test("matchKind and rankMatches: exact id, name, alias beat substrings; type filter and exactOnly apply", () => {
  const es = [
    { id: "place:cabin", type: "place", name: "Hill Cabin", aliases: ["the cabin"], description: "Weekend house" },
    { id: "concept:cabin-fever", type: "concept", name: "Cabin fever", aliases: [], description: "" },
    { id: "person:ada", type: "person", name: "Ada", aliases: ["cabin owner"], description: "likes the weekend" },
  ];
  assert.equal(a.matchKind(es[0], "PLACE:CABIN"), "exact-id");
  assert.equal(a.matchKind(es[0], "hill cabin"), "name");
  assert.equal(a.matchKind(es[0], "The Cabin"), "alias");
  assert.equal(a.matchKind(es[1], "cabin"), "name-substring");
  assert.equal(a.matchKind(es[2], "cabin"), "alias-substring");
  assert.equal(a.matchKind(es[2], "weekend"), "description");
  assert.equal(a.matchKind(es[2], "zzz"), null);
  assert.equal(a.matchKind(es[2], "  "), null);
  assert.deepEqual(a.rankMatches(es, "the cabin").map((e) => [e.id, e.match]), [["place:cabin", "alias"]]);
  assert.deepEqual(a.rankMatches(es, "cabin").map((e) => e.id), ["place:cabin", "concept:cabin-fever", "person:ada"]);
  assert.deepEqual(a.rankMatches(es, "cabin", { type: "person" }).map((e) => e.id), ["person:ada"]);
  assert.deepEqual(a.rankMatches(es, "cabin", { exactOnly: true }), []);
  assert.equal(a.rankMatches(es, "cabin", { limit: 2 }).length, 2);
});

test("alias conflicts: same type sharing a name or alias, each group once, different types ignored", () => {
  const es = [
    { id: "concept:kg", type: "concept", name: "Knowledge graphs", aliases: ["KG", "graphs"] },
    { id: "concept:knowledge-graph", type: "concept", name: "Knowledge Graph", aliases: ["kg", "Graphs"] },
    { id: "project:kg", type: "project", name: "KG", aliases: [] },
    { id: "concept:x", type: "concept", name: "X", aliases: ["y"] },
  ];
  assert.deepEqual(a.findAliasConflicts(es), [{ key: "kg", type: "concept", ids: ["concept:kg", "concept:knowledge-graph"] }]);
  assert.deepEqual(a.findAliasConflicts([]), []);
});

test("db path: explicit, then POTTER_KG_DB, then the state dir; never a guessed home; relative paths refused", () => {
  assert.equal(resolveDbPath({ explicit: "/data/kg.sqlite", env: { POTTER_KG_DB: "/other" }, stateDir: "/state" }), "/data/kg.sqlite");
  assert.equal(resolveDbPath({ env: { POTTER_KG_DB: "/other/kg.sqlite" }, stateDir: "/state" }), "/other/kg.sqlite");
  assert.equal(resolveDbPath({ env: {}, stateDir: "/state" }), "/state/potter-kg/potter-kg.sqlite");
  assert.equal(resolveDbPath({ env: { OPENCLAW_STATE_DIR: "/oc" } }), "/oc/potter-kg/potter-kg.sqlite");
  assert.equal(resolveDbPath({ env: {} }), null);
  assert.equal(resolveDbPath(), null);
  assert.equal(resolveDbPath({ explicit: ":memory:" }), ":memory:");
  assert.throws(() => resolveDbPath({ explicit: "kg.sqlite" }), /absolute/);
  assert.throws(() => resolveDbPath({ env: { OPENCLAW_STATE_DIR: "state" } }), /absolute/);
});
