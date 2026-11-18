#!/usr/bin/env node
// potter-bench: PotterBench, a fixed benchmark in the style of public model benchmarks. A fictional household
// (bench/world), 30 tasks with answer keys (bench/tasks.jsonl), two sealed agents built from the same data:
//   v3   Potter's design: the governor, the knowledge graph (built by v3's own capture pipeline from the household's
//        messages), Jev; plus the flat files every OpenClaw agent has
//   v2   the flat-file design v2 used: the same files and daily notes, no Potter brain
// Both read only their own folder (potter-core seals them). Scored with exact checks plus Jev-graded criteria;
// every run is kept, so the leaderboard shows progress by version.
//
//   potter-bench setup | run [--only id,..] | grade | report [--json] | leaderboard
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { gradeRequest, readGrade } from "./lib.js";
import { askV3 } from "./t1.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
export const WORLD = join(HERE, "bench", "world");
export const TASKS = join(HERE, "bench", "tasks.jsonl");
export const NOW = "2027-03-10T13:00:00Z";                       // Wednesday March 10, 2027, 8:00 AM New York
export const WORLD_PREFIX = "Today is Wednesday, March 10, 2027, 8:00 AM (America/New_York). Your workspace has the household's " +
  "files: USER.md, MEMORY.md, preferences/, rules/, memory/ (daily notes) and data/ (inbox.jsonl, calendar.jsonl, tasks.jsonl, " +
  "house.jsonl). Use only these; you have no other tools or accounts in this run.\n\n";
export const SYSTEMS = [["v3", "bench-v3"], ["v2", "bench-v2"]];

const lines = (p) => (existsSync(p) ? readFileSync(p, "utf8").split("\n").filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean) : []);
const save = (p, rows) => { const t = `${p}.tmp`; writeFileSync(t, rows.map((r) => JSON.stringify(r) + "\n").join("")); renameSync(t, p); };

export function loadTasks(path = TASKS) {
  return lines(path).filter((t) => t.id && t.prompt && Array.isArray(t.criteria) && t.criteria.length);
}

/** The daily notes every OpenClaw agent would have kept: one file per day, the owner's words. */
export function dailyNotes(messages) {
  const by = {};
  for (const m of messages) (by[m.ts.slice(0, 10)] ??= []).push(`- ${m.ts.slice(11, 16)} The owner said: ${m.text}`);
  return Object.fromEntries(Object.entries(by).map(([d, l]) => [`memory/${d}.md`, `# ${d}\n\n${l.join("\n")}\n`]));
}

/** Build both workspaces from the world. machinery: v3's own generic files (AGENTS.md, rules/global, rules/INDEX.md). */
export function buildWorkspaces(root, { world = WORLD, machinery = null } = {}) {
  const notes = dailyNotes(lines(join(world, "data", "messages.jsonl")));
  for (const sys of ["v3", "v2"]) {
    const ws = join(root, sys);
    rmSync(ws, { recursive: true, force: true });
    cpSync(world, ws, { recursive: true });
    rmSync(join(ws, "README.md"), { force: true });
    rmSync(join(ws, "data", "messages.jsonl"), { force: true });          // known only as notes (and, for v3, the graph)
    for (const [rel, text] of Object.entries(notes)) { mkdirSync(dirname(join(ws, rel)), { recursive: true }); writeFileSync(join(ws, rel), text); }
    if (sys === "v3" && machinery) {
      for (const rel of ["AGENTS.md", "rules/INDEX.md", "rules/global"]) {
        if (existsSync(join(machinery, rel))) cpSync(join(machinery, rel), join(ws, rel), { recursive: true });
      }
    }
  }
  return { v3: join(root, "v3"), v2: join(root, "v2"), notes: Object.keys(notes).length };
}

/** The graph v3 starts from: the household's people (what v3 would have imported), before it hears the messages. */
export const SEED = {
  entities: [["person:sam-alder", "Sam Alder", ["Sam"]], ["person:jordan-alder", "Jordan Alder", ["Jordan"]], ["person:nora-alder", "Nora Alder", ["Nora"]],
    ["person:leo-alder", "Leo Alder", ["Leo"]], ["person:helen-park", "Helen Park", ["Helen"]], ["place:germantown-home", "Germantown home", ["home"]],
    ["place:lake-harmony-cabin", "Lake Harmony cabin", ["the cabin", "cabin"]]],
  relations: [["person:sam-alder", "married_to", "person:jordan-alder"], ["person:sam-alder", "parent_of", "person:nora-alder"],
    ["person:sam-alder", "parent_of", "person:leo-alder"], ["person:jordan-alder", "parent_of", "person:nora-alder"],
    ["person:jordan-alder", "parent_of", "person:leo-alder"], ["person:sam-alder", "lives_at", "place:germantown-home"]],
};

export function exactScore(task, answer) {
  const a = String(answer ?? "").toLowerCase();
  if (!(task.expect_all?.length || task.forbid?.length)) return null;
  const hits = (task.expect_all ?? []).every((s) => a.includes(String(s).toLowerCase()));
  const bad = (task.forbid ?? []).some((s) => a.includes(String(s).toLowerCase()));
  return hits && !bad ? 1 : 0;
}

export function finalScore(task, answer, jevAnswers) {
  const g = readGrade(task, jevAnswers);
  const ex = exactScore(task, answer);
  const score = ex === null ? g.score : Math.round(40 * ex + 0.6 * g.score);
  return { ...g, exact: ex, score, pass: score >= 70 };
}

export function summarize(tasks, rows) {
  const out = {};
  for (const [sys] of SYSTEMS) {
    const rs = rows.filter((r) => r.system === sys && r.grade);
    if (!rs.length) continue;
    const avg = (xs) => (xs.length ? Math.round(xs.reduce((s, r) => s + r.grade.score, 0) / xs.length) : null);
    const by = (key) => Object.fromEntries([...new Set(tasks.map((t) => t[key]))].map((k) => [k, avg(rs.filter((r) => tasks.find((t) => t.id === r.task)?.[key] === k))]));
    const lat = rs.map((r) => r.ms).filter((x) => typeof x === "number");
    out[sys] = { score: avg(rs), passed: rs.filter((r) => r.grade.pass).length, of: rs.length, byCategory: by("category"), byDifficulty: by("difficulty"),
      seconds: lat.length ? Math.round(lat.reduce((s, x) => s + x, 0) / lat.length / 100) / 10 : null };
  }
  return out;
}

export function renderRun(run, sum) {
  const l = [`PotterBench ${run.id} (code ${run.version ?? "?"}), ${run.tasks} tasks.`];
  for (const [sys, s] of Object.entries(sum)) l.push(`${sys}: ${s.score}/100, passed ${s.passed}/${s.of}, ${s.seconds ?? "-"} s avg. ` +
    `Easy ${s.byDifficulty.easy ?? "-"}, medium ${s.byDifficulty.medium ?? "-"}, hard ${s.byDifficulty.hard ?? "-"}.`);
  const cats = [...new Set(Object.values(sum).flatMap((s) => Object.keys(s.byCategory)))];
  for (const c of cats) l.push(`- ${c}: ${Object.entries(sum).map(([sys, s]) => `${sys} ${s.byCategory[c] ?? "-"}`).join(", ")}`);
  return l.join("\n");
}

export async function main(argv, env = process.env, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }, deps = {}) {
  const [cmd, ...rest] = argv;
  let a;
  try {
    a = parseArgs({ args: rest, options: { root: { type: "string" }, machinery: { type: "string" }, decider: { type: "string" }, policy: { type: "string" },
      openclaw: { type: "string", default: "openclaw" }, only: { type: "string" }, json: { type: "boolean", default: false }, version: { type: "string" },
      run: { type: "string" } } }).values;
  } catch (e) { io.err(`potter-bench: ${e.message}\n`); return 2; }
  if (!a.root) { io.err("potter-bench: --root is required\n"); return 2; }
  const root = resolve(a.root), results = join(root, "results");
  mkdirSync(results, { recursive: true });
  const tasks = loadTasks(deps.tasksPath);

  if (cmd === "setup") {
    const r = buildWorkspaces(root, { machinery: a.machinery });
    // v3's graph: the seed, then the household's messages through v3's real capture pipeline.
    writeFileSync(join(root, "seed.json"), JSON.stringify(SEED));
    writeFileSync(join(root, "messages-queue.jsonl"), lines(join(WORLD, "data", "messages.jsonl")).map((m) => JSON.stringify({ ts: m.ts, text: m.text }) + "\n").join(""));
    io.out(`Built ${r.v3} and ${r.v2} (${r.notes} days of notes). Next: the graph and the agents (the setup wrapper does both).\n`);
    return 0;
  }
  if (cmd === "seed") {
    // A fresh graph for v3 with the household's people; the capture pipeline then adds what the messages say.
    const kgLib = a.machinery;                                    // here: the potter-kg lib folder
    const { openStore } = await import(pathToFileURL(join(kgLib, "store.js")).href);
    const db = join(root, "bench.sqlite");
    rmSync(db, { force: true }); rmSync(`${db}-wal`, { force: true }); rmSync(`${db}-shm`, { force: true });
    const st = openStore(db);
    for (const [id, name, aliases] of SEED.entities) st.upsertEntity({ id, name, aliases, confidence: "high", provenance: ["bench seed"] });
    for (const [s0, p, o] of SEED.relations) st.addRelation({ source_id: s0, predicate: p, target_id: o, confidence: "high", provenance: ["bench seed"] });
    st.close();
    io.out(`Seeded ${db}.\n`);
    return 0;
  }
  if (cmd === "run") {
    const runId = a.run ?? `bench-${new Date().toISOString().slice(0, 16).replace(/[:T-]/g, "")}`;
    const path = join(results, `${runId}.jsonl`);
    const ask = deps.askV3 ?? askV3;
    const only = a.only ? new Set(a.only.split(",")) : null;
    const rows = lines(path);
    for (const t of tasks) {
      if (only && !only.has(t.id)) continue;
      for (const [sys, agent] of SYSTEMS) {
        if (rows.some((r) => r.task === t.id && r.system === sys && r.answer)) continue;          // resumable
        const r = await ask({ openclaw: a.openclaw, agent, runId, task: { ...t, prompt: WORLD_PREFIX + t.prompt } });
        rows.push({ run: runId, version: a.version ?? null, system: sys, task: t.id, answer: r.answer, ms: r.ms, error: r.error });
        save(path, rows);
      }
    }
    io.out(`Ran ${runId}. Next: potter-bench grade --run ${runId}\n`);
    return 0;
  }
  const runs = readdirSync(results).filter((f) => f.endsWith(".jsonl")).sort();
  const pick = a.run ? `${a.run}.jsonl` : runs[runs.length - 1];
  if (cmd === "grade") {
    if (!pick) { io.err("potter-bench: no run yet\n"); return 3; }
    let decide = deps.decide;
    if (!decide && a.decider && a.policy) {
      const mod = await import(pathToFileURL(resolve(a.decider)).href);
      decide = mod.createDecider({ env, policyPath: a.policy, taskClass: "decision" });
    }
    if (!decide) { io.err("potter-bench: no decider\n"); return 3; }
    const path = join(results, pick), rows = lines(path);
    let n = 0;
    for (const r of rows) {
      if (r.grade || !r.answer) continue;
      const t = tasks.find((x) => x.id === r.task);
      if (!t) continue;
      const out = await decide(gradeRequest(t, r.answer));
      if (out?.ok) { r.grade = finalScore(t, r.answer, out.answers); n++; }
    }
    save(path, rows);
    io.out(`Graded ${n} answers in ${pick.replace(".jsonl", "")}.\n`);
    return 0;
  }
  if (cmd === "report") {
    if (!pick) { io.err("potter-bench: no run yet\n"); return 3; }
    const rows = lines(join(results, pick));
    const sum = summarize(tasks, rows);
    const run = { id: pick.replace(".jsonl", ""), version: rows[0]?.version, tasks: new Set(rows.map((r) => r.task)).size };
    io.out((a.json ? JSON.stringify({ run, summary: sum, answers: rows }, null, 1) : renderRun(run, sum)) + "\n");
    return 0;
  }
  if (cmd === "leaderboard") {
    const table = runs.map((f) => {
      const rows = lines(join(results, f)), sum = summarize(tasks, rows);
      return `${f.replace(".jsonl", "")}  code ${rows[0]?.version ?? "?"}  ` + SYSTEMS.map(([s]) => `${s} ${sum[s]?.score ?? "-"}`).join("  ");
    });
    io.out((table.length ? ["PotterBench leaderboard (run, code version, score per system):", ...table].join("\n") : "No runs yet.") + "\n");
    return 0;
  }
  io.err("potter-bench: setup | run | grade | report | leaderboard\n");
  return 2;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).then((c) => { process.exitCode = c; });
}
