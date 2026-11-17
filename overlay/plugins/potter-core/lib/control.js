// Potter v3.1 Control: may this call run now, and does the owner need to tap Approve? Pure.
//
//                     owner asked directly      scheduled job              anyone else / unknown
//   read              allow                     allow                      allow
//   reversible        allow + receipt           allow + receipt            allow + receipt (marked untrusted)
//   consequential     allow + receipt           allow if a standing grant  approval
//                     (high severity: approval) covers it, else approval
//   forbidden         block                     block                      block
//
// mode "shadow" records what it would have done and never blocks or asks.

export function trustOf(ctx = {}) {
  const r = ctx.requester;
  if (r && r.senderIsOwner === true) return "owner";
  if (ctx.trigger === "cron") return "schedule";
  if (r && r.senderIsOwner === false) return "other";
  return "unknown";
}

export function grantCovers(grants, capability) {
  return (grants ?? []).some((g) => {
    if (typeof g !== "string" || !g) return false;
    if (g.endsWith("*")) return capability.startsWith(g.slice(0, -1));
    return capability === g;
  });
}

/**
 * @returns { action: "allow"|"approve"|"block", reason, receipt: boolean }
 */
export function decide(cls, trust, cfg = {}) {
  const { risk, severity, capability } = cls;
  let out;
  if (risk === "forbidden") out = { action: "block", reason: "forbidden: locks, alarms and water valves are never operated by a tool call" };
  else if (risk === "read") out = { action: "allow", reason: "read", receipt: false };
  // Receipts are for actions on the outside world (Decision Log 17); Potter's own reversible housekeeping is not
  // receipted when the owner or a scheduled job started it, only when its origin is not proven.
  else if (risk === "reversible") out = trust === "owner" || trust === "schedule"
    ? { action: "allow", reason: "reversible", receipt: false }
    : { action: "allow", reason: "reversible, untrusted origin" };
  else if (trust === "owner") out = severity === "high" ? { action: "approve", reason: "high-stakes action: the owner taps Approve" } : { action: "allow", reason: "the owner asked directly" };
  else if (trust === "schedule" && severity !== "high" && grantCovers(cfg.standingGrants, capability)) out = { action: "allow", reason: "standing grant" };
  else out = { action: "approve", reason: trust === "schedule" ? "scheduled job without a standing grant" : "not started by the owner" };
  if (out.receipt === undefined) out.receipt = true;
  if (cfg.mode === "shadow" && out.action !== "allow") return { ...out, would: out.action, action: "allow" };
  return out;
}
