#!/usr/bin/env node
// kg-eval: ask a decision model about pairs from a real graph and measure how well it sees the links.
//
//   node kg-eval.mjs --db <file> --decider <module> --policy <file> [--pairs 200] [--negatives 60]
//                    [--owner <entity id>] [--arms v2,v3] [--max-usd 1] [--out <file>] [--seed 7]
//
// Opens the graph READ-ONLY. Writes nothing to it. The same sampled pairs go to every arm.
// Prints a short summary for the owner; the full per-pair results go to --out
// (default: kg-eval-<date>.json beside the graph file).
// Exit codes: 0 done; 2 bad arguments; 3 no decider (no key or policy); 4 every call failed.
import { writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { describe, findOwner, metrics, readV2, readV3, recallByPredicate, sample, v2Questions, v3Questions, v3State } from "../lib/eval.js";

function quietSqliteNotice() {
  const emit = process.emitWarning;
  process.emitWarning = function (warning, ...rest) {
    if (String(warning?.message ?? warning).startsWith("SQLite is an experimental feature")) return;
    return emit.call(this, warning, ...rest);
  };
}

/** Read the whole graph once, read-only. */
export async function loadGraph(path) {
  quietSqliteNotice();
  const { DatabaseSync } = await import("node:sqlite");
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    const entities = db.prepare("SELECT id, type, name, description, status FROM entities").all();
    const relations = db.prepare("SELECT source_id, predicate, target_id, status, confidence FROM relations").all();
    return { entities, relations };
  } finally {
    db.close();
  }
}

/** Runs the eval with an injected decide({state, questions}) -> {ok, answers, costUsd, latencyMs}. */
export async function runEval(graph, decide, { pairs = 200, negatives = 60, seed = 7, ownerId, arms = ["v2", "v3"], maxUsd = 1, concurrency = 4 } = {}) {
  const byId = new Map(graph.entities.map((e) => [e.id, e]));
  const adj = new Map();
  for (const r of graph.relations) {
    if (r.status !== "active") continue;
    for (const id of [r.source_id, r.target_id]) { if (!adj.has(id)) adj.set(id, []); adj.get(id).push(r); }
  }
  const rank = { high: 0, medium: 1, low: 2 };
  for (const list of adj.values()) list.sort((x, y) => (rank[x.confidence] ?? 1) - (rank[y.confidence] ?? 1));
  const neighbours = (id) => adj.get(id) ?? [];
  const nameOf = (id) => byId.get(id)?.name ?? id;
  const owner = ownerId ? byId.get(ownerId) ?? null : findOwner(graph.entities, graph.relations);
  const cases = sample({ ...graph, pairs, negatives, seed });

  let spent = 0, calls = 0, failures = 0, stopped = false;
  const jobs = [];
  for (const c of cases) for (const arm of arms) jobs.push({ c, arm });
  const out = cases.map((c) => ({ ...c }));
  const idx = new Map(cases.map((c, i) => [c, i]));
  let next = 0;
  async function worker() {
    for (;;) {
      if (spent >= maxUsd) { stopped = true; return; }
      const j = jobs[next++];
      if (!j) return;
      const A = byId.get(j.c.a), B = byId.get(j.c.b);
      const a = describe(A, { neighbours, nameOf, excludeId: B.id }), b = describe(B, { neighbours, nameOf, excludeId: A.id });
      const req = j.arm === "v2" ? { state: { a, b }, questions: v2Questions() } : { state: v3State({ a, b }, { owner }), questions: v3Questions() };
      let res;
      try { res = await decide(req); } catch (e) { res = { ok: false, reason: e?.message ?? String(e) }; }
      calls++;
      const row = out[idx.get(j.c)];
      if (!res?.ok) { failures++; row[j.arm] = { error: res?.reason ?? "failed" }; continue; }
      spent += typeof res.costUsd === "number" ? res.costUsd : 0;
      row[j.arm] = { ...(j.arm === "v2" ? readV2(res.answers) : readV3(res.answers)), costUsd: res.costUsd ?? null, latencyMs: res.latencyMs ?? null };
    }
  }
  await Promise.all(Array.from({ length: concurrency }, () => worker()));

  const summary = { owner: owner?.id ?? null, pairs: cases.length, calls, failures, spentUsd: Math.round(spent * 10000) / 10000, stoppedAtCostCap: stopped, arms: {} };
  for (const arm of arms) {
    const rows = out.filter((r) => r[arm] && !r[arm].error).map((r) => ({ ...r[arm], expectRelated: r.expectRelated, expectPredicate: r.expectPredicate }));
    const lat = rows.map((r) => r.latencyMs).filter((x) => typeof x === "number");
    summary.arms[arm] = {
      ...metrics(rows),
      ...(arm === "v3" ? { directOnly: metrics(rows, { field: "direct" }).at05, choiceTop1: metrics(rows, { ranked: "choiceRanked", top: "choice" }).predicateTop1 } : {}),
      avgLatencyMs: lat.length ? Math.round(lat.reduce((s, x) => s + x, 0) / lat.length) : null,
      weakest: recallByPredicate(rows).slice(0, 5),
    };
  }
  const missed = (arm) => out.filter((r) => r.expectRelated && r[arm] && !r[arm].error && r[arm].related < 0.5)
    .map((r) => ({ a: r.a, b: r.b, expect: r.expectPredicate, related: r[arm].related, got: r[arm].predicate })).slice(0, 25);
  const flagged = (arm) => out.filter((r) => !r.expectRelated && r[arm] && !r[arm].error && r[arm].related >= 0.5)
    .map((r) => ({ a: r.a, b: r.b, related: r[arm].related, got: r[arm].predicate }));
  return { summary, missed: Object.fromEntries(arms.map((a) => [a, missed(a)])), flaggedNegatives: Object.fromEntries(arms.map((a) => [a, flagged(a)])), results: out };
}

export function summaryText(s) {
  const pct = (v) => (v === null || v === undefined ? "-" : `${Math.round(v * 100)}%`);
  const lines = [`KG eval: ${s.pairs} pairs, ${s.calls} calls, ${s.failures} failed, $${s.spentUsd}${s.stoppedAtCostCap ? " (stopped at cost cap)" : ""}.`];
  for (const [arm, m] of Object.entries(s.arms)) {
    lines.push(`${arm}: recall ${pct(m.at05.recall)}, precision ${pct(m.at05.precision)}, F1 ${m.at05.f1}; ` +
      `predicate top-1 ${pct(m.predicateTop1)}, top-3 ${pct(m.predicateTop3)}; review band ${pct(m.reviewBand)}; ${m.avgLatencyMs ?? "-"} ms.`);
    if (m.weakest?.length) lines.push(`  weakest: ${m.weakest.map((w) => `${w.predicate} ${pct(w.recall)} (n=${w.n})`).join(", ")}`);
  }
  return lines.join("\n");
}

export async function main(argv, env = process.env, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }) {
  let a;
  try {
    a = parseArgs({ args: argv, options: {
      db: { type: "string" }, decider: { type: "string" }, policy: { type: "string" }, "task-class": { type: "string", default: "kg_curation" },
      pairs: { type: "string", default: "200" }, negatives: { type: "string", default: "60" }, owner: { type: "string" },
      arms: { type: "string", default: "v2,v3" }, "max-usd": { type: "string", default: "1" }, out: { type: "string" }, seed: { type: "string", default: "7" },
    } }).values;
  } catch (e) { io.err(`kg-eval: ${e.message}\n`); return 2; }
  if (!a.db) { io.err("kg-eval: --db is required\n"); return 2; }
  const arms = a.arms.split(",").map((x) => x.trim()).filter((x) => x === "v2" || x === "v3");
  if (!arms.length) { io.err("kg-eval: --arms takes v2, v3 or both\n"); return 2; }
  let decide = null;
  if (a.decider) {
    const mod = await import(pathToFileURL(resolve(a.decider)).href);
    decide = mod.createDecider({ env, policyPath: a.policy, taskClass: a["task-class"] });
  }
  if (!decide) { io.err("kg-eval: no decider (needs --decider, --policy and the model key in the environment)\n"); return 3; }
  const graph = await loadGraph(resolve(a.db));
  const res = await runEval(graph, decide, { pairs: +a.pairs, negatives: +a.negatives, seed: +a.seed, ownerId: a.owner, arms, maxUsd: +a["max-usd"] });
  const outPath = a.out ? resolve(a.out) : join(dirname(resolve(a.db)), `kg-eval-${new Date().toISOString().slice(0, 10)}.json`);
  writeFileSync(outPath, JSON.stringify(res, null, 2), { mode: 0o600 });
  io.out(summaryText(res.summary) + `\nFull results: ${outPath}\n`);
  return res.summary.calls && res.summary.failures === res.summary.calls ? 4 : 0;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).then((code) => { process.exitCode = code; });
}
