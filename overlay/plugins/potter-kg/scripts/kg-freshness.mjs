#!/usr/bin/env node
// kg-freshness: keep the graph current (see lib/freshness.js). Daily command job.
//
//   node kg-freshness.mjs --db <graph> --review <jsonl> [--usage <jsonl>] [--decider <module> --policy <file>] [--report]
//   node kg-freshness.mjs --db <graph> --review <jsonl> --list
//   node kg-freshness.mjs --db <graph> --review <jsonl> --keep <id,...>      still true: confirmed, high confidence
//   node kg-freshness.mjs --db <graph> --review <jsonl> --archive <id,...>   no longer true: archived (never deleted)
//
// Expired facts are archived automatically. "Still true?" questions go to --review for the weekly memory review.
// Without a decider, only the deterministic part runs. Exit codes: 0 done; 2 bad arguments.
import { appendFileSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { plan, readable, relKey, timeBoundRequest } from "../lib/freshness.js";
import { mergeLists } from "../lib/alias.js";

const lines = (p) => (p && existsSync(p) ? readFileSync(p, "utf8").split("\n").filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean) : []);
function writeAtomic(p, rows) { const t = `${p}.tmp`; writeFileSync(t, rows.map((r) => JSON.stringify(r) + "\n").join(""), { mode: 0o600 }); renameSync(t, p); }

export async function run({ store, reviewPath, usagePath, decide, today = new Date().toISOString().slice(0, 10), maxCalls = 40 }) {
  const relations = store.db.prepare(`SELECT r.* FROM relations r JOIN entities s ON s.id=r.source_id AND s.status='active'
    JOIN entities t ON t.id=r.target_id AND t.status='active' WHERE r.status='active'`).all()
    .map((r) => ({ ...r, provenance: JSON.parse(r.provenance || "[]") }));
  const usage = new Map();
  for (const u of lines(usagePath)) if (u.key && (!usage.has(u.key) || u.ts > usage.get(u.key))) usage.set(u.key, String(u.ts).slice(0, 10));
  const review = lines(reviewPath);
  const asked = new Map(review.map((q) => [q.key, String(q.asked).slice(0, 10)]));
  const judged = new Map(review.filter((q) => typeof q.timeBound === "number").map((q) => [q.key, q.timeBound]));
  const nameOf = (id) => store.getEntity(id)?.name ?? id;

  let p = plan({ relations, usage, asked, timeBound: judged, today });
  let calls = 0;
  if (decide && p.needJudgement.length) {
    for (const r of p.needJudgement.slice(0, maxCalls)) {
      const out = await decide(timeBoundRequest(readable(r, nameOf)));
      calls++;
      const v = out?.ok ? (out.answers?.time_bound?.noul ?? out.answers?.time_bound?.probabilityTrue) : undefined;
      if (typeof v === "number") judged.set(relKey(r), v);
    }
    p = plan({ relations, usage, asked, timeBound: judged, today });
  }
  for (const { r, why } of p.expire) {
    store.archiveRelation(r.source_id, r.predicate, r.target_id);
    store.db.prepare("UPDATE relations SET provenance=? WHERE id=?").run(JSON.stringify(mergeLists(r.provenance, [`expired ${today}: ${why}`])), r.id);
  }
  const rec = { ts: new Date().toISOString(), expired: p.expire.length, asked: 0, judged: calls };
  const rows = [...review];
  for (const [key, v] of judged) if (!rows.some((q) => q.key === key)) rows.push({ key, timeBound: v, status: "judged", asked: "1970-01-01" });
  for (const { r, why } of p.ask) {
    const id = `f${r.id}`;
    const i = rows.findIndex((q) => q.key === relKey(r));
    const q = { id, key: relKey(r), relId: r.id, text: readable(r, nameOf), why, status: "pending", asked: today, timeBound: judged.get(relKey(r)) };
    if (i >= 0) rows[i] = q; else rows.push(q);
    rec.asked++;
  }
  if (reviewPath) writeAtomic(reviewPath, rows);
  return rec;
}

export function answer(store, reviewPath, ids, keep, today = new Date().toISOString().slice(0, 10)) {
  const rows = lines(reviewPath);
  const want = new Set(ids);
  const out = [];
  for (const q of rows) {
    if (!want.has(q.id) || q.status !== "pending") continue;
    const r = store.getRelationById(q.relId);
    if (!r) { q.status = "gone"; out.push({ id: q.id, status: q.status }); continue; }
    if (keep) {
      store.db.prepare("UPDATE relations SET confidence='high', provenance=?, updated=? WHERE id=?")
        .run(JSON.stringify(mergeLists(r.provenance, [`confirmed:${today}`])), new Date().toISOString(), r.id);
      q.status = "kept";
    } else {
      store.archiveRelation(r.source_id, r.predicate, r.target_id);
      q.status = "archived";
    }
    q.decided = today;
    out.push({ id: q.id, status: q.status });
  }
  writeAtomic(reviewPath, rows);
  return out;
}

export async function main(argv, env = process.env, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }) {
  let a;
  try {
    a = parseArgs({ args: argv, options: { db: { type: "string" }, review: { type: "string" }, usage: { type: "string" }, decider: { type: "string" },
      policy: { type: "string" }, report: { type: "boolean", default: false }, list: { type: "boolean", default: false },
      keep: { type: "string" }, archive: { type: "string" } } }).values;
  } catch (e) { io.err(`kg-freshness: ${e.message}\n`); return 2; }
  if (!a.db || !a.review) { io.err("kg-freshness: --db and --review are required\n"); return 2; }
  const { openStore } = await import("../lib/store.js");
  const store = openStore(resolve(a.db));
  try {
    if (a.list) {
      const qs = lines(resolve(a.review)).filter((q) => q.status === "pending");
      io.out(qs.length ? qs.map((q) => `${q.id}  Still true? ${q.text}  (${q.why})`).join("\n") + "\n" : "Nothing waiting.\n");
      return 0;
    }
    if (a.keep || a.archive) {
      const res = answer(store, resolve(a.review), String(a.keep ?? a.archive).split(",").map((x) => x.trim()).filter(Boolean), Boolean(a.keep));
      io.out(res.length ? res.map((r) => `${r.id}: ${r.status}`).join("\n") + "\n" : "No matching pending questions.\n");
      return 0;
    }
    let decide = null;
    if (a.decider && a.policy) {
      const mod = await import(pathToFileURL(resolve(a.decider)).href);
      decide = mod.createDecider({ env, policyPath: a.policy, taskClass: "kg_curation" });
    }
    const rec = await run({ store, reviewPath: resolve(a.review), usagePath: a.usage ? resolve(a.usage) : null, decide });
    if (a.report) io.out(JSON.stringify(rec) + "\n");
    return 0;
  } finally { store.close(); }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).then((c) => { process.exitCode = c; });
}
