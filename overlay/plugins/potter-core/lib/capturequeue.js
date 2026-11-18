// The graph's write path starts here: each message the OWNER writes to Potter is queued (root-only file) for
// the potter-kg capture job. Scheduled jobs, heartbeats and other people's messages are never queued.
import { appendFileSync, mkdirSync, statSync } from "node:fs";
import { dirname } from "node:path";

const JOB_TRIGGERS = /^(cron|heartbeat|schedule|job|system|memory|compaction)/i;

export function isOwnerTurn(ctx = {}, ownerIds = []) {
  if (ctx.jobId || JOB_TRIGGERS.test(String(ctx.trigger ?? ""))) return false;
  if (!ctx.senderId) return false;                                   // unknown sender: never assume it is the owner
  const s = String(ctx.senderId);
  return ownerIds.some((id) => id && (s === id || s.endsWith(`:${id}`)));
}

export function shouldQueue(text, ctx, ownerIds) {
  const t = String(text ?? "").trim();
  if (t.length < 12 || /^\//.test(t)) return false;                   // slash commands, "ok", "thanks"
  if (/^run the [a-z0-9-]+ skill\b/i.test(t)) return false;            // a job's own instruction
  return isOwnerTurn(ctx, ownerIds);
}

export function enqueueMessage(path, text, ctx, { now = new Date(), maxBytes = 5_000_000 } = {}) {
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    try { if (statSync(path).size > maxBytes) return false; } catch { /* new file */ }
    appendFileSync(path, JSON.stringify({ ts: now.toISOString(), text: String(text).slice(0, 4000), session: ctx.sessionKey ?? null, channel: ctx.channel ?? null }) + "\n", { mode: 0o600 });
    return true;
  } catch { return false; }
}
