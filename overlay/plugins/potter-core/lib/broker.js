// Potter v3.3 model broker (ratified T28/T13): Potter, not a router outside it, picks the model for each turn from
// the validated candidates in the model policy, with a per-call ceiling from the policy and a daily spend ledger.
//   chat        the default for a conversation turn
//   escalation  hard reasoning: the owner asks for depth, a decision with trade-offs, or a very long request
// Scheduled jobs name their own model and are left alone. Over the daily escalation budget, turns stay on chat.
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";

export const DEPTH = /\b(think (hard|carefully|it through)|deep(ly)?[ -](dive|analysis|think)|analy[sz]e|strateg(y|ic|ize)|trade-?offs?|pros and cons|weigh (up )?the|second-order|stress-?test|devil'?s advocate|what am i missing|architect(ure)?|root cause|post-?mortem|plan out|roadmap)\b/i;

export function chooseClass(text, { domains = [], escalationSpentUsd = 0, escalationCapUsd = 3 } = {}) {
  const t = String(text ?? "");
  const wants = DEPTH.test(t) || t.length > 1500 || (domains.includes("money") && /\b(should i|decide|decision)\b/i.test(t));
  if (!wants) return { cls: "chat", reason: "ordinary turn" };
  if (escalationSpentUsd >= escalationCapUsd) return { cls: "chat", reason: `escalation budget for today used ($${escalationSpentUsd.toFixed(2)})` };
  return { cls: "escalation", reason: DEPTH.test(t) ? "the request asks for depth" : t.length > 1500 ? "a long request" : "a money decision" };
}

/** "openrouter/deepseek/deepseek-v4-pro" -> provider "openrouter", model "deepseek/deepseek-v4-pro". */
export function toOverride(policyClass) {
  if (!policyClass || typeof policyClass.model !== "string") return null;
  const [provider, ...rest] = policyClass.model.split("/");
  if (!rest.length) return null;
  return { providerOverride: provider, modelOverride: rest.join("/"), fallbacksOverride: [...(policyClass.fallbacks ?? [])] };
}

export function loadClasses(policyPath) {
  try {
    return JSON.parse(readFileSync(policyPath, "utf8")).classes ?? {};
  } catch {
    return {};
  }
}

// ---- spend ledger: one line per model call, content-free ------------------------------------------------------
export function recordSpend(path, entry, now = new Date()) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  appendFileSync(path, JSON.stringify({ ts: now.toISOString(), ...entry }) + "\n", { mode: 0o600 });
}

export function spentToday(path, { now = new Date(), cls } = {}) {
  if (!existsSync(path)) return 0;
  const day = now.toISOString().slice(0, 10);
  let sum = 0;
  for (const line of readFileSync(path, "utf8").split("\n")) {
    if (!line.startsWith(`{"ts":"${day}`)) continue;
    try {
      const r = JSON.parse(line);
      if ((!cls || r.cls === cls) && typeof r.costUsd === "number") sum += r.costUsd;
    } catch { /* skip */ }
  }
  return sum;
}

/** Cost from the provider's usage when it reports one, else from token counts and the policy's price hints. */
export function costOf(usage = {}) {
  const n = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  return n(usage.cost) ?? n(usage.costUsd) ?? n(usage.total_cost) ?? null;
}
