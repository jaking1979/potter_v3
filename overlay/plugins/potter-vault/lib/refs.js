// Vault references: the model writes {{vault:<item>#<field>}}; code puts the real value into the tool call at the
// last moment and scrubs it from anything that comes back. The value never enters the model's context.
// Pure: no I/O.

export const REF = /\{\{vault:([^#{}]{1,120})#([a-z_]{2,30})\}\}/g;
export const CARD_FIELDS = new Set(["number", "code", "expiry", "exp_month", "exp_year", "cardholder"]);
export const SECRET_FIELDS = new Set(["password", "totp", "number", "code"]);

/** Every reference in a params object (any depth). */
export function findRefs(value, out = []) {
  if (typeof value === "string") for (const m of value.matchAll(REF)) out.push({ item: m[1].trim(), field: m[2], raw: m[0] });
  else if (Array.isArray(value)) value.forEach((v) => findRefs(v, out));
  else if (value && typeof value === "object") Object.values(value).forEach((v) => findRefs(v, out));
  return out;
}

/** A copy of params with each reference replaced by resolve(item, field). */
export function substitute(value, resolve) {
  if (typeof value === "string") return value.replace(REF, (_, item, field) => resolve(item.trim(), field));
  if (Array.isArray(value)) return value.map((v) => substitute(v, resolve));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, substitute(v, resolve)]));
  return value;
}

/** Replace every known secret value in any text or structure with its reference. Short values (< 4) are left alone. */
export function scrub(value, secrets) {
  const list = [...secrets].filter(([v]) => typeof v === "string" && v.length >= 4).sort((a, b) => b[0].length - a[0].length);
  const fix = (s) => list.reduce((acc, [v, label]) => acc.split(v).join(label), s);
  const walk = (x) => (typeof x === "string" ? fix(x) : Array.isArray(x) ? x.map(walk) : x && typeof x === "object"
    ? Object.fromEntries(Object.entries(x).map(([k, v]) => [k, walk(v)])) : x);
  return walk(value);
}

/** Only tools that put a value into a form or a request header may receive one; never shells, messages or files. */
export function mayReceive(toolName, allow = /^(browser|web_?form|playwright|computer|http_request|fetch_with_auth)/i) {
  const n = String(toolName ?? "");
  if (/^(exec|bash|shell|process|message|write|edit|apply_patch|memory|cron|sessions?_|notion|agentmail|composio|kg_|vault_)/i.test(n)) return false;
  return allow.test(n);
}

/** OpenClaw's wrapper for deferred tools (`dispatch_action {id, args}`). The harness runs the same before_tool_call
 *  hooks again on the inner call, under the inner tool's own name, so the vault leaves the wrapper alone and fills the
 *  value at the inner call: there mayReceive applies, and the browser guard still sees the reference. */
export const isWrapper = (toolName) => /^(dispatch_action|tool_call)$/i.test(String(toolName ?? ""));

export const isCardRef = (r) => CARD_FIELDS.has(r.field);
