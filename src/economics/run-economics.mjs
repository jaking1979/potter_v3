// T40 E0 — pure, provider-neutral economics primitives.
// Offline-only prototype. No model calls, secrets, external actions, or writes.
export class EconomicsInputError extends Error {
  constructor(message) {
    super(message);
    this.name = "EconomicsInputError";
  }
}

// In-process provenance guard only; this does not authenticate serialized records.
const pricedByThisModule = new WeakSet();
const callKeys = new Set([
  "run_id", "task_id", "parent_task_id", "attempt", "call_id", "occurred_at", "provider",
  "model", "usage", "reported_cost_usd"
]);
const usageKeys = new Set([
  "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"
]);
const rateKeys = new Set([
  "provider", "model", "effective_at", "expires_at", "input_per_million",
  "output_per_million", "cache_read_per_million", "cache_write_per_million"
]);

function object(x, label) {
  if (!x || typeof x !== "object" || Array.isArray(x)) {
    throw new EconomicsInputError(`${label} must be an object`);
  }
  return x;
}

function allowed(x, keys, label) {
  for (const key of Object.keys(x)) {
    if (!keys.has(key)) throw new EconomicsInputError(`unexpected ${label} field: ${key}`);
  }
}

function text(x, label) {
  if (typeof x !== "string" || !x.trim() || x.length > 180) {
    throw new EconomicsInputError(`${label} must be a short nonempty string`);
  }
  return x;
}

function instant(x, label) {
  if (typeof x !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(x) || Number.isNaN(Date.parse(x))) {
    throw new EconomicsInputError(label + " must be a timestamp with timezone");
  }
  return x;
}

function count(x, label) {
  if (!Number.isSafeInteger(x) || x < 0) {
    throw new EconomicsInputError(`${label} must be a nonnegative safe integer`);
  }
  return x;
}

function dollars(x, label) {
  if (typeof x !== "number" || !Number.isFinite(x) || x < 0) {
    throw new EconomicsInputError(`${label} must be a nonnegative finite number`);
  }
  return x;
}

function rounded(x) {
  return Math.round((x + Number.EPSILON) * 1e9) / 1e9;
}

export function validateCall(raw) {
  object(raw, "call");
  allowed(raw, callKeys, "call"); // rejects prompt/body/tool arguments by design
  const usage = object(raw.usage, "usage");
  allowed(usage, usageKeys, "usage");
  const counts = {};
  for (const key of usageKeys) {
    // Null means usage unavailable; zero must be reported explicitly.
    counts[key] = usage[key] == null ? null : count(usage[key], key);
  }
  if (counts.input_tokens != null &&
      counts.cache_read_tokens != null &&
      counts.cache_write_tokens != null &&
      counts.cache_read_tokens + counts.cache_write_tokens > counts.input_tokens) {
    throw new EconomicsInputError("cache tokens exceed total input tokens");
  }
  instant(raw.occurred_at, "occurred_at");
  return Object.freeze({
    run_id: text(raw.run_id, "run_id"),
    task_id: text(raw.task_id, "task_id"),
    parent_task_id: raw.parent_task_id == null ? null : text(raw.parent_task_id, "parent_task_id"),
    attempt: count(raw.attempt, "attempt"),
    call_id: text(raw.call_id, "call_id"),
    occurred_at: text(raw.occurred_at, "occurred_at"),
    provider: text(raw.provider, "provider"),
    model: text(raw.model, "model"),
    usage: Object.freeze(counts),
    reported_cost_usd: raw.reported_cost_usd == null ? null : dollars(raw.reported_cost_usd, "reported_cost_usd"),
  });
}

export function validateRate(raw) {
  object(raw, "rate");
  allowed(raw, rateKeys, "rate");
  const rate = {
    provider: text(raw.provider, "provider"),
    model: text(raw.model, "model"),
    effective_at: instant(raw.effective_at, "effective_at"),
    expires_at: raw.expires_at == null ? null : instant(raw.expires_at, "expires_at"),
  };
  if (Number.isNaN(Date.parse(rate.effective_at))) {
    throw new EconomicsInputError("rate effective_at must be a parseable date");
  }
  if (rate.expires_at != null && (Number.isNaN(Date.parse(rate.expires_at)) || Date.parse(rate.expires_at) <= Date.parse(rate.effective_at))) throw new EconomicsInputError("invalid expires_at");
  for (const field of [...rateKeys].filter(k => k.endsWith("_per_million"))) {
    rate[field] = raw[field] == null ? null : dollars(raw[field], field);
  }
  return Object.freeze(rate);
}

// A provider adapter must normalize INPUT to total input (including cached tokens).
// This calculation never assumes that absent cache counters are zero.
export function priceCall(raw, rawRate = null) {
  const call = validateCall(raw);
  const rate = rawRate == null ? null : validateRate(rawRate);
  if (rate && (rate.model !== call.model || rate.provider !== call.provider)) {
    throw new EconomicsInputError("rate does not match provider/model");
  }
  const u = call.usage;
  const fullCounts = Object.values(u).every(Number.isSafeInteger);
  const uncached = fullCounts ? u.input_tokens - u.cache_read_tokens - u.cache_write_tokens : null;
  let computed = null;
  let reason = null;
  if (!rate) reason = "missing_price";
  else if (Date.parse(call.occurred_at) < Date.parse(rate.effective_at)) reason = "rate_not_yet_effective";
  else if (rate.expires_at && Date.parse(call.occurred_at) >= Date.parse(rate.expires_at)) reason = "rate_expired";
  else if (!fullCounts) reason = "missing_usage";
  else if ((uncached > 0 && rate.input_per_million == null) ||
           (u.output_tokens > 0 && rate.output_per_million == null) ||
           (u.cache_read_tokens > 0 && rate.cache_read_per_million == null) ||
           (u.cache_write_tokens > 0 && rate.cache_write_per_million == null)) {
    reason = "missing_rate_component";
  } else {
    computed = rounded((
      uncached * (rate.input_per_million ?? 0) +
      u.output_tokens * (rate.output_per_million ?? 0) +
      u.cache_read_tokens * (rate.cache_read_per_million ?? 0) +
      u.cache_write_tokens * (rate.cache_write_per_million ?? 0)
    ) / 1_000_000);
  }
  const priced = Object.freeze({
    ...call,
    uncached_input_tokens: uncached,
    computed_cost_usd: computed,
    rate_effective_at: rate?.effective_at ?? null,
    unpriced_reason: reason,
    billed_discrepancy_usd: computed == null || call.reported_cost_usd == null
      ? null : rounded(call.reported_cost_usd - computed),
  });
  pricedByThisModule.add(priced);
  return priced;
}

function emptySummary() {
  return {calls: 0, priced_calls: 0, unpriced_calls: 0, known_cost_usd: 0,
    total_cost_usd: 0, known_reported_cost_usd: 0, reported_cost_usd: 0, unreported_calls: 0, input_tokens: 0, output_tokens: 0,
    cache_read_tokens: 0, cache_write_tokens: 0};
}

function add(summary, call) {
  summary.calls++;
  if (call.reported_cost_usd == null) summary.unreported_calls++;
  else summary.known_reported_cost_usd = rounded(summary.known_reported_cost_usd + call.reported_cost_usd);
  if (call.computed_cost_usd == null) summary.unpriced_calls++;
  else {
    summary.priced_calls++;
    summary.known_cost_usd = rounded(summary.known_cost_usd + call.computed_cost_usd);
  }
  for (const k of usageKeys) {
    summary[k] = summary[k] == null || call.usage[k] == null
      ? null : summary[k] + call.usage[k];
  }
  summary.total_cost_usd = summary.unpriced_calls ? null : summary.known_cost_usd;
  summary.reported_cost_usd = summary.unreported_calls ? null : summary.known_reported_cost_usd;
}

// Tasks define lineage; only model CALLS are billable. Aggregate snapshots cannot be inputs.
export function rollupTasks(tasks, pricedCalls) {
  if (!Array.isArray(tasks) || !Array.isArray(pricedCalls)) {
    throw new EconomicsInputError("tasks and pricedCalls must be arrays");
  }
  const parent = new Map();
  for (const t of tasks) {
    object(t, "task");
    const id = text(t.task_id, "task_id");
    if (parent.has(id)) throw new EconomicsInputError("duplicate task_id");
    parent.set(id, t.parent_task_id == null ? null : text(t.parent_task_id, "parent_task_id"));
  }
  for (const [id, p] of parent) {
    const seen = new Set([id]);
    let cur = p;
    while (cur != null) {
      if (!parent.has(cur)) throw new EconomicsInputError("missing parent task");
      if (seen.has(cur)) throw new EconomicsInputError("task parent cycle");
      seen.add(cur);
      cur = parent.get(cur);
    }
  }
  const totals = new Map([...parent.keys()].map(k => [k, emptySummary()]));
  const identities = new Map();
  for (const call of pricedCalls) {
    if (!pricedByThisModule.has(call)) throw new EconomicsInputError("unverified priced call");
    if (!parent.has(call.task_id)) throw new EconomicsInputError("call references missing task");
    if (call.parent_task_id !== parent.get(call.task_id)) throw new EconomicsInputError("call parent does not match task lineage");
    const id = `${call.run_id}\u0000${call.task_id}\u0000${call.attempt}\u0000${call.call_id}`;
    const snapshot = JSON.stringify(call);
    if (identities.has(id)) {
      if (identities.get(id) !== snapshot) throw new EconomicsInputError("conflicting replay for call identity");
      continue; // identical delivery/replay, not another billable call
    }
    identities.set(id, snapshot);
    let task = call.task_id;
    while (task != null) {
      add(totals.get(task), call);
      task = parent.get(task);
    }
  }
  return Object.fromEntries(totals);
}

// Caller supplies explicit scenarios; no invented task priors or cache-hit assumptions.
// Each scenario is a sequence of hypothetical NORMALIZED calls.
export function estimatePreflight({scenarios, rateByModel}) {
  object(scenarios, "scenarios");
  object(rateByModel, "rateByModel");
  const result = {};
  for (const label of ["low", "likely", "high"]) {
    const calls = scenarios[label];
    if (!Array.isArray(calls) || !calls.length) {
      throw new EconomicsInputError(`scenario ${label} needs calls`);
    }
    const priced = calls.map(c => priceCall(c, rateByModel[`${c.provider}/${c.model}`] ?? null));
    const known = rounded(priced.reduce((sum, c) => sum + (c.computed_cost_usd ?? 0), 0));
    const missing = priced.filter(c => c.computed_cost_usd == null).map(c => c.unpriced_reason);
    result[label] = {known_cost_usd: known,
      total_cost_usd: missing.length ? null : known,
      missing_reasons: [...new Set(missing)],
      model_calls: priced.length};
  }
  if (result.low.total_cost_usd != null && result.likely.total_cost_usd != null &&
      result.high.total_cost_usd != null &&
      !(result.low.total_cost_usd <= result.likely.total_cost_usd &&
        result.likely.total_cost_usd <= result.high.total_cost_usd)) {
    throw new EconomicsInputError("scenario costs must be ordered low <= likely <= high");
  }
  return {status: Object.values(result).every(s => s.total_cost_usd != null) ? "priced" : "partial_or_unknown",
    provenance: "caller_supplied_heuristic_scenarios_not_a_quote",
    scenarios: result};
}
