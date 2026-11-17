// Potter v3.2 fast path: a recognised house request is answered by Merrick's deterministic router and verified
// Home Assistant calls, without the main model (System One). Anything Merrick does not recognise goes on to the
// normal turn. "confirm departure" is the owner's explicit go-ahead for locking and arming, so it runs only when the
// host proves the sender is the owner.
import { execFile } from "node:child_process";

const CONFIRM = /^\s*confirm departure\s*[.!]?\s*$/i;

function run(cmd, args, timeoutMs) {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: timeoutMs, maxBuffer: 1 << 20 }, (err, stdout) => {
      if (err && !stdout) return resolve(null);
      try {
        resolve(JSON.parse(stdout));
      } catch {
        resolve(null);
      }
    });
  });
}

/**
 * @returns null (not handled: the normal turn proceeds) or { text, outcome, intent, ms }
 */
export async function tryHouse(text, { isOwner, merrick = "merrick", timeoutMs = 8000, runner = run } = {}) {
  const t = String(text ?? "").trim();
  if (!t) return null;
  if (CONFIRM.test(t)) {
    if (isOwner !== true) return null;                          // let the main path ask who this is
    const out = await runner(merrick, ["confirm-departure"], timeoutMs);
    return out && typeof out.reply === "string" ? { text: out.reply, outcome: out.outcome ?? "unknown", intent: "confirm_departure" } : null;
  }
  const out = await runner(merrick, ["ask", t], timeoutMs);
  if (!out || out.path !== "fast" || typeof out.reply !== "string" || !out.reply.trim()) return null;
  return { text: out.reply, outcome: out.outcome, intent: out.intent, ms: out.ms };
}
