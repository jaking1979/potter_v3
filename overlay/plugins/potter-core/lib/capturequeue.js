// The graph's write path starts here: each message the OWNER writes to Potter is queued (root-only file) for
// the potter-kg capture job. Scheduled jobs, heartbeats and other people's messages are never queued.
import { appendFileSync, mkdirSync, statSync } from "node:fs";
import { dirname } from "node:path";

const JOB_TRIGGERS = /^(cron|heartbeat|schedule|job|system|memory|compaction)/i;

// Channels where only the owner can start a conversation (Potter's Telegram and iMessage allowlists hold only
// the owner's ids), so a channel-started, non-job turn there is the owner's even when no sender id is passed.
const OWNER_ONLY_CHANNELS = /^(telegram|imessage)$/i;

export function senderOf(ctx = {}) {
  return ctx.senderId ?? ctx.senderExternalId ?? ctx.channelContext?.sender?.id ?? ctx.requester?.senderId ?? null;
}

export function isOwnerTurn(ctx = {}, ownerIds = []) {
  if (ctx.jobId || ctx.inputProvenance?.jobId || JOB_TRIGGERS.test(String(ctx.trigger ?? ""))) return false;
  if (ctx.inputProvenance?.kind && ctx.inputProvenance.kind !== "external_user") return false;   // subagents, system
  if (ctx.requester?.senderIsOwner === true || ctx.senderIsOwner === true) return true;
  const match = (v) => v != null && ownerIds.some((id) => id && (String(v) === id || String(v).endsWith(`:${id}`)));
  const sender = senderOf(ctx);
  if (sender != null) return match(sender);                          // a known sender must be the owner
  const channel = String(ctx.channel ?? ctx.messageProvider ?? ctx.channelId ?? "");
  if (OWNER_ONLY_CHANNELS.test(channel)) return true;                // allowlisted to the owner only
  return match(ctx.chatId ?? ctx.channelContext?.chat?.id);          // a Telegram DM's chat id is the owner's id
}

/** Which identity fields this harness passed (names only), to diagnose owner detection without logging ids. */
export function ctxFields(ctx = {}) {
  return ["senderId", "senderExternalId", "channel", "messageProvider", "channelId", "chatId", "jobId", "trigger"]
    .filter((k) => ctx[k] != null).concat(ctx.channelContext?.sender?.id != null ? ["channelContext.sender.id"] : [],
      ctx.requester ? ["requester"] : [], ctx.inputProvenance?.kind ? [`provenance:${ctx.inputProvenance.kind}`] : []);
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
