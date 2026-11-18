import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  choose, createDecider, createDecisionClient, createTelemetry, decide, decisionRecord, DEFAULT_ENDPOINT, fileSink,
  isHealthy, loadPolicy, modelSetting, UNHEALTHY_MS, validatePolicy,
} from "./index.js";

const EXAMPLE = fileURLToPath(new URL("./policy.example.json", import.meta.url));
const POLICY = {
  version: 1,
  defaultClass: "chat",
  classes: {
    chat: { model: "prov/main", fallbacks: ["prov/second", "prov/third"], maxCostUsd: 0.5, timeoutMs: 60000 },
    decision: { model: "dec/primary", fallbacks: ["dec/backup"], maxCostUsd: 0.001, timeoutMs: 5000 },
    solo: { model: "prov/only", fallbacks: [], maxCostUsd: 0.01, timeoutMs: 1000 },
  },
};
const tmp = (t) => { const d = mkdtempSync(join(tmpdir(), "jev-")); t.after(() => rmSync(d, { recursive: true, force: true })); return d; };

// ---- policy ------------------------------------------------------------------------------------

test("the example policy is well formed but refused until its placeholders are replaced", () => {
  const example = JSON.parse(readFileSync(EXAMPLE, "utf8"));
  assert.deepEqual(validatePolicy(example, { allowPlaceholders: true }), []);
  const errors = validatePolicy(example);
  assert.ok(errors.length >= Object.keys(example.classes).length);
  assert.ok(errors.every((e) => /is a placeholder/.test(e)));
  assert.throws(() => loadPolicy(EXAMPLE), /not usable[\s\S]*placeholder/);
  for (const c of Object.values(example.classes)) {
    for (const id of [c.model, ...c.fallbacks]) assert.match(id, /^REPLACE_/, "the example names no real model");
  }
  assert.ok("kg_curation" in example.classes && "decision" in example.classes);
});

test("validatePolicy names every problem", () => {
  assert.deepEqual(validatePolicy(POLICY), []);
  assert.deepEqual(validatePolicy(null), ["policy must be a JSON object"]);
  assert.deepEqual(validatePolicy({ version: 2 }), ["version must be 1", "classes must be a non-empty object"]);
  const bad = {
    version: 1, defaultClass: "nope",
    classes: {
      "Bad-Name": { model: "a", fallbacks: [], maxCostUsd: 1, timeoutMs: 1 },
      a: { model: "", fallbacks: "x", maxCostUsd: 0, timeoutMs: 1.5 },
      b: { model: "m", fallbacks: ["m"], maxCostUsd: 1, timeoutMs: 10 },
      c: [],
    },
  };
  const errors = validatePolicy(bad).join("\n");
  for (const want of ["classes.Bad-Name: class names", "classes.a.model", "classes.a.fallbacks", "classes.a.maxCostUsd", "classes.a.timeoutMs",
    "classes.b: model and fallbacks must all differ", "classes.c must be an object", 'defaultClass "nope"']) {
    assert.ok(errors.includes(want), want);
  }
});

test("loadPolicy: reads a good file; a missing or malformed file is a clear error", (t) => {
  const dir = tmp(t);
  const file = join(dir, "policy.json");
  writeFileSync(file, JSON.stringify(POLICY));
  assert.deepEqual(loadPolicy(file), POLICY);
  assert.throws(() => loadPolicy(join(dir, "none.json")), /file not found/);
  writeFileSync(file, "{ nope");
  assert.throws(() => loadPolicy(file), /not valid JSON/);
});

test("choose: the primary when healthy, with the class's ceiling and timeout", () => {
  assert.deepEqual(choose("chat", POLICY, {}), { taskClass: "chat", maxCostUsd: 0.5, timeoutMs: 60000, model: "prov/main", reason: "primary", fallbackIndex: 0 });
  assert.equal(choose({ class: "decision" }, POLICY).model, "dec/primary");
  assert.equal(choose("chat", POLICY, { "prov/main": { ok: true } }).model, "prov/main");
});

test("choose: falls back in order past unhealthy models and says so; nothing healthy means no model", () => {
  const down = { ok: false };
  assert.deepEqual(choose("chat", POLICY, { "prov/main": down }), { taskClass: "chat", maxCostUsd: 0.5, timeoutMs: 60000, model: "prov/second", reason: "fallback:primary-unhealthy", fallbackIndex: 1 });
  assert.equal(choose("chat", POLICY, { "prov/main": down, "prov/second": down }).fallbackIndex, 2);
  assert.equal(choose("chat", POLICY, { "prov/second": down }).model, "prov/main", "an unhealthy fallback does not matter while the primary is fine");
  const none = choose("solo", POLICY, { "prov/only": down });
  assert.deepEqual([none.model, none.reason, none.fallbackIndex], [null, "all-unhealthy", null]);
});

test("choose: an unhealthy mark with `until` expires only when the caller supplies the time", () => {
  const health = { "prov/main": { ok: false, until: 1000 } };
  assert.equal(choose("chat", POLICY, health).model, "prov/second");
  assert.equal(choose("chat", POLICY, health, { now: 999 }).model, "prov/second");
  assert.equal(choose("chat", POLICY, health, { now: 1000 }).model, "prov/main");
  assert.equal(isHealthy("x", undefined), true);
  assert.equal(isHealthy("x", { x: { ok: false } }, 5), false);
});

test("choose: unknown classes use defaultClass and say so; with no defaultClass they get no model", () => {
  const d = choose("poetry", POLICY);
  assert.deepEqual([d.taskClass, d.model, d.reason], ["chat", "prov/main", "default-class:poetry;primary"]);
  assert.equal(choose(undefined, POLICY).reason, "default-class:;primary");
  const { defaultClass, ...strict } = POLICY;
  assert.deepEqual(choose("poetry", strict), { taskClass: "poetry", model: null, reason: "unknown-task-class", fallbackIndex: null, maxCostUsd: null, timeoutMs: null });
  assert.equal(choose("toString", strict).model, null, "inherited object keys are not classes");
});

test("choose: an estimate above the class ceiling is refused before any call; at the ceiling it runs", () => {
  const over = choose({ class: "decision", estimatedCostUsd: 0.002 }, POLICY);
  assert.deepEqual([over.model, over.reason, over.maxCostUsd], [null, "over-cost-ceiling", 0.001]);
  assert.equal(choose({ class: "decision", estimatedCostUsd: 0.001 }, POLICY).model, "dec/primary");
});

test("choose is pure: same answer twice, inputs untouched", () => {
  const health = { "prov/main": { ok: false } };
  const snapshot = JSON.stringify([POLICY, health]);
  assert.deepEqual(choose("chat", POLICY, health), choose("chat", POLICY, health));
  assert.equal(JSON.stringify([POLICY, health]), snapshot);
});

test("modelSetting gives the OpenClaw-shaped primary/fallbacks for a class", () => {
  assert.deepEqual(modelSetting(POLICY, "chat"), { primary: "prov/main", fallbacks: ["prov/second", "prov/third"], timeoutMs: 60000 });
  assert.throws(() => modelSetting(POLICY, "nope"), /no class/);
});

// ---- client ------------------------------------------------------------------------------------

const reply = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) });
const ANSWERS = { route: { type: "choice", choice: "a", confidence: 0.8 } };

test("client: posts model, state and questions to the decision endpoint with the key as a bearer header", async () => {
  let got;
  const client = createDecisionClient({
    apiKey: "test-key-123",
    fetchImpl: async (url, init) => { got = { url, init }; return reply({ answers: ANSWERS, model: "dec/primary-2026", usage: { cost: 0.00003, input_tokens: 100, output_tokens: 20 } }); },
    now: (() => { let t = 1000; return () => (t += 40); })(),
  });
  const out = await client.ask({ model: "dec/primary", state: { a: 1 }, questions: { route: { type: "choice", criteria: { a: "A", b: "B" } } } });
  assert.deepEqual(out, { ok: true, answers: ANSWERS, model: "dec/primary-2026", costUsd: 0.00003, inputTokens: 100, outputTokens: 20, latencyMs: 40 });
  assert.equal(got.url, DEFAULT_ENDPOINT);
  assert.equal(got.url, "https://openrouter.ai/api/alpha/decisions");
  assert.equal(got.init.method, "POST");
  assert.equal(got.init.headers.Authorization, "Bearer test-key-123");
  assert.deepEqual(JSON.parse(got.init.body), { model: "dec/primary", state: { a: 1 }, questions: { route: { type: "choice", criteria: { a: "A", b: "B" } } } });
  assert.ok(got.init.signal instanceof AbortSignal);
});

test("client: endpoint override; missing usage becomes null; the asked model is reported when none comes back", async () => {
  let url;
  const client = createDecisionClient({ apiKey: "k", endpoint: "http://decisions.test/v1", fetchImpl: async (u) => { url = u; return reply({ answers: {} }); } });
  const out = await client.ask({ model: "m", state: null, questions: {} });
  assert.equal(url, "http://decisions.test/v1");
  assert.deepEqual([out.ok, out.model, out.costUsd, out.inputTokens], [true, "m", null, null]);
});

test("client: every failure is a result, never a throw, and carries no key, no state and no response body", async () => {
  const ask = (deps, extra = {}) => createDecisionClient({ apiKey: "sekret-key", ...deps }).ask({ model: "m", state: { private: "diary" }, questions: {}, ...extra });
  const cases = [
    [await createDecisionClient({ fetchImpl: async () => reply({}) }).ask({ model: "m" }), { ok: false, kind: "no-key", latencyMs: 0 }],
    [await ask({}), { ok: false, kind: "no-fetch", latencyMs: 0 }],
    [await ask({ fetchImpl: async () => reply({ error: "sekret-key diary leaked" }, 500) }), { ok: false, kind: "http", status: 500 }],
    [await ask({ fetchImpl: async () => reply({ error: "rate" }, 429) }), { ok: false, kind: "http", status: 429 }],
    [await ask({ fetchImpl: async () => { throw new Error("getaddrinfo diary"); } }), { ok: false, kind: "network" }],
    [await ask({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError("bad"); } }) }), { ok: false, kind: "parse" }],
    [await ask({ fetchImpl: async () => reply({ nothing: true }) }), { ok: false, kind: "parse" }],
    [await ask({ fetchImpl: async () => reply(null) }), { ok: false, kind: "parse" }],
  ];
  for (const [out, want] of cases) {
    const { latencyMs, ...rest } = out;
    assert.deepEqual(rest, Object.fromEntries(Object.entries(want).filter(([k]) => k !== "latencyMs")));
    assert.equal(typeof latencyMs, "number");
    assert.doesNotMatch(JSON.stringify(out), /sekret|diary/);
  }
});

test("client: a call that outlives its timeout is aborted and reported as a timeout", async () => {
  const hang = (_url, init) => new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  });
  const out = await createDecisionClient({ apiKey: "k", fetchImpl: hang }).ask({ model: "m", state: 1, questions: {}, timeoutMs: 20 });
  assert.equal(out.kind, "timeout");
});

// ---- telemetry ---------------------------------------------------------------------------------

test("telemetry: one JSON line per decision with a fixed set of fields; anything else is dropped", () => {
  const lines = [];
  const tel = createTelemetry((l) => lines.push(l));
  tel.record({ ts: "2026-01-01T00:00:00.000Z", taskClass: "chat", model: "prov/main", reason: "primary", latencyMs: 12, ok: true, attempts: 1,
    costUsd: 0.01, state: { secret: "diary" }, questions: { q: 1 }, answers: { a: 1 }, error: "Bearer abc" });
  assert.equal(lines.length, 1);
  assert.ok(lines[0].endsWith("\n") && !lines[0].slice(0, -1).includes("\n"));
  assert.deepEqual(JSON.parse(lines[0]), {
    ts: "2026-01-01T00:00:00.000Z", taskClass: "chat", model: "prov/main", reason: "primary", latencyMs: 12, ok: true, attempts: 1,
    failure: null, costUsd: 0.01, overCeiling: false,
  });
  const r = decisionRecord({ taskClass: 7, model: { x: 1 }, latencyMs: "fast", ok: "yes", reason: "x".repeat(500) });
  assert.deepEqual([r.taskClass, r.model, r.latencyMs, r.ok, r.reason.length], [null, null, null, false, 200]);
  assert.match(r.ts, /^\d{4}-\d\d-\d\dT/);
});

test("telemetry: a failing sink never breaks the caller; the file sink appends and creates its directory", (t) => {
  assert.equal(createTelemetry(() => { throw new Error("disk full"); }).record({ taskClass: "chat" }).taskClass, "chat");
  const file = join(tmp(t), "logs", "jev.jsonl");
  const tel = createTelemetry(fileSink(file));
  tel.record({ taskClass: "a", ok: true });
  tel.record({ taskClass: "b", ok: false });
  const rows = readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.deepEqual(rows.map((r) => r.taskClass), ["a", "b"]);
});

// ---- decide ------------------------------------------------------------------------------------

/** A client whose answers are scripted per model id; records the calls it received. */
function fakeClient(byModel) {
  const calls = [];
  return { calls, ask: async (req) => { calls.push(req); return byModel[req.model] ?? { ok: false, kind: "http", status: 500 }; } };
}
function recorder() {
  const rows = [];
  return { rows, telemetry: createTelemetry((l) => rows.push(JSON.parse(l))) };
}
const clock = (start = 5000, step = 10) => { let t = start; return () => (t += step); };
const INPUT = { state: { text: "private words" }, questions: { route: { type: "choice", criteria: { a: "A", b: "B" } } } };

test("decide: asks the policy's model with the policy's timeout and logs one content-free line", async () => {
  const client = fakeClient({ "dec/primary": { ok: true, answers: ANSWERS, model: "dec/primary-1", costUsd: 0.0001 } });
  const { rows, telemetry } = recorder();
  const out = await decide("decision", INPUT, { policy: POLICY, client, telemetry, now: clock() });
  assert.deepEqual([out.ok, out.answers, out.model, out.reason, out.overCeiling], [true, ANSWERS, "dec/primary-1", "primary", false]);
  assert.deepEqual(client.calls, [{ model: "dec/primary", state: INPUT.state, questions: INPUT.questions, timeoutMs: 5000 }]);
  assert.equal(rows.length, 1);
  assert.deepEqual(Object.keys(rows[0]), ["ts", "taskClass", "model", "reason", "latencyMs", "ok", "attempts", "failure", "costUsd", "overCeiling"]);
  assert.deepEqual([rows[0].taskClass, rows[0].model, rows[0].reason, rows[0].ok, rows[0].attempts], ["decision", "dec/primary", "primary", true, 1]);
  assert.ok(rows[0].latencyMs > 0);
  assert.doesNotMatch(JSON.stringify(rows), /private words|criteria/);
});

test("decide: a failing primary is marked unhealthy and the fallback answers; the next decision skips the primary", async () => {
  const client = fakeClient({ "dec/backup": { ok: true, answers: ANSWERS, model: "dec/backup", costUsd: null } });
  const { rows, telemetry } = recorder();
  const health = {};
  const now = clock();
  const out = await decide("decision", INPUT, { policy: POLICY, client, telemetry, health, now });
  assert.deepEqual([out.ok, out.reason], [true, "fallback:primary-unhealthy"]);
  assert.deepEqual(client.calls.map((c) => c.model), ["dec/primary", "dec/backup"]);
  assert.equal(health["dec/primary"].ok, false);
  assert.ok(health["dec/primary"].until > 5000 && health["dec/primary"].until <= 5000 + 100 + UNHEALTHY_MS);
  assert.deepEqual([rows[0].model, rows[0].reason, rows[0].attempts, rows[0].ok], ["dec/backup", "fallback:primary-unhealthy", 2, true]);
  await decide("decision", INPUT, { policy: POLICY, client, telemetry, health, now });
  assert.deepEqual(client.calls.map((c) => c.model), ["dec/primary", "dec/backup", "dec/backup"]);
  assert.equal(rows.length, 2, "one line per decision, however many attempts it took");
});

test("decide: when every model fails it says so, logs the last failure kind, and does not loop", async () => {
  const client = fakeClient({ "dec/primary": { ok: false, kind: "timeout" } });
  const { rows, telemetry } = recorder();
  const out = await decide("decision", INPUT, { policy: POLICY, client, telemetry, now: clock() });
  assert.deepEqual([out.ok, out.reason, out.model], [false, "http:500", null]);
  assert.equal(client.calls.length, 2);
  assert.deepEqual([rows[0].ok, rows[0].model, rows[0].reason, rows[0].failure, rows[0].attempts], [false, null, "all-unhealthy", "http:500", 2]);
});

test("decide: no key is not a model problem: no fallback is tried and no model is marked unhealthy", async () => {
  const client = fakeClient({ "dec/primary": { ok: false, kind: "no-key" } });
  const health = {};
  const out = await decide("decision", INPUT, { policy: POLICY, client, health });
  assert.deepEqual([out.ok, out.reason], [false, "no-key"]);
  assert.equal(client.calls.length, 1);
  assert.deepEqual(health, {});
});

test("decide: an unknown class (no default) or an over-ceiling estimate makes no call at all", async () => {
  const client = fakeClient({});
  const { rows, telemetry } = recorder();
  const { defaultClass, ...strict } = POLICY;
  assert.deepEqual(await decide("poetry", INPUT, { policy: strict, client, telemetry, now: () => 1 }), { ok: false, reason: "unknown-task-class", model: null, latencyMs: 0 });
  assert.equal((await decide({ class: "decision", estimatedCostUsd: 1 }, INPUT, { policy: POLICY, client, telemetry })).reason, "over-cost-ceiling");
  assert.equal(client.calls.length, 0);
  assert.deepEqual(rows.map((r) => [r.reason, r.attempts, r.ok]), [["unknown-task-class", 0, false], ["over-cost-ceiling", 0, false]]);
});

test("decide: a call that cost more than the class ceiling is flagged in the result and the log", async () => {
  const client = fakeClient({ "dec/primary": { ok: true, answers: ANSWERS, model: "dec/primary", costUsd: 0.5 } });
  const { rows, telemetry } = recorder();
  const out = await decide("decision", INPUT, { policy: POLICY, client, telemetry });
  assert.deepEqual([out.ok, out.overCeiling, rows[0].overCeiling, rows[0].costUsd], [true, true, true, 0.5]);
});

// ---- createDecider (what the curator loads) ----------------------------------------------------

test("createDecider: no key or no policy means null (nothing to decide with), never an error", (t) => {
  const file = join(tmp(t), "policy.json");
  writeFileSync(file, JSON.stringify(POLICY));
  assert.equal(createDecider({ env: {}, policyPath: file }), null);
  assert.equal(createDecider({ env: { OPENROUTER_API_KEY: "k" } }), null);
  assert.equal(createDecider({ env: { OPENROUTER_API_KEY: "", POTTER_MODEL_POLICY: file } }), null);
});

test("createDecider: with a key and a policy it decides within the policy and writes telemetry; a bad policy throws", async (t) => {
  const dir = tmp(t);
  const file = join(dir, "policy.json");
  const log = join(dir, "jev.jsonl");
  writeFileSync(file, JSON.stringify({ ...POLICY, classes: { ...POLICY.classes, kg_curation: { model: "dec/kg", fallbacks: [], maxCostUsd: 0.001, timeoutMs: 20000 } } }));
  const seen = [];
  const fetchImpl = async (url, init) => { seen.push({ url, body: JSON.parse(init.body), auth: init.headers.Authorization }); return reply({ answers: { decision: { choice: "keep", confidence: 0.9 } } }); };
  const run = createDecider({ env: { OPENROUTER_API_KEY: "env-key", POTTER_MODEL_POLICY: file, POTTER_JEV_LOG: log, POTTER_DECISIONS_URL: "http://decisions.test/d" }, taskClass: "kg_curation", fetchImpl });
  const out = await run({ purpose: "potter-kg.curate.orphan", state: { entity: "x" }, questions: { decision: { type: "choice", criteria: { keep: "k", archive: "a" } } } });
  assert.deepEqual([out.ok, out.answers.decision.choice], [true, "keep"]);
  assert.deepEqual([seen[0].url, seen[0].body.model, seen[0].auth], ["http://decisions.test/d", "dec/kg", "Bearer env-key"]);
  const row = JSON.parse(readFileSync(log, "utf8").trim());
  assert.deepEqual([row.taskClass, row.model, row.reason, row.ok], ["kg_curation", "dec/kg", "primary", true]);
  assert.doesNotMatch(readFileSync(log, "utf8"), /env-key/);
  assert.throws(() => createDecider({ env: { OPENROUTER_API_KEY: "k" }, policyPath: EXAMPLE }), /placeholder/);
});

// ---------------------------------------------------------------- Merrick's Jev decider (v3.2)
import { INTENTS as MERRICK_INTENTS, questions as merrickQuestions, toRoute } from "./merrick-decide.mjs";
import { readFileSync as _rf } from "node:fs";

test("Merrick's Jev decider knows exactly the router's intents", () => {
  const py = _rf(new URL("../merrick/router.py", import.meta.url), "utf8");
  const listed = py.match(/INTENTS = \(([^)]*)\)/s)[1].match(/"([a-z_]+)"/g).map((s) => s.slice(1, -1));
  assert.deepEqual(Object.keys(MERRICK_INTENTS).sort(), listed.sort());
});

test("Merrick's decider turns Jev's answer into a route, or nothing", () => {
  assert.deepEqual(toRoute({ intent: { choice: "house_status", confidence: 0.92 }, zone: { choice: "none" } }),
    { intent: "house_status", confidence: 0.92, zone: null });
  assert.deepEqual(toRoute({ intent: { choice: "check_temperature", probabilities: { check_temperature: 0.8 } }, zone: { choice: "den" } }),
    { intent: "check_temperature", confidence: 0.8, zone: "den" });
  assert.equal(toRoute({ intent: { choice: "make_coffee", confidence: 1 } }), null);
  assert.equal(toRoute({}), null);
  const q = merrickQuestions(["family_room", "den"]);
  assert.deepEqual(Object.keys(q.zone.criteria), ["family_room", "den", "none"]);
});

test("house ideas: only supported, clearly safe and new ideas survive", async () => {
  const { filter, keep } = await import("./house-ideas.mjs");
  assert.equal(keep({ supported: { noul: 0.9 }, safe: { noul: 0.95 }, is_new: { noul: 0.9 } }), null);
  assert.equal(keep({ supported: { noul: 0.9 }, safe: { noul: 0.8 }, is_new: { noul: 0.9 } }), "not clearly safe");
  assert.equal(keep({ supported: { noul: 0.4 }, safe: { noul: 0.99 }, is_new: { noul: 0.9 } }), "not supported by the data yet");
  const ideas = [{ title: "Later preheat", what: "start 2.5 h before arrival" }, { title: "Unlock door on arrival", what: "unlock" }];
  const decide = async ({ state }) => ({ ok: true, answers: { supported: { noul: 0.9 }, is_new: { noul: 0.9 }, safe: { noul: /unlock/i.test(state.idea.what) ? 0.1 : 0.97 } } });
  const r = await filter({ ideas, evidence: { ready: [] }, existing: [] }, decide);
  assert.deepEqual(r.kept.map((i) => i.title), ["Later preheat"]);
  assert.deepEqual(r.dropped, [{ title: "Unlock door on arrival", why: "not clearly safe" }]);
});
