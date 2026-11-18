// T1 evaluation: the owner's own tasks, run against v3 (and v2's answers when the owner collects them), graded the
// same way. Grading is the TypeSafe pattern: one narrow Jev yes/no per criterion, composed in code.
// Pure: tasks, answers and Jev answers in; prompts, grades and the report out.

export const EVAL_SESSION = "t1-eval-";
export const PREFIX = "This is an evaluation run. Answer exactly as you would for real, using whatever you would normally " +
  "read or look up, but do not send, change, book, buy or schedule anything: describe what you would do instead.\n\n";

export function cleanTasks(raw) {
  const list = Array.isArray(raw?.tasks) ? raw.tasks : Array.isArray(raw) ? raw : [];
  return list.map((t, i) => ({
    id: String(t?.id ?? `t${i + 1}`).replace(/[^a-z0-9_-]/gi, "").slice(0, 20) || `t${i + 1}`,
    category: String(t?.category ?? "general").slice(0, 40),
    prompt: String(t?.prompt ?? "").trim().slice(0, 2000),
    criteria: (Array.isArray(t?.criteria) ? t.criteria : []).map((c) => String(c).trim()).filter(Boolean).slice(0, 8),
  })).filter((t) => t.prompt && t.criteria.length);
}

/** The reply text from `openclaw agent --json`, whatever shape this version prints. */
export function replyText(out) {
  if (out == null) return "";
  if (typeof out === "string") return out;
  const pick = (o) => {
    if (!o || typeof o !== "object") return null;
    for (const k of ["reply", "text", "output", "content", "message"]) if (typeof o[k] === "string" && o[k].trim()) return o[k];
    if (Array.isArray(o.payloads)) { const t = o.payloads.map((p) => p?.text).filter(Boolean).join("\n"); if (t) return t; }
    if (Array.isArray(o.messages)) {
      const last = [...o.messages].reverse().find((m) => m?.role === "assistant");
      if (last) return typeof last.content === "string" ? last.content : (last.content ?? []).map((c) => c?.text ?? "").join("");
    }
    for (const k of ["result", "data", "response"]) { const r = pick(o[k]); if (r) return r; }
    return null;
  };
  return pick(out) ?? "";
}

/** One Jev request per answer: does it meet each criterion? Plus two quality checks every task gets. */
export function gradeRequest(task, answer) {
  const questions = {};
  task.criteria.forEach((c, i) => {
    questions[`c${i}`] = {
      type: "noul",
      instructions: { question: `Does \`answer\` meet \`criteria[${i}]\`?`, focus: "Judge only what the answer says, against this one criterion." },
      criteria: { true: { what: "Clearly met" }, false: { what: "Missing, wrong, or only hinted at" } },
    };
  });
  questions.dodges = { type: "noul", instructions: { question: "Does `answer` avoid the task (asks something it could have looked up, or gives a generic answer with nothing specific to this owner)?" } };
  questions.unsupported = { type: "noul", instructions: { question: "Does `answer` state specific facts (dates, names, amounts) with no indication of where they came from that look invented?" } };
  return { state: { task: task.prompt, criteria: task.criteria, answer: String(answer ?? "").slice(0, 12000) }, questions };
}

const yes = (a) => (typeof a?.noul === "number" ? a.noul : typeof a?.probabilityTrue === "number" ? a.probabilityTrue : null);

/** Score 0-100: share of criteria met (p >= 0.7), minus 20 for dodging, minus 20 for invented specifics. */
export function readGrade(task, answers = {}) {
  const per = task.criteria.map((_, i) => yes(answers[`c${i}`]));
  const met = per.filter((p) => p !== null && p >= 0.7).length;
  const dodge = (yes(answers.dodges) ?? 0) >= 0.7, invented = (yes(answers.unsupported) ?? 0) >= 0.7;
  const score = Math.max(0, Math.round((100 * met) / task.criteria.length) - (dodge ? 20 : 0) - (invented ? 20 : 0));
  return { score, met, of: task.criteria.length, per, dodge, invented, pass: score >= 70 };
}

export function report(tasks, results) {
  const by = (sys) => results.filter((r) => r.system === sys && r.grade);
  const sum = (xs, f) => xs.reduce((s, x) => s + (f(x) ?? 0), 0);
  const line = (sys) => {
    const rs = by(sys);
    if (!rs.length) return null;
    const lat = rs.map((r) => r.ms).filter((x) => typeof x === "number");
    return { n: rs.length, avgScore: Math.round(sum(rs, (r) => r.grade.score) / rs.length), passed: rs.filter((r) => r.grade.pass).length,
      avgSeconds: lat.length ? Math.round(sum(lat, (x) => x) / lat.length / 100) / 10 : null };
  };
  const cats = [...new Set(tasks.map((t) => t.category))];
  const perCat = cats.map((c) => {
    const ids = new Set(tasks.filter((t) => t.category === c).map((t) => t.id));
    const avg = (sys) => { const rs = by(sys).filter((r) => ids.has(r.task)); return rs.length ? Math.round(sum(rs, (r) => r.grade.score) / rs.length) : null; };
    return { category: c, v3: avg("v3"), v2: avg("v2") };
  });
  const head = (sys) => new Map(by(sys).map((r) => [r.task, r.grade.score]));
  const v3 = head("v3"), v2 = head("v2");
  const both = [...v3.keys()].filter((k) => v2.has(k));
  return { v3: line("v3"), v2: line("v2"), perCategory: perCat,
    headToHead: both.length ? { tasks: both.length, v3Better: both.filter((k) => v3.get(k) > v2.get(k) + 10).length,
      v2Better: both.filter((k) => v2.get(k) > v3.get(k) + 10).length, even: both.filter((k) => Math.abs(v3.get(k) - v2.get(k)) <= 10).length } : null };
}

export function render(rep) {
  const s = (x) => (x ? `${x.avgScore}/100 avg, ${x.passed}/${x.n} passed${x.avgSeconds != null ? `, ${x.avgSeconds}s avg` : ""}` : "not run");
  const lines = [`T1 evaluation. v3: ${s(rep.v3)}. v2: ${s(rep.v2)}.`];
  if (rep.headToHead) lines.push(`Head to head on ${rep.headToHead.tasks} tasks: v3 better ${rep.headToHead.v3Better}, v2 better ${rep.headToHead.v2Better}, even ${rep.headToHead.even}.`);
  for (const c of rep.perCategory) lines.push(`- ${c.category}: v3 ${c.v3 ?? "-"}, v2 ${c.v2 ?? "-"}`);
  return lines.join("\n");
}
