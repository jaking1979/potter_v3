import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync as writeSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { buildWorkspaces, exactScore, finalScore, loadTasks, main, SEED, WORLD } from "./bench.mjs";

test("the world and tasks are well formed and fictional", () => {
  const tasks = loadTasks();
  assert.equal(tasks.length, 30);
  assert.equal(new Set(tasks.map((t) => t.id)).size, 30);
  assert.ok(tasks.every((t) => ["easy", "medium", "hard"].includes(t.difficulty)));
  for (const f of ["USER.md", "MEMORY.md", "data/inbox.jsonl", "data/calendar.jsonl", "data/tasks.jsonl", "data/house.jsonl", "data/messages.jsonl"]) {
    const text = readFileSync(join(WORLD, f), "utf8");
    assert.doesNotMatch(text, /\b(josh|christie|molly|edie|catskills|bay ridge)\b/i, f);
  }
  assert.ok(SEED.entities.length >= 5);
});

test("both workspaces get the same files and notes; messages exist only as notes; v3 adds its machinery", () => {
  const root = mkdtempSync(join(tmpdir(), "bench-"));
  const mach = mkdtempSync(join(tmpdir(), "mach-"));
  writeSync(join(mach, "AGENTS.md"), "v3 machinery");
  const r = buildWorkspaces(root, { machinery: mach });
  for (const ws of [r.v3, r.v2]) {
    assert.ok(existsSync(join(ws, "data/inbox.jsonl")) && existsSync(join(ws, "USER.md")));
    assert.ok(!existsSync(join(ws, "data/messages.jsonl")));
    assert.match(readFileSync(join(ws, "memory/2027-03-01.md"), "utf8"), /piano moved to Tuesdays at 5:00/);
  }
  assert.equal(readFileSync(join(r.v3, "AGENTS.md"), "utf8"), "v3 machinery");
  assert.ok(!existsSync(join(r.v2, "AGENTS.md")));
});

test("scoring: exact checks with forbidden stale answers, blended with the Jev criteria", () => {
  const t = { id: "mem-2", expect_all: ["rosa", "0199"], forbid: ["0142"], criteria: ["a", "b"] };
  assert.equal(exactScore(t, "Call Rosa Diaz at 215-555-0199."), 1);
  assert.equal(exactScore(t, "Rosa (0199), or Dave at 215-555-0142"), 0);
  assert.equal(exactScore({ criteria: ["x"] }, "anything"), null);
  const all = { c0: { noul: 0.9 }, c1: { noul: 0.9 } };
  assert.equal(finalScore(t, "Rosa Diaz, 215-555-0199", all).score, 100);
  assert.equal(finalScore(t, "Dave Kim, 215-555-0142", { c0: { noul: 0.1 }, c1: { noul: 0.1 } }).score, 0);
});

test("run, grade, report and leaderboard end to end, resumable", async () => {
  const root = mkdtempSync(join(tmpdir(), "benchrun-"));
  const out = [];
  const io = { out: (x) => out.push(x), err: (x) => out.push(x) };
  const asked = [];
  const askV3 = async ({ agent, task }) => { asked.push(agent);
    assert.match(task.prompt, /^Today is Wednesday, March 10, 2027/);
    return { ok: true, answer: agent === "bench-v3" ? "Tuesdays at 5:00 with Ms. Okafor. Rosa Diaz 215-555-0199." : "Thursdays at 4:30.", ms: 2000 }; };
  assert.equal(await main(["run", "--root", root, "--only", "mem-1,mem-2", "--run", "r1", "--version", "abc1234"], {}, io, { askV3 }), 0);
  assert.equal(asked.length, 4);
  await main(["run", "--root", root, "--only", "mem-1,mem-2", "--run", "r1"], {}, io, { askV3 });
  assert.equal(asked.length, 4);                                                   // already answered: skipped
  const decide = async ({ state, questions }) => ({ ok: true, answers: Object.fromEntries(Object.keys(questions).map((k) =>
    [k, { noul: k === "dodges" || k === "unsupported" ? 0.05 : (/Tuesdays|Rosa/.test(state.answer) ? 0.9 : 0.1) }])) });
  assert.equal(await main(["grade", "--root", root, "--run", "r1"], {}, io, { decide }), 0);
  assert.equal(await main(["report", "--root", root, "--run", "r1"], {}, io), 0);
  const rep = out.pop();
  assert.match(rep, /^PotterBench r1 \(code abc1234\), 2 tasks\./);
  assert.match(rep, /v3: 100\/100, passed 2\/2/);
  assert.match(rep, /v2: 0\/100, passed 0\/2/);
  assert.equal(await main(["leaderboard", "--root", root], {}, io), 0);
  assert.match(out.pop(), /r1  code abc1234  v3 100  v2 0/);
});

