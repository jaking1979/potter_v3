// Potter v3.5 durability (ratified T10, T4/T17): an action that changes the outside world happens once.
//  - effectKey: a fingerprint of the exact call (tool + arguments), never the arguments themselves.
//  - alreadyDone: the same consequential call already succeeded recently -> the owner is asked before repeating it.
//  - inDoubt: calls that started but have no recorded result (a crash or restart in between) -> listed for checking.
//  - Notion outbox: a Notion write that failed is kept (root-only file) and retried by the outbox job; a retry of an
//    outbox item is the same decision, so it is not asked again.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

function stable(v) {
  if (Array.isArray(v)) return `[${v.map(stable).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + stable(v[k])).join(",")}}`;
  return JSON.stringify(v ?? null);
}
export function effectKey(toolName, params) {
  return createHash("sha256").update(String(toolName) + "\n" + stable(params)).digest("hex").slice(0, 16);
}

function lines(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8").split("\n").filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
}

/** The receipt of the same consequential call that succeeded within `days`, or null. */
export function alreadyDone(receiptsPath, key, { days = 7, now = new Date() } = {}) {
  const since = new Date(now.getTime() - days * 86400000).toISOString();
  const recs = lines(receiptsPath);
  const okIntents = new Set(recs.filter((r) => r.phase === "result" && r.ok === true).map((r) => r.intent));
  return recs.find((r) => r.phase === "intent" && r.effect === key && r.ts >= since && r.decision === "allow" && okIntents.has(r.hash)) ?? null;
}

/** Consequential intents with no result after `minutes`. */
export function inDoubt(receiptsPath, { minutes = 10, now = new Date() } = {}) {
  const recs = lines(receiptsPath);
  const finished = new Set(recs.filter((r) => r.phase === "result").map((r) => r.intent));
  const before = new Date(now.getTime() - minutes * 60000).toISOString();
  return recs.filter((r) => r.phase === "intent" && r.risk === "consequential" && r.decision === "allow" && r.ts < before && !finished.has(r.hash));
}

// ---- Notion outbox ---------------------------------------------------------------------------------------------
function loadBox(path) {
  try { return JSON.parse(readFileSync(path, "utf8")); } catch { return { items: [] }; }
}
function saveBox(path, box) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const tmp = `${path}.tmp${process.pid}`;
  writeFileSync(tmp, JSON.stringify(box, null, 1), { mode: 0o600 });
  renameSync(tmp, path);
}
export function enqueue(path, { tool, params, error }, now = new Date()) {
  const box = loadBox(path);
  const key = effectKey(tool, params);
  const existing = box.items.find((i) => i.key === key && i.status === "pending");
  if (existing) { existing.attempts += 1; existing.last_error = String(error ?? "").slice(0, 200); existing.updated = now.toISOString(); }
  else box.items.push({ key, tool, params, status: "pending", attempts: 1, last_error: String(error ?? "").slice(0, 200), queued: now.toISOString(), updated: now.toISOString() });
  saveBox(path, box);
  return key;
}
export function outboxPending(path) {
  return loadBox(path).items.filter((i) => i.status === "pending");
}
export function isOutboxRetry(path, key) {
  return outboxPending(path).some((i) => i.key === key);
}
export function settle(path, key, ok, now = new Date()) {
  const box = loadBox(path);
  const i = box.items.find((x) => x.key === key && x.status === "pending");
  if (!i) return null;
  if (ok) i.status = "done"; else i.attempts += 1;
  if (!ok && i.attempts >= 6) i.status = "gave_up";
  i.updated = now.toISOString();
  saveBox(path, box);
  return i;
}
