// The agent tools and kg_check_connection (ported from v2's jev.test.mjs: same rubric, same
// normalisation, but the decision call is injected instead of an HTTP request made by the plugin).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildConnectionQuestions, checkConnection, normalizeConnectionAnswers } from "../lib/connection.js";
import { buildTools, TOOL_NAMES } from "../lib/tools.js";
import { ENTITY_TYPES, PREDICATES } from "../lib/vocab.js";
import { seeded } from "./fixture.mjs";

const toolset = (deps) => Object.fromEntries(buildTools(deps).map((t) => [t.name, t]));
const call = async (tool, params) => (await tool.execute("call-1", params)).details;

test("every tool has a name, a description, a closed JSON Schema and an execute function", () => {
  const tools = buildTools({ store: seeded() });
  assert.deepEqual(tools.map((t) => t.name), TOOL_NAMES);
  for (const t of tools) {
    assert.ok(t.description.length > 40, t.name);
    assert.equal(t.parameters.type, "object");
    assert.equal(t.parameters.additionalProperties, false);
    for (const r of t.parameters.required) assert.ok(r in t.parameters.properties, `${t.name}.${r}`);
    assert.equal(typeof t.execute, "function");
  }
});

test("the manifest declares exactly the tools the plugin registers", () => {
  const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
  assert.deepEqual(manifest.contracts.tools, TOOL_NAMES);
});

test("results are text content plus structured details; errors come back as results, never as throws", async () => {
  const t = toolset({ store: seeded() });
  const res = await t.kg_search.execute("id", { query: "ada" });
  assert.equal(res.content[0].type, "text");
  assert.deepEqual(JSON.parse(res.content[0].text), res.details);
  const broken = toolset({ store: () => { throw new Error("cannot open graph file"); } });
  const bad = await broken.kg_stats.execute("id", {});
  assert.deepEqual(bad.details, { ok: false, error: "cannot open graph file" });
  assert.match(bad.content[0].text, /^Error: cannot open/);
});

test("the store is opened once, on first use, not when the tools are built", async () => {
  let opened = 0;
  const t = toolset({ store: () => { opened++; return seeded(); } });
  assert.equal(opened, 0);
  await t.kg_stats.execute("a", {});
  await t.kg_stats.execute("b", undefined);
  assert.equal(opened, 1);
});

test("kg_search, kg_neighbors, kg_path, kg_context, kg_source", async () => {
  const t = toolset({ store: seeded() });
  assert.equal((await call(t.kg_search, { query: "the cabin" }))[0].id, "place:cabin");
  assert.deepEqual(await call(t.kg_search, { query: "zzz" }), { message: 'No entities match "zzz".', entities: [] });
  assert.deepEqual(await call(t.kg_search, { query: "example", type: "device", limit: 5 }).then((r) => r.map((e) => e.id)), ["device:laptop"]);
  assert.equal((await call(t.kg_neighbors, { id: "person:ada" })).stats.nodes, 3);
  assert.equal((await call(t.kg_neighbors, { id: "person:ada", max_hops: 2 })).stats.nodes, 4);
  assert.equal((await call(t.kg_neighbors, { id: "person:nobody" })).ok, false);
  const p = await call(t.kg_path, { from: "device:laptop", to: "system:home-hub" });
  assert.deepEqual(p.path, ["device:laptop", "system:assistant", "system:home-hub"]);
  const none = await call(t.kg_path, { from: "interest:baking", to: "person:ada", max_depth: 2 });
  assert.deepEqual([none.found, none.message], [false, "No path from interest:baking to person:ada within depth 2."]);
  assert.equal((await call(t.kg_context, { id: "person:ada", max_nodes: 2 })).subgraph.nodes.length, 2);
  assert.equal((await call(t.kg_source, { id: "place:cabin" })).relations.length, 2);
});

test("kg_add: adds, defaults provenance to 'inference', reports each rejected item and keeps the rest", async () => {
  const store = seeded();
  const t = toolset({ store });
  const out = await call(t.kg_add, {
    entities: [
      { id: "project:garden", name: "Garden", aliases: ["veg patch"] },
      { id: "garden", name: "Bad id" },
      { id: "place:shed", type: "project", name: "Shed" },
    ],
    relations: [
      { source_id: "person:ada", predicate: "works_on", target_id: "project:garden", provenance: ["chat"] },
      { source_id: "person:ada", predicate: "owns", target_id: "project:garden" },
      { source_id: "person:ada", predicate: "uses", target_id: "device:none" },
    ],
  });
  assert.deepEqual(out.added, { entities: 1, relations: 1 });
  assert.equal(out.ok, false);
  assert.equal(out.errors.length, 4);
  assert.deepEqual(out.stats, { entities: 12, relations: 9 });
  assert.deepEqual(store.getEntity("project:garden").provenance, ["inference"]);
  assert.deepEqual(store.getRelation("person:ada", "works_on", "project:garden").provenance, ["chat"]);
  assert.deepEqual(await call(t.kg_add, {}), { ok: true, added: { entities: 0, relations: 0 }, errors: [], stats: { entities: 12, relations: 9 } });
});

test("kg_stats carries the controlled vocabulary", async () => {
  const s = await call(toolset({ store: seeded() }).kg_stats, {});
  assert.deepEqual([s.entities, s.relations, s.orphanCount], [11, 8, 1]);
  assert.deepEqual(s.entity_types, ENTITY_TYPES);
  assert.deepEqual(s.predicates, PREDICATES);
});

test("connection rubric: a yes/no 'related' plus a 29-way predicate choice; the yes/no type is selectable", () => {
  const q = buildConnectionQuestions();
  assert.equal(q.related.type, "boolean");
  assert.deepEqual(Object.keys(q.related.criteria), ["true", "false"]);
  assert.equal(q.predicate.type, "choice");
  assert.equal(Object.keys(q.predicate.criteria).length, 29);
  assert.ok(q.predicate.criteria.none);
  assert.equal(buildConnectionQuestions({ booleanType: "noul" }).related.type, "noul");
});

test("answers normalise from both spellings, and missing parts become null", () => {
  assert.deepEqual(
    normalizeConnectionAnswers({ related: { type: "boolean", probabilityTrue: 0.9 }, predicate: { type: "choice", choice: "lives_at", probabilities: { lives_at: 0.8, none: 0.2 } } }),
    { related: 0.9, predicate: "lives_at", predicateConfidence: 0.8, probabilities: { lives_at: 0.8, none: 0.2 } },
  );
  assert.deepEqual(
    normalizeConnectionAnswers({ related: { type: "noul", noul: 0.4 }, predicate: { choice: "none", confidence: 0.7 } }),
    { related: 0.4, predicate: "none", predicateConfidence: 0.7, probabilities: null },
  );
  assert.deepEqual(normalizeConnectionAnswers(), { related: null, predicate: null, predicateConfidence: null, probabilities: null });
});

test("kg_check_connection sends both entities with their neighbourhood, suggests, and writes nothing", async () => {
  const store = seeded();
  let sent;
  const decide = async (req) => {
    sent = req;
    return { ok: true, model: "decider/test", answers: { related: { probabilityTrue: 0.96 }, predicate: { choice: "works_on", probabilities: { works_on: 0.75 } } } };
  };
  const before = store.stats();
  const out = await call(toolset({ store, decide }).kg_check_connection, { a: "the owner", b: "system:home-hub" });
  assert.deepEqual(out, {
    a: { id: "person:ada", name: "Ada Example" }, b: { id: "system:home-hub", name: "Home Hub" },
    available: true, related: 0.96, predicate: "works_on", predicateConfidence: 0.75, probabilities: { works_on: 0.75 }, model: "decider/test",
  });
  assert.equal(sent.purpose, "potter-kg.check-connection");
  assert.deepEqual(Object.keys(sent.state), ["a", "b"]);
  assert.deepEqual(sent.state.a.related.map((r) => r.predicate).sort(), ["lives_at", "married_to"]);
  assert.ok(sent.state.b.related.some((r) => r.entity === "Assistant Gateway" && r.predicate === "integrates_with"));
  assert.deepEqual(Object.keys(sent.questions), ["related", "predicate"]);
  assert.deepEqual(store.stats(), before);
});

test("kg_check_connection degrades: unresolved names, same entity, no decider, unavailable or throwing decider", async () => {
  const store = seeded();
  assert.deepEqual(await checkConnection(store, "nobody at all", "person:ada"), { error: 'could not resolve "nobody at all"' });
  assert.deepEqual(await checkConnection(store, "person:ada", "zzz"), { error: 'could not resolve "zzz"' });
  assert.match((await checkConnection(store, "Ada", "person:ada", async () => ({ ok: true }))).error, /same entity/);
  const none = await call(toolset({ store }).kg_check_connection, { a: "person:ada", b: "person:bo" });
  assert.deepEqual([none.available, none.reason], [false, "no decision model is configured"]);
  const off = await checkConnection(store, "person:ada", "person:bo", async () => ({ ok: false, reason: "not-configured" }));
  assert.deepEqual([off.available, off.reason], [false, "not-configured"]);
  const boom = await checkConnection(store, "person:ada", "person:bo", async () => { throw new Error("deadline"); });
  assert.deepEqual([boom.available, boom.reason], [false, "deadline"]);
  // The direct neighbour is left out of each side's context so the model is not handed the answer.
  let sent;
  await checkConnection(store, "person:ada", "person:bo", async (req) => { sent = req; return { ok: false }; });
  assert.equal(sent.state.a.related.some((r) => r.entity === "Bo Example"), false);
});
