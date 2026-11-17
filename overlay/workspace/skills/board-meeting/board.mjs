#!/usr/bin/env node
// Board meeting (v3): one program runs the whole meeting, so Potter only starts it and relays the report.
// Same protections as v2: secrets redacted before any model sees anything; EHR or client material stops the
// meeting; sensitive content needs the owner's approval of the redacted packet; directors answer independently
// and never see each other; the chair evaluates anonymised memoranda; advisory only, nothing is acted on.
// Models come from the model policy (classes board_chair, board_director_*), through the owner's OpenRouter key.
//
//   echo '{"conversation":"...","question":"optional"}' | node board.mjs run [--full]
//   echo '{"approved_packet":{...}}'                      | node board.mjs run       (after the owner approved)
// Output (JSON): {status: ok|blocked|clarify|approval_required|failed, report?, packet?, questions?, participation, run_id}
import { randomInt, randomUUID } from "node:crypto";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REF = (f) => readFileSync(join(HERE, "references", f), "utf8");

export const packetShape = { board_question: "", decision_type: "", background: "", known_facts: [], assumptions_or_unverified_claims: [],
  owner_goals: [], constraints: [], options_already_discussed: [], tentative_position: "", important_uncertainties: [], requested_output: "" };
export const memoShape = { recommendation: "", reasoning: [], important_assumptions: [], risks_and_second_order_effects: [],
  strongest_counterargument: "", missing_information: [], facts_requiring_verification: [], what_would_change_my_recommendation: [],
  recommended_next_step: "", confidence: 0 };
export const HEADINGS = ["# Board Meeting", "## Question", "## Recommendation", "## Why", "## Where the Board Agreed", "## Strongest Dissent",
  "## Important Uncertainties", "## What Would Change the Recommendation", "## Recommended Next Move", "## Confidence", "## Board Participation"];
export const DEFAULT_SEATS = {
  board_chair: "openrouter/openai/gpt-5.6-terra",
  board_director_openai: "openrouter/openai/gpt-5.6-sol",
  board_director_anthropic: "openrouter/anthropic/claude-opus-5.5",
  board_director_google: "openrouter/google/gemini-3.1-pro-preview",
};

const SECRET = [/((?:api[_ -]?key|access[_ -]?token|password|secret|authorization|auth(?:entication)?[_ -]?code)\s*[:=]\s*)[^\s,;]+/gi,
  /\b(?:\d[ -]*?){13,19}\b/g, /\b\d{3}-\d{2}-\d{4}\b/g, /\bsk-[A-Za-z0-9_-]{8,}/g, /\b\d{5,12}:[A-Za-z0-9_-]{20,}\b/g];
const EHR = /\b(electronic health record|ehr|patient(?:\s+name|\s+id)?|client(?:\s+name|\s+id)?|clinical note|therapy session)\b/i;
const SENSITIVE = /\b(diagnosis|medication|employer|employment|lawsuit|legal advice|divorce|income|bank account|investment account|tax return)\b/i;

export function redact(v) {
  let s = String(v ?? "");
  for (const p of SECRET) s = s.replace(p, (m, prefix) => (typeof prefix === "string" && prefix ? `${prefix}[REDACTED]` : "[REDACTED]"));
  return s;
}
export function extract(v) {
  if (v && typeof v === "object") return v;
  const m = String(v ?? "").match(/\{[\s\S]*\}/);
  if (!m) throw new Error("no JSON object");
  return JSON.parse(m[0]);
}
export function normalize(v, shape) {
  return Object.fromEntries(Object.entries(shape).map(([k, d]) => [k, Array.isArray(d)
    ? (Array.isArray(v?.[k]) ? v[k].map(String) : v?.[k] == null ? [] : [String(v[k])])
    : typeof d === "number" ? Math.max(0, Math.min(100, Number(v?.[k]) || 0)) : v?.[k] == null ? "" : String(v[k])]));
}
export function anonymize(rows) {
  const r = [...rows];
  for (let i = r.length - 1; i > 0; i--) { const j = randomInt(i + 1); [r[i], r[j]] = [r[j], r[i]]; }
  return { memoranda: r.map((x, i) => ({ label: "Director " + String.fromCharCode(65 + i), memorandum: x.memorandum })),
    mapping: Object.fromEntries(r.map((x, i) => ["Director " + String.fromCharCode(65 + i), x.seat])) };
}
export function missingHeadings(report) {
  return HEADINGS.filter((h) => !String(report ?? "").includes(h));
}

function seats(policyPath) {
  let classes = {};
  try { classes = JSON.parse(readFileSync(policyPath, "utf8")).classes ?? {}; } catch { /* defaults */ }
  const pick = (k) => (classes[k]?.model ?? DEFAULT_SEATS[k]);
  const directors = Object.keys({ ...DEFAULT_SEATS, ...classes }).filter((k) => k.startsWith("board_director_"));
  return { chair: pick("board_chair"), directors: directors.map((k) => ({ seat: k.replace("board_director_", ""), model: pick(k) })) };
}

/** One OpenRouter chat call. Returns {ok, text, ms, cost, error}. `post` is injectable for tests. */
// Reasoning models (Claude, Gemini Pro) spend part of max_tokens thinking; too small a budget returns no text at all.
// Budgets are generous and reasoning is kept moderate; the reply text can come as a string or as content parts.
export function replyText(json) {
  const c = json?.choices?.[0]?.message?.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map((p) => (typeof p === "string" ? p : p?.text ?? "")).join("");
  return null;
}

export async function chat(model, system, user, { key, maxTokens = 8000, timeoutMs = 240000, post } = {}) {
  const t0 = Date.now();
  const body = { model: model.replace(/^openrouter\//, ""), max_tokens: maxTokens, reasoning: { effort: "medium" },
    messages: [{ role: "system", content: system }, { role: "user", content: user }] };
  try {
    const res = post ? await post(body) : await (async () => {
      const ac = new AbortController(); const tm = setTimeout(() => ac.abort(), timeoutMs);
      try {
        const r = await fetch("https://openrouter.ai/api/v1/chat/completions", { method: "POST", signal: ac.signal,
          headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" }, body: JSON.stringify(body) });
        return { status: r.status, json: await r.json().catch(() => null) };
      } finally { clearTimeout(tm); }
    })();
    const text = replyText(res?.json);
    if (res.status !== 200) {
      const why = String(res?.json?.error?.message ?? "").replace(/sk-[A-Za-z0-9_-]+/g, "(key)").slice(0, 160);
      return { ok: false, ms: Date.now() - t0, error: `http_${res.status}${why ? ": " + why : ""}` };
    }
    if (!text || !text.trim()) {
      const fin = res?.json?.choices?.[0]?.finish_reason ?? "none";
      return { ok: false, ms: Date.now() - t0, error: `empty_reply (finish_reason ${fin})` };
    }
    return { ok: true, text, ms: Date.now() - t0, cost: res.json?.usage?.cost ?? null };
  } catch (e) {
    return { ok: false, ms: Date.now() - t0, error: e?.name === "AbortError" ? "timeout" : "network" };
  }
}

export async function runMeeting(input, { key, policyPath, runsDir, full = false, post } = {}) {
  const run_id = randomUUID();
  const record = (row) => {
    if (!runsDir) return;
    mkdirSync(runsDir, { recursive: true, mode: 0o700 });
    appendFileSync(join(runsDir, `${run_id}.jsonl`), JSON.stringify({ ts: new Date().toISOString(), run_id, ...row }) + "\n", { mode: 0o600 });
  };
  const { chair, directors } = seats(policyPath);
  const errors = new Map();
  const lastError = (seat) => errors.get(`director-${seat}`) ?? "unknown";
  const call = async (role, model, system, user, maxTokens) => {
    for (let attempt = 1; attempt <= 2; attempt++) {
      const r = await chat(model, system, user, { key, maxTokens, post });
      record({ event: "call", role, model, attempt, outcome: r.ok ? "success" : r.error, ms: r.ms, cost: r.cost ?? null });
      if (r.ok) return r;
      errors.set(role, r.error);
    }
    return null;
  };

  let packet;
  if (input.approved_packet) {
    packet = normalize(extract(input.approved_packet), packetShape);
  } else {
    const raw = String(input.conversation ?? "");
    if (EHR.test(raw) || EHR.test(String(input.question ?? ""))) {
      record({ event: "blocked" });
      return { status: "blocked", reason: "Encountered excluded EHR or client material. Nothing was sent to any model.", run_id };
    }
    const sys = REF("board-packet-schema.md") + "\n\n" + REF("privacy-rules.md");
    const user = (input.question ? `Board question: ${redact(input.question)}\n\n` : "") + "Conversation:\n" + redact(raw).slice(0, 40000);
    const r = await call("chair-packet", chair, sys, user, 6000);
    if (!r) return { status: "failed", reason: "the chair could not prepare the packet", run_id };
    let obj;
    try { obj = extract(r.text); } catch { return { status: "failed", reason: "the chair's packet was not JSON", run_id }; }
    if (obj.clarification_required || obj.status === "clarification_required") {
      return { status: "clarify", questions: obj.questions ?? obj.clarification_required, run_id };
    }
    packet = JSON.parse(redact(JSON.stringify(normalize(obj, packetShape))));
    if (SENSITIVE.test(JSON.stringify(packet))) {
      record({ event: "approval_required" });
      return { status: "approval_required", packet, run_id,
        note: "Show the owner this redacted packet. Continue only on approval, with {\"approved_packet\": <packet>}." };
    }
  }

  const prompt = REF("director-prompt.md") + "\n\nBoard packet:\n" + JSON.stringify(packet);
  const results = await Promise.all(directors.map(async (d) => {
    const r = await call(`director-${d.seat}`, d.model, "You are an independent board director.", prompt, 8000);
    if (!r) return { seat: d.seat, ok: false, why: lastError(d.seat) };
    try { return { seat: d.seat, ok: true, memorandum: normalize(extract(r.text), memoShape) }; } catch {
      record({ event: "call", role: `director-${d.seat}`, model: d.model, attempt: 0, outcome: "not_json" });
      return { seat: d.seat, ok: false, why: "answer was not JSON" };
    }
  }));
  const ok = results.filter((x) => x.ok);
  const participation = results.map((x) => `${x.seat}: ${x.ok ? "answered" : "did not answer (" + (x.why ?? "unknown") + ")"}`);
  if (ok.length < 2) return { status: "failed", reason: "fewer than two directors answered", participation, run_id };

  const anon = anonymize(ok);
  record({ event: "mapping", mapping: anon.mapping });                 // kept in the run file, revealed only on request
  // The chair sees only how many answered: never which maker is behind which memorandum.
  const evalUser = `Board packet:\n${JSON.stringify(packet)}\n\nMemoranda:\n${JSON.stringify(anon.memoranda)}\n\n` +
    `Participation: ${ok.length} of ${results.length} directors answered.`;
  let report = (await call("chair-report", chair, REF("chair-evaluation-rubric.md"), evalUser, 8000))?.text ?? "";
  if (missingHeadings(report).length) {
    report = (await call("chair-report-repair", chair, REF("chair-evaluation-rubric.md"),
      evalUser + `\n\nYour previous report was missing: ${missingHeadings(report).join(", ")}. Return the full report with every heading.`, 8000))?.text ?? report;
  }
  if (!report) return { status: "failed", reason: "the chair did not return a report", participation, run_id };
  if (full) report += "\n\n## Memoranda\n" + anon.memoranda.map((m) => `### ${m.label}\n${JSON.stringify(m.memorandum, null, 1)}`).join("\n");
  return { status: "ok", report, participation, run_id, missing_headings: missingHeadings(report) };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let text = "";
  for await (const c of process.stdin) text += c;
  const env = (() => { try { return readFileSync(process.env.POTTER_JEV_ENV || "/etc/potter/secrets/jev.env", "utf8"); } catch { return ""; } })();
  const key = (env.match(/^OPENROUTER_API_KEY=(.+)$/m) || [])[1];
  if (!key) { console.log(JSON.stringify({ status: "failed", reason: "no OpenRouter key available" })); process.exit(1); }
  const ws = process.env.POTTER_WORKSPACE || join(HERE, "..", "..");
  const out = await runMeeting(JSON.parse(text || "{}"), { key, policyPath: join(ws, "state", "model-policy.json"),
    runsDir: join(ws, "state", "board-runs"), full: process.argv.includes("--full") });
  console.log(JSON.stringify(out));
}
