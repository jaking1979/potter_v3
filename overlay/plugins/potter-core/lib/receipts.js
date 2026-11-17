// Potter v3.1 receipts: one JSON line per consequential or reversible action, before and after, hash-chained so a
// removed or edited line is detectable. Never holds tool arguments: only the capability, a short target, the
// decision and the outcome. Keys and tokens are masked if a target happens to contain one.
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";

const SECRETISH = /(sk-[A-Za-z0-9_-]{8,}|\d{5,12}:[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9._-]{20,}|tskey-[A-Za-z0-9-]+|Bearer\s+\S+)/g;

export function mask(s) {
  return String(s ?? "").replace(SECRETISH, "(secret)").slice(0, 120);
}

function lastHash(path) {
  if (!existsSync(path)) return "0".repeat(16);
  const lines = readFileSync(path, "utf8").trimEnd().split("\n");
  try {
    return JSON.parse(lines[lines.length - 1]).hash ?? "0".repeat(16);
  } catch {
    return "broken";
  }
}

export function append(path, entry, now = new Date()) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const prev = lastHash(path);
  const rec = { ts: now.toISOString(), ...entry, target: mask(entry.target), prev };
  rec.hash = createHash("sha256").update(JSON.stringify(rec)).digest("hex").slice(0, 16);
  appendFileSync(path, JSON.stringify(rec) + "\n", { mode: 0o600 });
  return rec;
}

/** Recompute the chain. Returns { ok, count, brokenAt }. */
export function verify(path) {
  if (!existsSync(path)) return { ok: true, count: 0, brokenAt: null };
  const lines = readFileSync(path, "utf8").split("\n").filter(Boolean);
  let prev = "0".repeat(16);
  for (let i = 0; i < lines.length; i++) {
    let rec;
    try {
      rec = JSON.parse(lines[i]);
    } catch {
      return { ok: false, count: lines.length, brokenAt: i + 1 };
    }
    const { hash, ...rest } = rec;
    const again = createHash("sha256").update(JSON.stringify(rest)).digest("hex").slice(0, 16);
    if (rec.prev !== prev || hash !== again) return { ok: false, count: lines.length, brokenAt: i + 1 };
    prev = hash;
  }
  return { ok: true, count: lines.length, brokenAt: null };
}

export function recent(path, n = 10, filter = () => true) {
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8").split("\n").filter(Boolean).map((l) => {
    try { return JSON.parse(l); } catch { return null; }
  }).filter((r) => r && filter(r)).slice(-n);
}
