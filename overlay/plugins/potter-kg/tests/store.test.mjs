// The graph engine. The first block is v2's 15-question evaluation suite, re-expressed on a made-up
// graph; the rest covers what v3 changed (merging upserts, live-only traversal, real merges).
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openStore } from "../lib/store.js";
import { freshStore, seeded, T0 } from "./fixture.mjs";

test("seed loads with the expected counts", () => {
  const s = seeded().stats();
  assert.equal(s.entities, 11);
  assert.equal(s.relations, 8);
});

test("resolve: by alias, by name, by id, any case; no match is an empty list", () => {
  const store = seeded();
  assert.deepEqual(store.resolve("ada").map((e) => [e.id, e.match]), [["person:ada", "alias"]]);
  assert.equal(store.resolve("Upstate Place")[0].id, "place:cabin");
  assert.equal(store.resolve("host-a.example")[0].id, "device:laptop");
  assert.equal(store.resolve("hill cabin")[0].match, "name");
  assert.equal(store.resolve("PERSON:BO")[0].match, "exact-id");
  assert.deepEqual(store.resolve("cab"), []);
  assert.deepEqual(store.resolve(""), []);
});

test("search: substrings of name, alias and description, with a type filter that always applies", () => {
  const store = seeded();
  assert.ok(store.search("overthink").some((e) => e.id === "concept:overthinking"));
  assert.equal(store.search("rumination")[0].match, "description");
  assert.deepEqual(store.search("example", { type: "device" }).map((e) => e.id), ["device:laptop"]);
  assert.deepEqual(store.search("ada", { type: "place" }), []);
  assert.equal(store.search("e", { limit: 3 }).length, 3);
  assert.equal(store.lookup("the cabin").id, "place:cabin");
  assert.equal(store.lookup("place:cabin").id, "place:cabin");
  assert.equal(store.lookup("nothing like this"), null);
});

test("neighbors: direct relations at 1 hop, wider at 2, each edge once, only edges inside the node set", () => {
  const store = seeded();
  const one = store.neighbors("person:ada", { maxHops: 1 });
  assert.deepEqual(one.nodes.map((n) => n.id).sort(), ["person:ada", "person:bo", "place:cabin"]);
  assert.equal(one.edges.length, 2);
  const two = store.neighbors("person:ada", { maxHops: 2 });
  assert.ok(two.nodes.some((n) => n.id === "system:home-hub"));
  assert.equal(two.edges.length, 3);
  assert.equal(new Set(two.edges.map((e) => `${e.source_id} ${e.predicate} ${e.target_id}`)).size, two.edges.length);
  const capped = store.neighbors("person:ada", { maxHops: 3, maxNodes: 2 });
  assert.equal(capped.nodes.length, 2);
  assert.equal(capped.stats.truncated, true);
  for (const e of capped.edges) assert.ok(capped.nodes.some((n) => n.id === e.source_id) && capped.nodes.some((n) => n.id === e.target_id));
  assert.deepEqual(store.neighbors("person:nobody"), { ok: false, error: "no entity person:nobody" });
});

test("path: direct, multi-hop with the relation at each step, not found within depth, same node, unknown node", () => {
  const store = seeded();
  const direct = store.path("device:laptop", "system:assistant");
  assert.deepEqual([direct.found, direct.depth, direct.steps[0].predicate], [true, 1, "candidate_host_for"]);
  const chain = store.path("device:laptop", "person:ada");
  assert.deepEqual(chain.path, ["device:laptop", "system:assistant", "system:home-hub", "place:cabin", "person:ada"]);
  assert.deepEqual(chain.steps.map((s) => s.predicate), ["candidate_host_for", "integrates_with", "located_in", "lives_at"]);
  assert.equal(chain.depth, 4);
  assert.equal(store.path("device:laptop", "person:ada", { maxDepth: 3 }).found, false);
  assert.equal(store.path("resource:sleep-course", "concept:stoicism").depth, 3);
  assert.equal(store.path("interest:baking", "person:ada").found, false);
  assert.deepEqual(store.path("person:ada", "person:ada"), { ok: true, found: true, depth: 0, path: ["person:ada"], steps: [] });
  assert.equal(store.path("person:ada", "person:nobody").ok, false);
});

test("source view shows provenance on the entity and on every relation", () => {
  const v = seeded().sourceView("person:ada");
  assert.deepEqual(v.entity.provenance, ["seed"]);
  assert.equal(v.relations.length, 2);
  for (const r of v.relations) assert.deepEqual(r.provenance, ["seed"]);
  assert.equal(seeded().sourceView("person:nobody").ok, false);
});

test("context is bounded and reports recent changes and totals from the injected clock", () => {
  const clock = { t: T0 };
  const store = seeded(clock);
  clock.t = T0 + 30 * 86400000;
  store.upsertEntity({ id: "interest:baking", name: "Baking", description: "Bread mostly" });
  const c = store.context("person:ada", { maxHops: 2, maxNodes: 4 });
  assert.equal(c.subgraph.nodes.length, 4);
  assert.deepEqual(c.recent.map((r) => r.ref), ["interest:baking"]);
  assert.deepEqual(c.stats, { totalEntities: 11, totalRelations: 8 });
  assert.equal(store.context("person:nobody").ok, false);
});

test("add is idempotent: re-adding the same entity and relation creates no duplicates", () => {
  const store = seeded();
  for (let i = 0; i < 2; i++) {
    store.upsertEntity({ id: "project:garden", name: "Garden" });
    store.addRelation({ source_id: "person:ada", predicate: "works_on", target_id: "project:garden" });
  }
  const s = store.stats();
  assert.deepEqual([s.entities, s.relations], [12, 9]);
});

test("archive is soft: gone from search, traversal and counts; the row stays; an upsert revives it", () => {
  const store = seeded();
  assert.deepEqual(store.archiveEntity("person:bo", "test"), { ok: true });
  assert.deepEqual(store.search("Bo Example"), []);
  assert.equal(store.getEntity("person:bo").status, "archived");
  assert.ok(store.getEntity("person:bo").provenance.includes("test"));
  assert.equal(store.neighbors("person:ada").nodes.some((n) => n.id === "person:bo"), false);
  assert.equal(store.stats().relations, 7);
  assert.equal(store.stats().archivedEntities, 1);
  assert.throws(() => store.addRelation({ source_id: "person:ada", predicate: "uses", target_id: "person:bo" }), /archived/);
  store.upsertEntity({ id: "person:bo", name: "Bo Example" });
  assert.equal(store.stats().relations, 8, "its relations are live again");
  assert.equal(store.archiveEntity("person:nobody").ok, false);
});

test("controlled vocabulary is enforced on write: bad type, bad predicate, missing endpoint", () => {
  const store = seeded();
  assert.throws(() => store.upsertEntity({ id: "wizard:x", name: "X" }), /entity.id must look like/);
  assert.throws(() => store.addRelation({ source_id: "person:ada", predicate: "adores", target_id: "person:bo" }), /predicate must be one of/);
  assert.throws(() => store.addRelation({ source_id: "person:ada", predicate: "uses", target_id: "device:none" }), /target entity not found/);
  assert.throws(() => store.addRelation({ source_id: "device:none", predicate: "uses", target_id: "person:ada" }), /source entity not found/);
  assert.equal(seeded().stats().relations, store.stats().relations);
});

test("upsert merges instead of wiping: aliases and provenance union, omitted fields keep their value", () => {
  const { store, clock } = freshStore();
  store.upsertEntity({ id: "place:cabin", name: "Cabin", description: "First", aliases: ["the cabin"], confidence: "high", provenance: ["a.md"] });
  clock.t += 1000;
  const e = store.upsertEntity({ id: "place:cabin", name: "Hill Cabin", aliases: ["THE CABIN", "hill cabin", "upstate"], provenance: ["b.md"] });
  assert.equal(e.name, "Hill Cabin");
  assert.equal(e.description, "First");
  assert.deepEqual(e.aliases, ["the cabin", "upstate"], "no duplicate by case, and the name itself is not an alias");
  assert.equal(e.confidence, "high");
  assert.deepEqual(e.provenance, ["a.md", "b.md"]);
  assert.ok(e.updated > e.created);
  assert.equal(store.upsertEntity({ id: "place:cabin", name: "Hill Cabin", description: "Second" }).description, "Second");
});

test("relations: provenance unions, confidence is kept unless given, re-adding revives an archived one", () => {
  const store = seeded();
  const key = ["person:ada", "lives_at", "place:cabin"];
  store.addRelation({ source_id: key[0], predicate: key[1], target_id: key[2], provenance: ["note.md"], confidence: "high" });
  store.addRelation({ source_id: key[0], predicate: key[1], target_id: key[2] });
  let r = store.getRelation(...key);
  assert.deepEqual([r.provenance, r.confidence], [["seed", "note.md"], "high"]);
  assert.deepEqual(store.archiveRelation(...key), { ok: true });
  assert.equal(store.relationsOf("place:cabin").some((x) => x.predicate === "lives_at"), false);
  store.addRelation({ source_id: key[0], predicate: key[1], target_id: key[2] });
  assert.equal(store.getRelation(...key).status, "active");
  assert.deepEqual(store.archiveRelation("person:ada", "uses", "place:cabin"), { ok: false });
  assert.equal(store.setRelationConfidence(r.id, "low", "why").ok, true);
  r = store.getRelation(...key);
  assert.deepEqual([r.confidence, r.provenance.at(-1)], ["low", "why"]);
  assert.equal(store.setRelationConfidence(99999, "low").ok, false);
});

test("health views: orphans, weak relations (low first), alias conflicts", () => {
  const store = seeded();
  assert.deepEqual(store.orphans().map((e) => e.id), ["interest:baking"]);
  assert.deepEqual(store.stats().orphans, ["interest:baking"]);
  const weak = store.weakRelations();
  assert.equal(weak.length, 7);
  assert.equal(weak[0].confidence, "low");
  assert.deepEqual(store.aliasConflicts(), []);
  store.upsertEntity({ id: "concept:sleep-therapy", name: "Sleep therapy" });
  assert.deepEqual(store.aliasConflicts().map((c) => c.ids), [["concept:cbti", "concept:sleep-therapy"]]);
});

test("merge re-points relations, carries names over as aliases, archives the duplicate, and loses nothing", () => {
  const store = seeded();
  store.upsertEntity({ id: "concept:sleep-therapy", name: "Sleep therapy", aliases: ["insomnia therapy"], provenance: ["dup.md"] });
  store.addRelation({ source_id: "concept:sleep-therapy", predicate: "connects_to", target_id: "concept:overthinking", confidence: "high", provenance: ["dup.md"] });
  store.addRelation({ source_id: "person:bo", predicate: "interested_in", target_id: "concept:sleep-therapy" });
  store.addRelation({ source_id: "concept:sleep-therapy", predicate: "connects_to", target_id: "concept:cbti" });
  const before = store.stats().relations;
  const out = store.mergeEntities("concept:cbti", "concept:sleep-therapy");
  assert.deepEqual(out, { ok: true, kept: "concept:cbti", archived: "concept:sleep-therapy", relationsMoved: 2 });
  const kept = store.getEntity("concept:cbti");
  assert.deepEqual(kept.aliases, ["sleep therapy", "insomnia therapy"]);
  assert.ok(kept.provenance.includes("dup.md"));
  const dup = store.getEntity("concept:sleep-therapy");
  assert.equal(dup.status, "archived");
  assert.ok(dup.provenance.includes("merged-into:concept:cbti"));
  const same = store.getRelation("concept:cbti", "connects_to", "concept:overthinking");
  assert.equal(same.confidence, "high", "the stronger confidence of the two wins");
  assert.ok(same.provenance.includes("dup.md") && same.provenance.includes("seed"));
  assert.equal(store.getRelation("person:bo", "interested_in", "concept:cbti").status, "active");
  assert.equal(store.getRelation("concept:cbti", "connects_to", "concept:cbti"), null, "no self-loop is created");
  assert.equal(store.stats().relations, before - 2, "one duplicate edge and one would-be self-loop are gone");
  assert.throws(() => store.mergeEntities("concept:cbti", "concept:cbti"), /itself/);
  assert.throws(() => store.mergeEntities("concept:cbti", "concept:sleep-therapy"), /missing or archived/);
});

test("a failed merge rolls back completely", () => {
  const store = seeded();
  store.upsertEntity({ id: "person:ada-2", name: "Ada Two" });
  store.addRelation({ source_id: "person:ada-2", predicate: "lives_at", target_id: "place:cabin" });
  const real = store.archiveEntity.bind(store);
  store.archiveEntity = () => { throw new Error("disk full"); };
  assert.throws(() => store.mergeEntities("person:ada", "person:ada-2"), /disk full/);
  store.archiveEntity = real;
  assert.equal(store.getRelation("person:ada-2", "lives_at", "place:cabin").status, "active");
  assert.deepEqual(store.getEntity("person:ada").aliases, ["Ada", "the owner"]);
});

test("a file store persists, creates its directory, reopens, and refuses an unknown schema version", () => {
  const dir = mkdtempSync(join(tmpdir(), "kg-store-"));
  try {
    const file = join(dir, "nested", "kg.sqlite");
    const a = openStore(file);
    a.upsertEntity({ id: "person:ada", name: "Ada" });
    a.close();
    a.close();
    const b = openStore(file);
    assert.equal(b.getEntity("person:ada").name, "Ada");
    assert.equal(b.getMeta("schema_version"), "1");
    b.setMeta("schema_version", "9");
    b.close();
    assert.throws(() => openStore(file), /schema version 9/);
    assert.throws(() => openStore(""), /path is required/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
