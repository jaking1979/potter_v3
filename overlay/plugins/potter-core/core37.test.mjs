// The knowledge graph read on every request: match, candidates, Jev relevance, render.
import assert from "node:assert/strict";
import { mkdtempSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import { candidates, loadGraph, match, recall, relevanceRequest, render, select, stale } from "./lib/kgrecall.js";

function graphFile() {
  const path = join(mkdtempSync(join(tmpdir(), "kgr-")), "g.sqlite");
  const db = new DatabaseSync(path);
  db.exec(`CREATE TABLE entities (id TEXT PRIMARY KEY, type TEXT, name TEXT, description TEXT DEFAULT '', aliases TEXT DEFAULT '[]',
    confidence TEXT DEFAULT 'medium', status TEXT DEFAULT 'active', provenance TEXT DEFAULT '[]', created TEXT, updated TEXT);
    CREATE TABLE relations (id INTEGER PRIMARY KEY, source_id TEXT, predicate TEXT, target_id TEXT, confidence TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'active', provenance TEXT DEFAULT '[]', created TEXT, updated TEXT);`);
  const E = db.prepare("INSERT INTO entities (id, type, name, description, aliases, confidence) VALUES (?, ?, ?, ?, ?, ?)");
  E.run("person:ada", "person", "Ada Example", "", '["Ada"]', "high");
  E.run("person:mia", "person", "Mia Example", "Daughter; races on the ski team", '["Mia"]', "high");
  E.run("place:cabin", "place", "Hill Cabin", "Weekend house", '["the cabin", "upstate place"]', "high");
  E.run("interest:skiing", "interest", "Skiing", "", "[]", "medium");
  E.run("resource:cake", "resource", "Lemon Cake", "A saved recipe", "[]", "medium");
  E.run("concept:old", "concept", "Old Idea", "", "[]", "low");
  db.prepare("UPDATE entities SET status='archived' WHERE id='concept:old'").run();
  const R = db.prepare("INSERT INTO relations (source_id, predicate, target_id, confidence, provenance, updated) VALUES (?, ?, ?, ?, ?, ?)");
  R.run("person:ada", "parent_of", "person:mia", "high", '["profile"]', "2026-01-01");
  R.run("person:ada", "lives_at", "place:cabin", "high", "[]", "2026-01-01");
  R.run("person:ada", "interested_in", "interest:skiing", "medium", "[]", "2026-01-01");
  R.run("person:mia", "interested_in", "interest:skiing", "high", "[]", "2026-01-01");
  R.run("person:mia", "interested_in", "resource:cake", "medium", "[]", "2026-01-01");
  R.run("person:ada", "interested_in", "concept:old", "low", "[]", "2026-01-01");     // endpoint archived: never shown
  db.close();
  return path;
}

test("names and aliases match, longest first; archived things never; 'I' means the owner", () => {
  const g = loadGraph(graphFile());
  assert.equal(g.owner, "person:ada");
  assert.equal(loadGraph(graphFile(), { owner: "person:mia" }).owner, "person:mia");          // config wins
  assert.deepEqual(match(g, "How is Mia doing at the cabin?").map((m) => m.id), ["place:cabin", "person:mia"]);
  assert.deepEqual(match(g, "is mia's race on saturday").map((m) => m.id), ["person:mia"]);
  assert.deepEqual(match(g, "what should I cook").map((m) => m.id), ["person:ada"]);
  assert.deepEqual(match(g, "what is the weather").map((m) => m.id), []);
  assert.ok(!match(g, "Old Idea").length);
});

test("candidates: description plus direct facts, strongest first, readable", () => {
  const g = loadGraph(graphFile());
  const f = candidates(g, match(g, "Mia"));
  assert.equal(f[0].kind, "about");
  assert.match(f[0].text, /Mia Example \(person\): Daughter/);
  assert.ok(f.some((x) => x.text === "Ada Example is a parent of Mia Example."));
  assert.ok(f.findIndex((x) => x.confidence === "medium") > f.findIndex((x) => x.text.includes("Skiing")));
  assert.ok(!f.some((x) => x.text.includes("Old Idea")));
});

test("one narrow yes/no per fact; Jev decides what stays; without Jev only high-confidence facts", () => {
  const g = loadGraph(graphFile());
  const facts = candidates(g, match(g, "Mia"));
  const req = relevanceRequest("When is Mia's ski race?", facts);
  assert.equal(Object.keys(req.questions).length, facts.length);
  assert.match(req.questions.f0.instructions.question, /`facts\[0\]`/);
  assert.equal(req.state.facts.length, facts.length);
  const answers = Object.fromEntries(facts.map((x) => [`f${x.i}`, { noul: /Skiing|Daughter/.test(x.text) ? 0.9 : 0.1 }]));
  const s = select(facts, answers);
  assert.equal(s.source, "jev");
  assert.ok(s.kept.every((x) => /Skiing|Daughter/.test(x.text)));
  const fb = select(facts, null);
  assert.equal(fb.source, "fallback");
  assert.ok(fb.kept.every((x) => x.confidence === "high"));
});

test("render labels unconfirmed facts and keeps inside the budget", () => {
  const out = render([{ text: "A uses B.", confidence: "high" }, { text: "C uses D.", confidence: "medium" }]);
  assert.match(out, /^## What your knowledge graph says\n- A uses B\.\n- \(unconfirmed\) C uses D\./);
  assert.equal(render([]), "");
  assert.ok(render([{ text: "x".repeat(400), confidence: "high" }], { budget: 100 }) === "");
});

test("recall end to end: Jev answers, Jev times out, no graph, nothing named", async () => {
  const g = loadGraph(graphFile());
  const yes = async ({ questions }) => ({ ok: true, answers: Object.fromEntries(Object.keys(questions).map((k) => [k, { noul: 0.8 }])) });
  const r = await recall({ graph: g, text: "How is Mia?", decide: yes });
  assert.equal(r.source, "jev");
  assert.ok(r.context.includes("Mia Example"));
  const slow = () => new Promise(() => {});
  const t = await recall({ graph: g, text: "How is Mia?", decide: slow, timeoutMs: 20 });
  assert.equal(t.source, "fallback");
  assert.equal(t.error, "timeout");
  assert.equal((await recall({ graph: null, text: "Mia" })).source, "none");
  assert.equal((await recall({ graph: g, text: "what is the weather" })).context, "");
});

test("a changed graph file is noticed", () => {
  const path = graphFile();
  const g = loadGraph(path);
  assert.ok(!stale(g));
  utimesSync(path, new Date(), new Date(Date.now() + 5000));
  assert.ok(stale(g));
});

test("file passages are judged in the same request: irrelevant ones are dropped; without Jev all are kept", async () => {
  const g = loadGraph(graphFile());
  const passages = [{ path: "learnings/ERRORS.md", head: "Log lines", text: "How to read a log line." },
    { path: "preferences/ski.md", head: "Race days", text: "Race days start at 7." }];
  const judge = async ({ state, questions }) => ({ ok: true, answers: Object.fromEntries(Object.keys(questions).map((k) => {
    const i = Number(k.slice(1));
    return [k, { noul: /Race days|Skiing/.test(state.facts[i]) ? 0.9 : 0.1 }];
  })) });
  const r = await recall({ graph: g, text: "When is Mia's race?", decide: judge, passages });
  assert.deepEqual(r.passagesKept.map((h) => h.path), ["preferences/ski.md"]);
  assert.equal(r.passagesConsidered, 2);
  const noGraph = await recall({ graph: null, text: "When is the race?", decide: judge, passages });
  assert.deepEqual(noGraph.passagesKept.map((h) => h.path), ["preferences/ski.md"]);   // works without a graph
  const noJev = await recall({ graph: g, text: "When is Mia's race?", passages });
  assert.equal(noJev.passagesKept.length, 2);
});

test("only the owner's own messages are queued for the graph; jobs, strangers and chit-chat never", async () => {
  const { shouldQueue, enqueueMessage } = await import("./lib/capturequeue.js");
  const owner = ["12345"];
  const msg = "Mia is moving to the U14 team in January.";
  assert.ok(shouldQueue(msg, { senderId: "12345" }, owner));
  assert.ok(shouldQueue(msg, { senderId: "telegram:12345" }, owner));
  assert.ok(!shouldQueue(msg, { senderId: "999" }, owner));
  assert.ok(!shouldQueue(msg, {}, owner));                                        // unknown sender
  assert.ok(!shouldQueue(msg, { senderId: "12345", jobId: "j1" }, owner));
  assert.ok(!shouldQueue(msg, { senderId: "12345", trigger: "heartbeat" }, owner));
  assert.ok(!shouldQueue("thanks!", { senderId: "12345" }, owner));
  assert.ok(!shouldQueue("Run the morning-brief skill now.", { senderId: "12345" }, owner));
  const path = join(mkdtempSync(join(tmpdir(), "q-")), "q.jsonl");
  assert.ok(enqueueMessage(path, msg, { sessionKey: "s1" }));
  const { readFileSync, statSync } = await import("node:fs");
  assert.equal(JSON.parse(readFileSync(path, "utf8")).text, msg);
  assert.equal(statSync(path).mode & 0o777, 0o600);
});

test("expired facts are never recalled; old temporary ones are flagged; facts carry a key for usage", async () => {
  const path = graphFile();
  const db = new DatabaseSync(path);
  db.prepare("UPDATE relations SET provenance=? WHERE predicate='interested_in' AND target_id='interest:skiing' AND source_id='person:mia'")
    .run(JSON.stringify(['chat 2026-01-01: "on the ski team until March" until:2026-03-01 kind:situational']));
  db.prepare("UPDATE relations SET provenance=? WHERE predicate='interested_in' AND target_id='resource:cake'")
    .run(JSON.stringify(['chat 2026-01-01: "loves that cake" kind:situational']));
  db.close();
  const g = loadGraph(path);
  const f = candidates(g, match(g, "Mia"), { today: "2026-11-18" });
  assert.ok(!f.some((x) => x.text.includes("Skiing")));
  assert.ok(f.some((x) => x.text.includes("Lemon Cake") && x.text.includes("(as of 2026-01-01; may have changed)")));
  assert.ok(f.filter((x) => x.kind === "relation").every((x) => /\|/.test(x.key)));
});

test("vault tools are gated by potter-vault itself, so potter-core does not ask twice", async () => {
  const { classify } = await import("./lib/classify.js");
  for (const t of ["vault_list", "vault_authorize", "vault_send_to_owner"]) assert.equal(classify(t, {}).risk, "read", t);
});
