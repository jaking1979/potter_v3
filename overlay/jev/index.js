// Jev, Potter's decision layer (v3). Four small parts:
//   policy.js     the model policy format, validation, and choose(task, policy, health)
//   client.js     thin client for the typed-decision endpoint (fetch and key injected)
//   telemetry.js  one content-free JSON line per decision
//   decide()      below: choose a model within the policy, ask, fall back once per unhealthy model, log
// v2's task router, skill router, memory gate and context filter were shadow-only experiments and are
// not carried; they can be rebuilt on decide() when there is evidence they pay for themselves.
import { createDecisionClient } from "./client.js";
import { choose, loadPolicy } from "./policy.js";
import { createTelemetry, fileSink } from "./telemetry.js";

export * from "./client.js";
export * from "./policy.js";
export * from "./telemetry.js";

export const UNHEALTHY_MS = 5 * 60 * 1000;

/**
 * One decision, inside the policy.
 *
 * @param task   task class name, or { class, estimatedCostUsd? }
 * @param input  { state, questions }
 * @param deps   { policy, client, health?, telemetry?, now? }
 *   `health` is a plain object the caller owns; a failed model is marked unhealthy in it for
 *   UNHEALTHY_MS so the next decision skips it. Pass the same object across calls to keep that memory.
 * @returns { ok: true, answers, model, costUsd, latencyMs, reason } | { ok: false, reason, model }
 */
export async function decide(task, { state, questions }, { policy, client, health = {}, telemetry, now = Date.now } = {}) {
  const started = now();
  let attempts = 0;
  let failure = null;
  let sel = choose(task, policy, health, { now: started });
  let result = null;
  while (sel.model) {
    attempts++;
    const out = await client.ask({ model: sel.model, state, questions, timeoutMs: sel.timeoutMs });
    if (out.ok) { result = out; break; }
    failure = out.kind + (out.status ? `:${out.status}` : "");
    if (out.kind === "no-key" || out.kind === "no-fetch") break; // no other model can fix that
    health[sel.model] = { ok: false, until: now() + UNHEALTHY_MS };
    sel = choose(task, policy, health, { now: now() });
  }
  const latencyMs = Math.max(0, now() - started);
  const overCeiling = Boolean(result && typeof result.costUsd === "number" && result.costUsd > sel.maxCostUsd);
  telemetry?.record({
    ts: new Date(started).toISOString(), taskClass: sel.taskClass, model: sel.model, reason: sel.reason, latencyMs,
    ok: Boolean(result), attempts, failure: result ? null : failure, costUsd: result?.costUsd ?? null, overCeiling,
  });
  if (!result) return { ok: false, reason: failure ?? sel.reason, model: sel.model, latencyMs };
  return { ok: true, answers: result.answers, model: result.model, costUsd: result.costUsd, latencyMs, reason: sel.reason, overCeiling };
}

/**
 * Build a ready-to-call decide({ purpose, state, questions }) for a command-line program, from the
 * environment. Returns null (not an error) when there is nothing to decide with: no key or no policy.
 * A policy file that exists but is wrong still throws, because that is a mistake someone must see.
 *
 * env: OPENROUTER_API_KEY (the key), POTTER_MODEL_POLICY (policy path), POTTER_JEV_LOG (telemetry file),
 *      POTTER_DECISIONS_URL (endpoint override). Arguments win over the environment.
 */
export function createDecider({ env = process.env, policyPath, taskClass = "decision", telemetryPath, fetchImpl = globalThis.fetch, now } = {}) {
  const apiKey = env.OPENROUTER_API_KEY;
  const file = policyPath || env.POTTER_MODEL_POLICY;
  if (!apiKey || !file) return null;
  const policy = loadPolicy(file);
  const client = createDecisionClient({ apiKey, fetchImpl, endpoint: env.POTTER_DECISIONS_URL || undefined, now });
  const log = telemetryPath || env.POTTER_JEV_LOG;
  const telemetry = log ? createTelemetry(fileSink(log)) : undefined;
  const health = {};
  return ({ state, questions }) => decide(taskClass, { state, questions }, { policy, client, health, telemetry, now });
}
