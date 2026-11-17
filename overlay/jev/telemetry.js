// Telemetry: one JSON line per decision. Task class, chosen model, reason, latency and outcome only.
// No state, no questions, no answers, no error text: nothing a reader could reconstruct content from.
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const FIELDS = ["ts", "taskClass", "model", "reason", "latencyMs", "ok", "attempts", "failure", "costUsd", "overCeiling"];

/** Keep only the known fields, with the types they must have; anything else is dropped. */
export function decisionRecord(input) {
  const str = (v) => (typeof v === "string" ? v.slice(0, 200) : null);
  const n = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const full = {
    ts: str(input.ts) ?? new Date().toISOString(),
    taskClass: str(input.taskClass),
    model: str(input.model),
    reason: str(input.reason),
    latencyMs: n(input.latencyMs),
    ok: input.ok === true,
    attempts: n(input.attempts) ?? 0,
    failure: str(input.failure),
    costUsd: n(input.costUsd),
    overCeiling: input.overCeiling === true,
  };
  return Object.fromEntries(FIELDS.map((k) => [k, full[k]]));
}

/** `write(line)` receives one JSON line (with the newline). A failing sink never breaks a decision. */
export function createTelemetry(write) {
  return {
    record(input) {
      const rec = decisionRecord(input);
      try { write(JSON.stringify(rec) + "\n"); } catch { /* telemetry must not take the caller down */ }
      return rec;
    },
  };
}

export function fileSink(path) {
  let ready = false;
  return (line) => {
    if (!ready) { mkdirSync(dirname(path), { recursive: true }); ready = true; }
    appendFileSync(path, line, "utf8");
  };
}
