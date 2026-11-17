// Potter v3.2 Context Governor (System One, "00 Brain Architecture"): before the main model sees a turn, decide
// what the turn is about and hand the model a short plan: which rules to read, which skill to use, which tools
// matter. Step 1 is a table (free, instant, same answer every time). Step 2, only when the table finds nothing,
// is one Jev decision with a hard timeout. A failure in either step means no plan, never a blocked turn.

export const DOMAINS = [
  { id: "house", re: /\b(house|home|cabin|thermostat|heat(ing)?|temp(erature)?s?|warm|cold|furnace|hvac|mini-?split|basement|doors?|windows?|lock (it )?up|close (it )?up|merrick|setpoint|degrees|frozen|freez\w*|pipes?|away (mode|hold)|(family|living|bed|dining) ?room|bedroom|kitchen|den|upstairs|downstairs|zones?)\b|\b(set|turn|raise|lower|bump|make) (it|the \w+( \w+)?)( up| down)? to \d{2}\b/i,
    servers: ["homeassistant"], skills: ["home-status"], rules: [],
    hint: 'Run `merrick ask "<the owner\'s words>"` first and answer from its JSON. Use Home Assistant tools only when it returns path "fallback".' },
  { id: "email", re: /\b(e-?mails?|inbox|mail|replies|reply|forward|newsletters?|unsubscribe|agentmail)\b/i,
    servers: ["composio"], skills: ["inbox-triage", "reply-monitor"], rules: ["global/email-pipeline.md", "global/permissions.md"] },
  { id: "calendar", re: /\b(calendar|meetings?|appointments?|events? (on|this|next)|free (time|slot)|busy|availability|reschedule)\b/i,
    servers: ["composio"], skills: [], rules: ["global/retrieval.md"] },
  { id: "notion", re: /\bnotion\b/i, servers: ["notion"], skills: [], rules: ["global/retrieval.md"] },
  { id: "automation", re: /\b(cron|automations?|scheduled (job|task)s?|jobs?|remind(er|ers| me)|every (day|morning|evening|week|night|hour)|standby|take over|hand (it )?back)\b/i,
    servers: [], skills: ["handover"], rules: ["global/schedules.md"] },
  { id: "money", re: /\b(trad(e|es|ing)|stocks?|portfolio|invest\w*|deals?|credit cards?|points|miles|bank\w*|budget|bills?|mandate)\b/i,
    servers: ["composio"], skills: ["trading-run", "financial-scan", "financial-digest"], rules: ["global/permissions.md"] },
  { id: "memory", re: /\b(remember|forget|you forgot|that'?s wrong|correction|i prefer|preference)\b/i,
    servers: [], skills: [], rules: ["global/memory.md"] },
  { id: "verify", re: /\b(did you|have you|is it done|are you sure|status of|follow(ed)? up)\b/i,
    servers: [], skills: [], rules: ["global/verification.md"] },
  { id: "local", re: /\b(this weekend|concerts?|shows?|what'?s on|things to do|events (near|around|in)|margaretville|catskills)\b/i,
    servers: [], skills: ["events-digest", "local-monitor"], rules: [] },
  { id: "food", re: /\b(dinner|lunch|breakfast|eat|eating|restaurants?|takeout|take-out|delivery|order (food|in|out)|cook(ing)?|recipes?|meal ?plan|meals?|groceries|grocery|wine)\b/i,
    servers: ["composio", "notion"], skills: ["meal-plan"], rules: [],
    hint: "Read preferences/food-wine.md (the owner's food and wine preferences) before suggesting anything." },
  { id: "brief", re: /\b(brief|my day|today|tonight|tomorrow|this week|plan for)\b/i,
    servers: ["composio"], skills: ["morning-brief", "evening-checkin"], rules: ["global/retrieval.md"] },
];

const BY_ID = new Map(DOMAINS.map((d) => [d.id, d]));
const ENVELOPE = /^\s*(\[[^\]]{0,200}\]|(conversation|sender|channel|message|chat|from|thread)\b[^:\n]{0,40}:)/i;

/** The owner's own words from a prepared prompt that may carry channel context lines. */
export function requestText(event = {}) {
  if (typeof event.currentUserMessage === "string") return event.currentUserMessage.trim();
  const raw = String(event.cleanedBody ?? event.prompt ?? "");
  const blocks = raw.split(/\n\s*\n/).map((b) => b.split("\n").filter((l) => !ENVELOPE.test(l)).join("\n").trim()).filter(Boolean);
  return (blocks[blocks.length - 1] ?? "").slice(0, 2000);
}

export function tableDomains(text) {
  return DOMAINS.filter((d) => d.re.test(text)).map((d) => d.id);
}

/** Jev questions: one yes/no per domain. */
export function jevQuestions() {
  const q = {};
  for (const d of DOMAINS) {
    q[d.id] = { type: "noul", instructions: `Does this request concern ${d.id === "brief" ? "the owner's day, agenda or plans" : d.id}?`,
      criteria: { true: `Clearly about ${d.id}`, false: `Not about ${d.id}` } };
  }
  return q;
}

export function jevDomains(answers = {}, min = 0.7) {
  return DOMAINS.map((d) => d.id).filter((id) => {
    const a = answers[id];
    if (!a || typeof a !== "object") return false;
    // Jev answers yes/no as a probability: { noul } (v2's endpoint) or { probabilityTrue } (OpenClaw's service).
    const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
    const p = num(a.noul) ?? num(a.probabilityTrue) ?? num(a.probabilities?.true);
    if (p !== null) return p >= min;
    const yes = a.value === true || a.choice === "true" || a.answer === true;
    return yes && (num(a.confidence) ?? 0) >= min;
  });
}

/** The plan text the model sees, kept short so it adds focus, not bulk. */
export function planText(domains) {
  const ds = domains.map((id) => BY_ID.get(id)).filter(Boolean);
  if (!ds.length) return "";
  const rules = [...new Set(ds.flatMap((d) => d.rules))];
  const skills = [...new Set(ds.flatMap((d) => d.skills))];
  const servers = [...new Set(ds.flatMap((d) => d.servers))];
  const lines = [`Turn plan (Potter Context Governor): this request concerns ${ds.map((d) => d.id).join(", ")}.`];
  if (rules.length) lines.push(`Read first: ${rules.map((r) => "rules/" + r).join(", ")}.`);
  if (skills.length) lines.push(`Skills that fit: ${skills.join(", ")}.`);
  if (servers.length) lines.push(`Connected apps that matter here: ${servers.join(", ")}; others are not needed for this turn.`);
  for (const d of ds) if (d.hint) lines.push(d.hint);
  lines.push("Answer the request; do not survey unrelated areas.");
  return lines.join("\n");
}

/**
 * @param text     the owner's words
 * @param decide   optional async ({purpose, state, questions}) => {ok, answers}
 * @returns { domains, source: "table"|"jev"|"none", context, latencyMs, error? }
 */
export async function plan(text, { decide, timeoutMs = 2500, now = Date.now } = {}) {
  const started = now();
  const t = String(text ?? "").trim();
  if (!t) return { domains: [], source: "none", context: "", latencyMs: 0 };
  let domains = tableDomains(t);
  let source = domains.length ? "table" : "none";
  let error;
  if (!domains.length && decide && t.length >= 12) {
    try {
      const out = await Promise.race([
        decide({ purpose: "potter-governor", state: { request: t.slice(0, 1500) }, questions: jevQuestions() }),
        new Promise((r) => setTimeout(() => r({ ok: false, reason: "timeout" }), timeoutMs)),
      ]);
      if (out?.ok) {
        domains = jevDomains(out.answers);
        if (domains.length) source = "jev";
      } else error = out?.reason ?? "no answer";
    } catch (e) {
      error = e?.name ?? "error";
    }
  }
  return { domains, source, context: planText(domains), latencyMs: Math.max(0, now() - started), ...(error ? { error } : {}) };
}
