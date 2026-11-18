#!/usr/bin/env node
// potter-t1: the owner's T1 evaluation.
//
//   potter-t1 run [--only <id,...>]       run every task against v3 (an isolated session per task; actions blocked)
//   potter-t1 v2-sheet                    the numbered prompts, for the owner to send to v2
//   potter-t1 v2-answer <id> <file|->     store v2's answer to one task
//   potter-t1 grade                       grade every ungraded answer (v3 and v2) with Jev
//   potter-t1 report [--json]             scores, per category, head to head
//
// Files (in --dir, root-only): tasks.json (the owner-approved set), results.jsonl (answers and grades).
// Exit codes: 0 done; 2 bad arguments; 3 no tasks or no decider.
import { execFile } from "node:child_process";
import { appendFileSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { cleanTasks, EVAL_SESSION, gradeRequest, PREFIX, readGrade, render, replyText, report } from "./lib.js";

const rows = (p) => (existsSync(p) ? readFileSync(p, "utf8").split("\n").filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean) : []);
const save = (p, rs) => { const t = `${p}.tmp`; writeFileSync(t, rs.map((r) => JSON.stringify(r) + "\n").join(""), { mode: 0o600 }); renameSync(t, p); };

export function askV3({ openclaw = "openclaw", agent = "potter", runId, task, timeoutS = 300 }) {
  return new Promise((res) => {
    const t0 = Date.now();
    const args = ["agent", "--agent", agent, "--session-key", `${EVAL_SESSION}${runId}-${task.id}`, "--message", PREFIX + task.prompt,
      "--json", "--timeout", String(timeoutS)];
    const [bin, ...pre] = openclaw.split(" ").filter(Boolean);
    execFile(bin, [...pre, ...args], { timeout: (timeoutS + 30) * 1000, maxBuffer: 16 * 1024 * 1024 }, (err, stdout) => {
      let out = stdout;
      try { out = JSON.parse(String(stdout).slice(String(stdout).indexOf("{"))); } catch { /* plain text */ }
      res({ ok: !err, answer: replyText(out), ms: Date.now() - t0, error: err ? String(err.message).slice(0, 200) : null });
    });
  });
}

export async function main(argv, env = process.env, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }, deps = {}) {
  const [cmd, ...rest] = argv;
  let a;
  try {
    a = parseArgs({ args: rest, allowPositionals: true, options: { dir: { type: "string" }, decider: { type: "string" }, policy: { type: "string" },
      openclaw: { type: "string", default: "openclaw" }, only: { type: "string" }, json: { type: "boolean", default: false } } });
  } catch (e) { io.err(`potter-t1: ${e.message}\n`); return 2; }
  const o = a.values, pos = a.positionals;
  if (!o.dir) { io.err("potter-t1: --dir is required\n"); return 2; }
  const dir = resolve(o.dir), resultsPath = join(dir, "results.jsonl");
  let tasks = [];
  try { tasks = cleanTasks(JSON.parse(readFileSync(join(dir, "tasks.json"), "utf8"))); } catch { /* none yet */ }
  if (!tasks.length) { io.err("potter-t1: no approved tasks yet (state/t1/tasks.json): run the t1-eval skill first\n"); return 3; }

  if (cmd === "v2-sheet") {
    io.out(tasks.map((t, i) => `${i + 1}. [${t.id}] ${t.prompt}`).join("\n\n") + "\n");
    return 0;
  }
  if (cmd === "v2-answer") {
    const [id, file] = pos;
    const t = tasks.find((x) => x.id === id);
    if (!t || !file) { io.err("potter-t1: v2-answer <task id> <file or ->\n"); return 2; }
    const answer = file === "-" ? readFileSync(0, "utf8") : readFileSync(file, "utf8");
    const rs = rows(resultsPath).filter((r) => !(r.system === "v2" && r.task === id));
    rs.push({ system: "v2", task: id, answer: answer.trim(), ms: null, at: new Date().toISOString() });
    save(resultsPath, rs);
    io.out(`Stored v2's answer to ${id}.\n`);
    return 0;
  }
  if (cmd === "run") {
    const runId = new Date().toISOString().slice(0, 16).replace(/[:T-]/g, "");
    const only = o.only ? new Set(o.only.split(",")) : null;
    const ask = deps.askV3 ?? askV3;
    let n = 0;
    for (const t of tasks) {
      if (only && !only.has(t.id)) continue;
      const r = await ask({ openclaw: o.openclaw, runId, task: t });
      const rs = rows(resultsPath).filter((x) => !(x.system === "v3" && x.task === t.id));
      rs.push({ system: "v3", task: t.id, answer: r.answer, ms: r.ms, error: r.error, at: new Date().toISOString(), run: runId });
      save(resultsPath, rs);
      n++;
    }
    io.out(`Ran ${n} tasks against v3. Next: potter-t1 grade\n`);
    return 0;
  }
  if (cmd === "grade") {
    let decide = deps.decide;
    if (!decide && o.decider && o.policy) {
      const mod = await import(pathToFileURL(resolve(o.decider)).href);
      decide = mod.createDecider({ env, policyPath: o.policy, taskClass: "decision" });
    }
    if (!decide) { io.err("potter-t1: no decider\n"); return 3; }
    const rs = rows(resultsPath);
    let n = 0;
    for (const r of rs) {
      if (r.grade || !r.answer) continue;
      const t = tasks.find((x) => x.id === r.task);
      if (!t) continue;
      const out = await decide(gradeRequest(t, r.answer));
      if (out?.ok) { r.grade = readGrade(t, out.answers); n++; }
    }
    save(resultsPath, rs);
    io.out(`Graded ${n} answers.\n`);
    return 0;
  }
  if (cmd === "report") {
    const rep = report(tasks, rows(resultsPath));
    io.out((o.json ? JSON.stringify(rep, null, 1) : render(rep)) + "\n");
    return 0;
  }
  io.err("potter-t1: run | v2-sheet | v2-answer | grade | report\n");
  return 2;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).then((c) => { process.exitCode = c; });
}
