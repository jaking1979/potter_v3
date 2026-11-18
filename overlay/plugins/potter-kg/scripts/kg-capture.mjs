#!/usr/bin/env node
// kg-capture: turn the owner's queued messages into graph facts (see lib/capture.js), as a command job.
//
//   node kg-capture.mjs --queue <jsonl> --db <graph> --staged <jsonl> --decider <module> --policy <file>
//                       [--owner <entity id>] [--max-usd 0.5] [--report]
//   node kg-capture.mjs --staged <jsonl> --db <graph> --list                 what waits for the owner
//   node kg-capture.mjs --staged <jsonl> --db <graph> --approve <id,...>     write them (owner approved)
//   node kg-capture.mjs --staged <jsonl> --db <graph> --reject <id,...>      never write them
//
// The queue is written by potter-core (one line per owner message). Processed lines are removed.
// Facts Jev is unsure of go to --staged for the weekly review. Silence is a result: prints only with --report.
// Exit codes: 0 done; 2 bad arguments; 3 no decider or no key.
import { appendFileSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { applyEntry, captureMessage, EXTRACTION_SCHEMA } from "../lib/capture.js";
import { findOwner } from "../lib/eval.js";

function quietSqliteNotice() {
  const emit = process.emitWarning;
  process.emitWarning = function (w, ...rest) {
    if (String(w?.message ?? w).startsWith("SQLite is an experimental feature")) return;
    return emit.call(this, w, ...rest);
  };
}

const readLines = (p) => (existsSync(p) ? readFileSync(p, "utf8").split("\n").filter(Boolean) : []);
function writeAtomic(p, text) { const t = `${p}.tmp`; writeFileSync(t, text, { mode: 0o600 }); renameSync(t, p); }

/** The extraction call: a cheap chat model with a JSON schema. */
export function createExtractor({ apiKey, model, fetchImpl = globalThis.fetch, timeoutMs = 60000 }) {
  return async (messages) => {
    if (!apiKey) return { ok: false, reason: "no-key" };
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const res = await fetchImpl("https://openrouter.ai/api/v1/chat/completions", {
        method: "POST", signal: ctl.signal,
        headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
        body: JSON.stringify({ model, messages, temperature: 0, response_format: { type: "json_schema", json_schema: EXTRACTION_SCHEMA }, usage: { include: true } }),
      });
      if (!res.ok) return { ok: false, reason: `http:${res.status}` };
      const data = await res.json();
      const text = data?.choices?.[0]?.message?.content ?? "";
      let json;
      try { json = JSON.parse(String(text).replace(/^```(json)?|```$/g, "").trim()); } catch { return { ok: false, reason: "parse" }; }
      return { ok: true, json, costUsd: typeof data?.usage?.cost === "number" ? data.usage.cost : null };
    } catch (e) {
      return { ok: false, reason: ctl.signal.aborted ? "timeout" : "network" };
    } finally { clearTimeout(timer); }
  };
}

export function modelFor(policyPath, cls = "worker") {
  const p = JSON.parse(readFileSync(policyPath, "utf8"));
  return String(p.classes?.[cls]?.model ?? "").replace(/^openrouter\//, "");
}

/** Process the queue. Returns a run record. */
export async function run({ store, queuePath, stagedPath, decide, extract, ownerId, maxUsd = 0.5, now = () => new Date() }) {
  const lines = readLines(queuePath);
  const ents = store.db.prepare("SELECT id, type, name, status FROM entities").all();
  const rels = store.db.prepare("SELECT source_id, target_id, status FROM relations").all();
  const owner = (ownerId && store.getEntity(ownerId)) || findOwner(ents, rels);
  const rec = { ts: now().toISOString(), messages: 0, remembered: 0, written: 0, staged: 0, dropped: 0, errors: 0, cost: 0, stoppedAtCostCap: false };
  let done = 0;
  for (const line of lines) {
    if (rec.cost >= maxUsd) { rec.stoppedAtCostCap = true; break; }
    done++;
    let item;
    try { item = JSON.parse(line); } catch { continue; }
    if (!item?.text) continue;
    rec.messages++;
    let r;
    try {
      r = await captureMessage(item, { store, decide, extract, owner: owner ? { id: owner.id, name: owner.name } : null, date: String(item.ts ?? rec.ts).slice(0, 10) });
    } catch (e) { rec.errors++; continue; }
    rec.cost += r.cost;
    if (r.error) rec.errors++;
    if (r.screen?.proceed) rec.remembered++;
    for (const w of r.written) {
      try { applyEntry(store, w, { source: `chat ${String(item.ts ?? "").slice(0, 10)}`, confidence: w.confidence }); rec.written++; } catch { rec.errors++; }
    }
    for (const s of r.staged) {
      const id = `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
      appendFileSync(stagedPath, JSON.stringify({ id, status: "pending", staged: rec.ts, why: s.why, text: readable(s), entry: s }) + "\n", { mode: 0o600 });
      rec.staged++;
    }
    rec.dropped += r.dropped.length;
  }
  // Remove what was processed; lines appended meanwhile stay.
  if (done) writeAtomic(queuePath, readLines(queuePath).slice(done).map((l) => l + "\n").join(""));
  rec.cost = Math.round(rec.cost * 100000) / 100000;
  return rec;
}

export function readable(entry) {
  return `${entry.fact.subject.name} ${entry.predicate.replace(/_/g, " ")} ${entry.fact.object.name} ("${entry.quote}")`;
}

export function decideStaged(store, stagedPath, ids, approve) {
  const want = new Set(ids);
  const out = [];
  const rows = readLines(stagedPath).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  for (const r of rows) {
    if (!want.has(r.id) || r.status !== "pending") continue;
    if (approve) {
      try { applyEntry(store, r.entry, { source: "chat (owner approved)", confidence: "high" }); r.status = "approved"; }
      catch (e) { r.status = "failed"; r.error = e.message; }
    } else r.status = "rejected";
    r.decided = new Date().toISOString();
    out.push({ id: r.id, status: r.status });
  }
  writeAtomic(stagedPath, rows.map((r) => JSON.stringify(r) + "\n").join(""));
  return out;
}

export async function main(argv, env = process.env, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }) {
  let a;
  try {
    a = parseArgs({ args: argv, options: {
      queue: { type: "string" }, db: { type: "string" }, staged: { type: "string" }, decider: { type: "string" }, policy: { type: "string" },
      owner: { type: "string" }, "max-usd": { type: "string", default: "0.5" }, report: { type: "boolean", default: false },
      list: { type: "boolean", default: false }, approve: { type: "string" }, reject: { type: "string" }, "extract-class": { type: "string", default: "worker" },
    } }).values;
  } catch (e) { io.err(`kg-capture: ${e.message}\n`); return 2; }
  if (!a.db || !a.staged) { io.err("kg-capture: --db and --staged are required\n"); return 2; }
  quietSqliteNotice();
  const { openStore } = await import("../lib/store.js");
  const store = openStore(resolve(a.db));
  try {
    if (a.list) {
      const rows = readLines(a.staged).map((l) => JSON.parse(l)).filter((r) => r.status === "pending");
      io.out(rows.length ? rows.map((r) => `${r.id}  ${r.text}  [${r.why}]`).join("\n") + "\n" : "Nothing waiting.\n");
      return 0;
    }
    if (a.approve || a.reject) {
      const res = decideStaged(store, a.staged, String(a.approve ?? a.reject).split(",").map((x) => x.trim()).filter(Boolean), Boolean(a.approve));
      io.out(res.map((r) => `${r.id}: ${r.status}`).join("\n") + (res.length ? "\n" : "No matching pending items.\n"));
      return 0;
    }
    if (!a.queue || !a.decider || !a.policy) { io.err("kg-capture: --queue, --decider and --policy are required\n"); return 2; }
    if (!existsSync(a.queue) || !readLines(a.queue).length) return 0;
    const mod = await import(pathToFileURL(resolve(a.decider)).href);
    const decide = mod.createDecider({ env, policyPath: a.policy, taskClass: "kg_curation" });
    if (!decide || !env.OPENROUTER_API_KEY) { io.err("kg-capture: no decider or key\n"); return 3; }
    const extract = createExtractor({ apiKey: env.OPENROUTER_API_KEY, model: modelFor(a.policy, a["extract-class"]) });
    const rec = await run({ store, queuePath: resolve(a.queue), stagedPath: resolve(a.staged), decide, extract, ownerId: a.owner, maxUsd: +a["max-usd"] });
    appendFileSync(join(dirname(resolve(a.db)), "kg-capture-runs.jsonl"), JSON.stringify(rec) + "\n", { mode: 0o600 });
    if (a.report) io.out(JSON.stringify(rec) + "\n");
    return 0;
  } finally { store.close(); }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).then((c) => { process.exitCode = c; });
}
