// The curator as a program: what an OpenClaw command job actually runs.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { openStore } from "../lib/store.js";
import { main } from "../scripts/kg-curate.mjs";

const SCRIPT = fileURLToPath(new URL("../scripts/kg-curate.mjs", import.meta.url));
const OLD = () => new Date("2026-01-01T00:00:00Z");

function workdir(t) {
  const dir = mkdtempSync(join(tmpdir(), "kg-curate-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const db = join(dir, "kg.sqlite");
  const store = openStore(db, { now: OLD });
  store.upsertEntity({ id: "interest:baking", name: "Baking" });
  store.upsertEntity({ id: "person:ada", name: "Ada" });
  store.upsertEntity({ id: "place:cabin", name: "Cabin" });
  store.addRelation({ source_id: "person:ada", predicate: "lives_at", target_id: "place:cabin" });
  store.close();
  return { dir, db };
}

function capture() {
  const io = { stdout: "", stderr: "", out: (s) => { io.stdout += s; }, err: (s) => { io.stderr += s; } };
  return io;
}

/** A decider module on disk, as --decider expects: answers every question with `choice`. */
function deciderModule(dir, body) {
  const file = join(dir, "decider.mjs");
  writeFileSync(file, body);
  return file;
}

test("as a child process with no key and no decider: exit 0, prints nothing, leaves a run record", (t) => {
  const { dir, db } = workdir(t);
  const r = spawnSync(process.execPath, [SCRIPT, "--db", db, "--apply"], { encoding: "utf8", env: { PATH: process.env.PATH } });
  assert.deepEqual([r.status, r.stdout, r.stderr], [0, "", ""]);
  const run = JSON.parse(readFileSync(join(dir, "kg-curator-last-run.json"), "utf8"));
  assert.equal(run.mode, "deterministic");
  assert.deepEqual(run.found, { orphans: 1, aliasConflicts: 0, weakRelations: 1 });
  assert.equal(run.ok, true);
});

test("a decider that reports 'nothing to decide with' (returns null) is the same quiet deterministic run", async (t) => {
  const { dir, db } = workdir(t);
  const decider = deciderModule(dir, "export const createDecider = ({ env }) => (env.SOME_KEY ? () => ({ ok: false }) : null);\n");
  const io = capture();
  assert.equal(await main(["--db", db, "--apply", "--decider", decider], {}, io), 0);
  assert.deepEqual([io.stdout, io.stderr], ["", ""]);
  assert.equal(JSON.parse(readFileSync(join(dir, "kg-curator-last-run.json"), "utf8")).mode, "deterministic");
});

test("the graph path can come from OPENCLAW_STATE_DIR; with no path at all it refuses (exit 2)", async (t) => {
  const { dir } = workdir(t);
  const io = capture();
  assert.equal(await main([], { OPENCLAW_STATE_DIR: dir }, io), 0);
  assert.ok(existsSync(join(dir, "potter-kg", "kg-curator-last-run.json")));
  const none = capture();
  assert.equal(await main([], {}, none), 2);
  assert.match(none.stderr, /no graph file/);
  assert.equal(await main(["--db", "relative.sqlite"], {}, capture()), 2);
  assert.equal(await main(["--no-such-flag"], {}, capture()), 2);
  assert.equal(await main(["--db", join(dir, "x.sqlite"), "--max-calls", "many"], {}, capture()), 2);
});

test("with a decider and --apply it acts, hands the decider the policy path and task class, and --report prints the record", async (t) => {
  const { dir, db } = workdir(t);
  const seen = join(dir, "seen.json");
  const decider = deciderModule(dir, `
    import { writeFileSync } from "node:fs";
    export function createDecider(opts) {
      writeFileSync(${JSON.stringify(seen)}, JSON.stringify({ policyPath: opts.policyPath, taskClass: opts.taskClass, telemetryPath: opts.telemetryPath }));
      return async ({ purpose }) => ({ ok: true, answers: { decision: { choice: purpose.endsWith("orphan") ? "archive" : "upgrade", confidence: 0.95 } } });
    }`);
  const io = capture();
  const runFile = join(dir, "run.json");
  const code = await main(["--db", db, "--apply", "--report", "--decider", decider, "--policy", "/etc/policy.json", "--telemetry", "/var/log/jev.jsonl", "--run-file", runFile], {}, io);
  assert.equal(code, 0);
  assert.deepEqual(JSON.parse(readFileSync(seen, "utf8")), { policyPath: "/etc/policy.json", taskClass: "kg_curation", telemetryPath: "/var/log/jev.jsonl" });
  const printed = JSON.parse(io.stdout);
  assert.deepEqual(printed, JSON.parse(readFileSync(runFile, "utf8")));
  assert.deepEqual(printed.actions.map((a) => a.action), ["archive-entity", "upgrade-relation"]);
  const store = openStore(db);
  t.after(() => store.close());
  assert.equal(store.getEntity("interest:baking").status, "archived");
  assert.equal(store.getRelation("person:ada", "lives_at", "place:cabin").confidence, "high");
});

test("without --apply a decider's answers are recorded but the graph is untouched", async (t) => {
  const { dir, db } = workdir(t);
  const decider = deciderModule(dir, "export const createDecider = () => async () => ({ ok: true, answers: { decision: { choice: 'archive', confidence: 1 } } });\n");
  assert.equal(await main(["--db", db, "--decider", decider, "--max-calls", "1"], {}, capture()), 0);
  const store = openStore(db);
  t.after(() => store.close());
  assert.equal(store.getEntity("interest:baking").status, "active");
});

test("a configured decider whose every call fails is exit 3 with one line on stderr; a broken decider module is exit 2", async (t) => {
  const { dir, db } = workdir(t);
  const failing = deciderModule(dir, "export const createDecider = () => async () => ({ ok: false });\n");
  const io = capture();
  assert.equal(await main(["--db", db, "--apply", "--decider", failing], {}, io), 3);
  assert.match(io.stderr, /^kg-curate: decision model gave no usable answer \(2 of 2 calls failed\)\n$/);
  assert.equal(io.stdout, "");
  const broken = capture();
  assert.equal(await main(["--db", db, "--decider", join(dir, "missing.mjs")], {}, broken), 2);
  assert.match(broken.stderr, /cannot load decider/);
  const throwing = deciderModule(dir, "export const createDecider = () => { throw new Error('model policy is not usable'); };\n");
  // Same file name as before: import a fresh copy by giving it a new path.
  const io2 = capture();
  writeFileSync(join(dir, "decider2.mjs"), readFileSync(throwing));
  assert.equal(await main(["--db", db, "--decider", join(dir, "decider2.mjs")], {}, io2), 2);
  assert.match(io2.stderr, /model policy is not usable/);
});
