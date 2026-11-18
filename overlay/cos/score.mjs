#!/usr/bin/env node
// potter-cos-score: score the day's forecasts against the evening record and keep the owner's patterns in the graph.
//
//   node score.mjs --forecasts <dir> --db <graph> --decider <module> --policy <file> [--owner <id>] [--report]
//   node score.mjs --forecasts <dir> --week          the last 7 days in numbers (JSON), for the Sunday review
//
// Forecast files are state/forecasts/<YYYY-MM-DD>.json (written by the brief, completed by the evening check-in).
// Patterns are pattern:* entities linked to the owner with has_pattern; one that loses its evidence is archived.
// Exit codes: 0 done; 2 bad arguments.
import { existsSync, readdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { cleanForecast, outcomeRequest, patterns, readOutcomes, weekSummary } from "./lib.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const KG = join(HERE, "..", "plugins", "potter-kg", "lib");           // same layout in the repo and when installed

export function loadForecasts(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).filter((n) => /^\d{4}-\d{2}-\d{2}\.json$/.test(n)).sort().map((n) => {
    try { return { file: join(dir, n), raw: JSON.parse(readFileSync(join(dir, n), "utf8")) }; } catch { return null; }
  }).filter(Boolean).map((x) => ({ ...x, f: cleanForecast(x.raw) })).filter((x) => x.f);
}

function writeAtomic(p, obj) { const t = `${p}.tmp`; writeFileSync(t, JSON.stringify(obj, null, 1) + "\n", { mode: 0o600 }); renameSync(t, p); }

export async function scoreAll(entries, decide) {
  let scored = 0;
  for (const e of entries) {
    if (!e.f.evening || e.f.outcomes || !e.f.predictions.length || !decide) continue;
    const out = await decide(outcomeRequest(e.f));
    if (!out?.ok) continue;
    e.f.outcomes = readOutcomes(e.f, out.answers);
    writeAtomic(e.file, { ...e.raw, outcomes: e.f.outcomes, scored: new Date().toISOString() });
    scored++;
  }
  return scored;
}

export function syncPatterns(store, owner, found, { today, mergeLists }) {
  const keep = new Set();
  for (const p of found) {
    const id = `pattern:${p.key.replace(/[^a-z0-9._-]+/g, "-").slice(0, 80)}`;
    keep.add(id);
    store.upsertEntity({ id, name: p.name, description: `${p.description} Evidence: ${p.k} of ${p.n}.`, confidence: p.confidence,
      provenance: [`cos ${today}: ${p.k} of ${p.n}`] });
    store.addRelation({ source_id: owner.id, predicate: "has_pattern", target_id: id, confidence: p.confidence, provenance: [`cos ${today}`] });
  }
  let archived = 0;
  for (const r of store.relationsOf(owner.id)) {
    if (r.predicate !== "has_pattern" || keep.has(r.target_id)) continue;
    store.archiveRelation(r.source_id, r.predicate, r.target_id);
    store.archiveEntity(r.target_id, `cos ${today}: evidence no longer supports it`);
    archived++;
  }
  return { written: keep.size, archived };
}

export async function main(argv, env = process.env, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }) {
  let a;
  try {
    a = parseArgs({ args: argv, options: { forecasts: { type: "string" }, db: { type: "string" }, decider: { type: "string" }, policy: { type: "string" },
      owner: { type: "string" }, report: { type: "boolean", default: false }, week: { type: "boolean", default: false }, today: { type: "string" } } }).values;
  } catch (e) { io.err(`potter-cos-score: ${e.message}\n`); return 2; }
  if (!a.forecasts) { io.err("potter-cos-score: --forecasts is required\n"); return 2; }
  const today = a.today ?? new Date().toISOString().slice(0, 10);
  const entries = loadForecasts(resolve(a.forecasts));
  if (a.week) { io.out(JSON.stringify(weekSummary(entries.map((e) => e.f), { today })) + "\n"); return 0; }
  if (!a.db) { io.err("potter-cos-score: --db is required\n"); return 2; }
  let decide = null;
  if (a.decider && a.policy) {
    const mod = await import(pathToFileURL(resolve(a.decider)).href);
    decide = mod.createDecider({ env, policyPath: a.policy, taskClass: "decision" });
  }
  const scored = await scoreAll(entries, decide);
  const { openStore } = await import(pathToFileURL(join(KG, "store.js")).href);
  const { findOwner } = await import(pathToFileURL(join(KG, "eval.js")).href);
  const { mergeLists } = await import(pathToFileURL(join(KG, "alias.js")).href);
  const store = openStore(resolve(a.db));
  try {
    const owner = (a.owner && store.getEntity(a.owner)) ||
      findOwner(store.db.prepare("SELECT id, type, name, status FROM entities").all(), store.db.prepare("SELECT source_id, target_id, status FROM relations").all());
    if (!owner) { if (a.report) io.out(JSON.stringify({ scored, patterns: [], note: "no owner in the graph" }) + "\n"); return 0; }
    const found = patterns(entries.map((e) => e.f), { today });
    const res = syncPatterns(store, owner, found, { today, mergeLists });
    if (a.report) io.out(JSON.stringify({ scored, patterns: found.map((p) => `${p.name} (${p.confidence}, ${p.k}/${p.n})`), archived: res.archived }) + "\n");
    return 0;
  } finally { store.close(); }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).then((c) => { process.exitCode = c; });
}
