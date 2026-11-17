// The model policy: one JSON file that says, per task class, which model to use, its fallbacks, a
// per-call cost ceiling and a timeout. Everything here is pure except loadPolicy (reads one file).
import { readFileSync } from "node:fs";

export const POLICY_VERSION = 1;
export const PLACEHOLDER = /^REPLACE_/;

const isId = (v) => typeof v === "string" && v.trim().length > 0 && v === v.trim();
const isPositive = (v) => typeof v === "number" && Number.isFinite(v) && v > 0;

/** Every problem with a policy object, as readable strings. An empty list means it is usable. */
export function validatePolicy(policy, { allowPlaceholders = false } = {}) {
  const errors = [];
  if (!policy || typeof policy !== "object" || Array.isArray(policy)) return ["policy must be a JSON object"];
  if (policy.version !== POLICY_VERSION) errors.push(`version must be ${POLICY_VERSION}`);
  const classes = policy.classes;
  if (!classes || typeof classes !== "object" || Array.isArray(classes) || Object.keys(classes).length === 0) {
    errors.push("classes must be a non-empty object");
    return errors;
  }
  for (const [name, c] of Object.entries(classes)) {
    const at = `classes.${name}`;
    if (!/^[a-z][a-z0-9_]*$/.test(name)) errors.push(`${at}: class names are lower_snake_case`);
    if (!c || typeof c !== "object" || Array.isArray(c)) { errors.push(`${at} must be an object`); continue; }
    if (!isId(c.model)) errors.push(`${at}.model must be a non-empty string`);
    if (!Array.isArray(c.fallbacks) || !c.fallbacks.every(isId)) errors.push(`${at}.fallbacks must be a list of model ids (may be empty)`);
    else if (new Set([c.model, ...c.fallbacks]).size !== c.fallbacks.length + 1) errors.push(`${at}: model and fallbacks must all differ`);
    if (!isPositive(c.maxCostUsd)) errors.push(`${at}.maxCostUsd must be a number above 0`);
    if (!Number.isInteger(c.timeoutMs) || c.timeoutMs < 1) errors.push(`${at}.timeoutMs must be a whole number of milliseconds above 0`);
    if (!allowPlaceholders) {
      for (const id of [c.model, ...(Array.isArray(c.fallbacks) ? c.fallbacks : [])]) {
        if (typeof id === "string" && PLACEHOLDER.test(id)) errors.push(`${at}: "${id}" is a placeholder; put a real model id`);
      }
    }
  }
  if (policy.defaultClass !== undefined && !Object.hasOwn(classes, policy.defaultClass)) {
    errors.push(`defaultClass "${policy.defaultClass}" is not one of the classes`);
  }
  return errors;
}

/** Read and validate a policy file. Throws with every problem listed; never returns a bad policy. */
export function loadPolicy(path, opts) {
  let policy;
  try {
    policy = JSON.parse(readFileSync(path, "utf8"));
  } catch (e) {
    throw new Error(`model policy ${path}: ${e.code === "ENOENT" ? "file not found" : "not valid JSON"}`);
  }
  const errors = validatePolicy(policy, opts);
  if (errors.length) throw new Error(`model policy ${path} is not usable:\n  - ${errors.join("\n  - ")}`);
  return policy;
}

/** health: { "<model id>": { ok: false, until?: <epoch ms> } }. Anything not listed is healthy. */
export function isHealthy(model, health, now) {
  const h = health?.[model];
  if (!h || h.ok !== false) return true;
  return typeof h.until === "number" && typeof now === "number" && now >= h.until;
}

/**
 * Which model does this task use, and why. Pure.
 *
 * @param task    a task class name, or { class, estimatedCostUsd? }
 * @param policy  a validated policy
 * @param health  see isHealthy; pass {} when nothing is known
 * @param opts    { now?: epoch ms } only used to expire `until` marks
 * @returns { taskClass, model, reason, fallbackIndex, maxCostUsd, timeoutMs }
 *   model is null when nothing may run. reason is one of:
 *   "primary" | "fallback:primary-unhealthy" | "default-class:<asked>" (prefix, then one of the first two)
 *   | "unknown-task-class" | "over-cost-ceiling" | "all-unhealthy"
 */
export function choose(task, policy, health = {}, { now } = {}) {
  const asked = typeof task === "string" ? task : task?.class;
  const estimate = typeof task === "object" && task ? task.estimatedCostUsd : undefined;
  let taskClass = asked;
  let prefix = "";
  if (!asked || !Object.hasOwn(policy.classes, asked)) {
    if (!policy.defaultClass) {
      return { taskClass: asked ?? null, model: null, reason: "unknown-task-class", fallbackIndex: null, maxCostUsd: null, timeoutMs: null };
    }
    taskClass = policy.defaultClass;
    prefix = `default-class:${asked ?? ""};`;
  }
  const c = policy.classes[taskClass];
  const base = { taskClass, maxCostUsd: c.maxCostUsd, timeoutMs: c.timeoutMs };
  if (typeof estimate === "number" && estimate > c.maxCostUsd) {
    return { ...base, model: null, reason: `${prefix}over-cost-ceiling`, fallbackIndex: null };
  }
  const candidates = [c.model, ...c.fallbacks];
  const i = candidates.findIndex((m) => isHealthy(m, health, now));
  if (i < 0) return { ...base, model: null, reason: `${prefix}all-unhealthy`, fallbackIndex: null };
  return { ...base, model: candidates[i], reason: prefix + (i === 0 ? "primary" : "fallback:primary-unhealthy"), fallbackIndex: i };
}

/** The same fact in the shape OpenClaw's model settings use, so config is generated, not retyped. */
export function modelSetting(policy, taskClass) {
  const c = policy.classes[taskClass];
  if (!c) throw new Error(`model policy has no class "${taskClass}"`);
  return { primary: c.model, fallbacks: [...c.fallbacks], timeoutMs: c.timeoutMs };
}
