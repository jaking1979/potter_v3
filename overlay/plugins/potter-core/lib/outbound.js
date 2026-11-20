// v3.14 Outgoing-message filter: before anything leaves for someone other than the owner (a message, an email, a post,
// text typed into a website), check it for things that must not leak.
//   code  card numbers (Luhn-valid 13-19 digits) and US Social Security numbers: exact, cheap, no false alarms on
//         ordinary numbers
//   Jev   one request, one narrow yes/no each: a government ID number, a bank account or routing number, a password or
//         one-time code, someone's health or clinical information
// A finding never blocks silently: on an owner-started turn the owner approves or not, seeing WHAT KIND of thing was
// found (never the value); on a turn started by something untrusted (an email, a web page) it is blocked.
// Vault references ({{vault:...}}) are exempt: those were approved for exactly that use.

export function luhn(digits) {
  let sum = 0, alt = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let d = digits.charCodeAt(i) - 48;
    if (alt) { d *= 2; if (d > 9) d -= 9; }
    sum += d; alt = !alt;
  }
  return sum % 10 === 0;
}

/** Deterministic findings: kinds only, never the values. */
export function detect(text) {
  const t = String(text ?? "").replace(/\{\{vault:[^}]*\}\}/g, " ");
  const kinds = new Set();
  for (const m of t.matchAll(/\b(?:\d[ -]?){12,18}\d\b/g)) {
    const d = m[0].replace(/\D/g, "");
    if (d.length >= 13 && d.length <= 19 && luhn(d) && !/^(\d)\1+$/.test(d)) kinds.add("card number");
  }
  if (/\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b/.test(t)) kinds.add("Social Security number");
  return [...kinds];
}

export const JEV_KINDS = {
  gov_id: { label: "government ID number", q: "Does `text` contain a government ID number (passport, driver's license, national ID, tax ID)?" },
  bank: { label: "bank account or routing number", q: "Does `text` contain a bank account, routing or IBAN number?" },
  secret: { label: "password or one-time code", q: "Does `text` contain a password, PIN, security answer, API key or one-time login code?" },
  health: { label: "health or clinical information", q: "Does `text` reveal a named or identifiable person's medical, mental-health, diagnosis, medication or treatment information?" },
};

export function jevRequest(text) {
  const questions = {};
  for (const [k, v] of Object.entries(JEV_KINDS)) {
    questions[k] = { type: "noul", instructions: { question: v.q, focus: "Only what is actually in `text`. A mention of a topic is not the information itself." } };
  }
  return { state: { text: String(text).replace(/\{\{vault:[^}]*\}\}/g, "[vault value]").slice(0, 8000) }, questions };
}

const yes = (a) => (typeof a?.noul === "number" ? a.noul : typeof a?.probabilityTrue === "number" ? a.probabilityTrue : null);

export function readJev(answers = {}, at = 0.7) {
  return Object.entries(JEV_KINDS).filter(([k]) => (yes(answers[k]) ?? 0) >= at).map(([, v]) => v.label);
}

/** All the text an outgoing call would carry (recipient fields excluded). */
export function outgoingText(params) {
  const out = [];
  const skip = /^(to|target|chatId|recipient|channel|action|accountId|inboxId|threadId|messageId|targetId|ref|kind|url|profile)$/i;
  const walk = (v, k) => {
    if (typeof v === "string") { if (!skip.test(k ?? "")) out.push(v); }
    else if (Array.isArray(v)) v.forEach((x) => walk(x, k));
    else if (v && typeof v === "object") for (const [kk, vv] of Object.entries(v)) walk(vv, kk);
  };
  walk(params);
  return out.join("\n").slice(0, 20000);
}

/** Is this call sending something out to someone? (Reading, drafting and the owner's own chat are not.) */
export function isOutgoing(toolName, params = {}, cls = {}) {
  const n = String(toolName ?? "");
  if (n === "message") return cls.risk === "consequential";                 // messages to the owner are not "out"
  if (/agentmail.*(send|reply|forward)/i.test(n)) return true;
  if (/composio/i.test(n)) {
    const slugs = JSON.stringify(params).match(/[A-Z][A-Z0-9]+_[A-Z0-9_]+/g) ?? [];
    return slugs.some((s) => /_(SEND|REPLY|FORWARD|POST|PUBLISH|SHARE|INVITE)(_|$)/.test(s) && !/DRAFT/.test(s));
  }
  if (n === "browser") return params.action === "act" && /^(type|fill)$/.test(String(params.request?.kind ?? params.kind ?? ""));
  return false;
}

/** The whole check. decide is optional (Jev); without it only the deterministic findings count. */
export async function check(text, { decide, timeoutMs = 2500 } = {}) {
  const found = detect(text);
  let jev = [], error = null;
  if (decide && String(text).trim().length >= 8) {
    try {
      const out = await Promise.race([decide({ purpose: "potter-outbound", ...jevRequest(text) }),
        new Promise((r) => setTimeout(() => r({ ok: false, reason: "timeout" }), timeoutMs))]);
      if (out?.ok) jev = readJev(out.answers); else error = out?.reason ?? "no answer";
    } catch (e) { error = e?.name ?? "error"; }
  }
  return { kinds: [...new Set([...found, ...jev])], error };
}
