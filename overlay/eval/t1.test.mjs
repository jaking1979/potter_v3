import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { cleanTasks, gradeRequest, readGrade, replyText } from "./lib.js";
import { main } from "./t1.mjs";

const TASKS = { tasks: [
  { id: "brief1", category: "brief", prompt: "What does tomorrow look like?", criteria: ["Names tomorrow's calendar events", "Flags anything due"] },
  { id: "food1", category: "food", prompt: "What wine with lamb on Friday?", criteria: ["Suggests a specific wine", "Fits the owner's stated taste"], new_info: true },
  { id: "bad", category: "x", prompt: "", criteria: ["x"] },
] };

test("tasks are cleaned: empty prompts and criteria-less tasks are dropped", () => {
  assert.deepEqual(cleanTasks(TASKS).map((t) => t.id), ["brief1", "food1"]);
});

test("the reply is found in the shapes the agent command may print", () => {
  assert.equal(replyText({ reply: "a" }), "a");
  assert.equal(replyText({ result: { payloads: [{ text: "b" }, { text: "c" }] } }), "b\nc");
  assert.equal(replyText({ messages: [{ role: "user", content: "q" }, { role: "assistant", content: [{ type: "text", text: "d" }] }] }), "d");
  assert.equal(replyText("plain"), "plain");
});

test("grading: one narrow question per criterion plus dodge and invention checks, composed in code", () => {
  const t = cleanTasks(TASKS)[0];
  assert.deepEqual(Object.keys(gradeRequest(t, "x").questions), ["c0", "c1", "dodges", "unsupported"]);
  assert.deepEqual(readGrade(t, { c0: { noul: 0.9 }, c1: { noul: 0.8 } }).score, 100);
  assert.equal(readGrade(t, { c0: { noul: 0.9 }, c1: { noul: 0.2 } }).score, 50);
  assert.equal(readGrade(t, { c0: { noul: 0.9 }, c1: { noul: 0.9 }, unsupported: { noul: 0.9 } }).score, 80);
});

test("run (actions blocked by the session name), v2 answers, grade and report end to end", async () => {
  const dir = mkdtempSync(join(tmpdir(), "t1-"));
  writeFileSync(join(dir, "tasks.json"), JSON.stringify(TASKS));
  const out = [];
  const io = { out: (x) => out.push(x), err: (x) => out.push(x) };
  const asked = [];
  const askV3 = async ({ task, agent }) => { asked.push(`${agent}:${task.id}`);
    if (agent === "potter-v2ref") return { ok: true, answer: "Not sure what's on tomorrow.", ms: 3000 };
    return { ok: true, answer: task.id === "brief1" ? "Dentist at 9, taxes due" : "Some wine", ms: 4000 }; };
  assert.equal(await main(["run", "--dir", dir], {}, io, { askV3 }), 0);
  assert.deepEqual(asked, ["potter:brief1", "potter-v2ref:brief1", "potter:food1", "potter-v2ref:food1"]);
  const f = join(dir, "v2.txt"); writeFileSync(f, "Not sure what's on tomorrow.");
  assert.equal(await main(["v2-answer", "--dir", dir, "brief1", f], {}, io), 0);          // a real v2 answer replaces the baseline's
  const decide = async ({ state, questions }) => ({ ok: true, answers: Object.fromEntries(Object.keys(questions).map((k) => {
    if (k === "dodges") return [k, { noul: /Not sure/.test(state.answer) ? 0.9 : 0.1 }];
    if (k === "unsupported") return [k, { noul: 0.1 }];
    return [k, { noul: /Dentist/.test(state.answer) ? 0.9 : 0.2 }];
  })) });
  assert.equal(await main(["grade", "--dir", dir], {}, io, { decide }), 0);
  assert.equal(await main(["report", "--dir", dir], {}, io), 0);
  const text = out.pop();
  assert.match(text, /^T1 evaluation\. v3: 50\/100 avg, 1\/2 passed, 4s avg\. v2 \(baseline from v2's own files\): 0\/100 avg, 0\/2 passed/);
  assert.match(text, /Head to head on 2 tasks: v3 better 1, v2 better 0, even 1\./);
  assert.match(text, /Known before the install \(1 tasks\): v3 100, v2 0\. Told to v3 since \(1 tasks; v2 baseline given the same messages as notes\): v3 0, v2 0\./);
  assert.ok(readFileSync(join(dir, "results.jsonl"), "utf8").includes('"system":"v2"'));
  assert.equal(await main(["report", "--dir", mkdtempSync(join(tmpdir(), "t1e-"))], {}, io), 3);
});
