// The vault's rules, independent of the harness: what may be used, when, by which tool, and what the model may see.
//   vault_list            names, kinds, which fields exist; never a value
//   vault_authorize       the owner approves using one item for a while (cards: always asked; logins: asked unless
//                         the item's notes say potter:auto)
//   {{vault:item#field}}  substituted into an allowed tool's parameters at the last moment, for authorized items only
//   vault_send_to_owner   the value goes straight to the owner's chat; the model only learns "sent"
// Every value that passes through is scrubbed from tool results and the model's own messages.
import { describe, fieldValue } from "./bw.js";
import { findRefs, isCardRef, mayReceive, scrub, substitute } from "./refs.js";

export function createVault({ backend, audit = () => {}, send, now = () => Date.now(), minutes = 15 }) {
  const authorized = new Map();          // item name (lower case) -> expiry ms
  const seen = new Map();                // secret value -> "{{vault:item#field}}" (for scrubbing), most recent 200
  const key = (s) => String(s).trim().toLowerCase();
  const remember = (v, label) => { if (typeof v === "string" && v) { seen.delete(v); seen.set(v, label); while (seen.size > 200) seen.delete(seen.keys().next().value); } };

  async function find(name) {
    const r = await backend.items();
    if (!r.ok) return { error: `vault unavailable (${r.reason})` };
    const hits = r.items.filter((it) => key(it.name) === key(name));
    if (hits.length !== 1) return { error: hits.length ? `more than one item is named "${name}"` : `no item named "${name}" in Potter's vault` };
    return { item: hits[0] };
  }

  return {
    async list() {
      const r = await backend.items();
      if (!r.ok) return { ok: false, error: `vault unavailable (${r.reason})` };
      return { ok: true, items: r.items.map(describe) };
    },

    /** Does using this item need the owner's approval right now? */
    async needsApproval(name) {
      const f = await find(name);
      if (f.error) return { error: f.error };
      const d = describe(f.item);
      return { approval: !(d.auto && d.kind === "login"), describe: d };
    },

    async authorize(name, purpose) {
      const f = await find(name);
      if (f.error) return { ok: false, error: f.error };
      authorized.set(key(f.item.name), now() + minutes * 60000);
      audit({ action: "authorize", item: f.item.name, purpose: String(purpose ?? "").slice(0, 120) });
      return { ok: true, item: f.item.name, minutes,
        use: `Write {{vault:${f.item.name}#<field>}} in a browser form field; fields: ${describe(f.item).fields.join(", ")}` };
    },

    /** before_tool_call: substitute references, or block. Returns undefined (nothing to do), {block, reason} or {params}. */
    async prepare(toolName, params, toolCallId) {
      const refs = findRefs(params);
      if (!refs.length) return undefined;
      if (!mayReceive(toolName)) {
        audit({ action: "refused", tool: toolName, items: [...new Set(refs.map((r) => r.item))] });
        return { block: true, reason: "vault values can only be typed into a browser form or a request, never into a shell, a message or a file" };
      }
      const values = new Map();
      for (const r of refs) {
        const exp = authorized.get(key(r.item));
        if (!exp || exp < now()) return { block: true, reason: `"${r.item}" is not authorized right now: call vault_authorize first${isCardRef(r) ? " (a card always asks the owner)" : ""}` };
        const f = await find(r.item);
        if (f.error) return { block: true, reason: f.error };
        const v = await fieldValue(backend, f.item, r.field);
        if (v === null || v === undefined || v === "") return { block: true, reason: `"${r.item}" has no ${r.field}` };
        values.set(`${key(r.item)}#${r.field}`, String(v));
        remember(String(v), r.raw);
      }
      audit({ action: "fill", tool: toolName, call: toolCallId ?? null, refs: refs.map((r) => `${r.item}#${r.field}`) });
      return { params: substitute(params, (item, field) => values.get(`${key(item)}#${field}`)) };
    },

    async sendToOwner(name, field) {
      const f = await find(name);
      if (f.error) return { ok: false, error: f.error };
      const exp = authorized.get(key(f.item.name));
      if (!exp || exp < now()) return { ok: false, error: `"${f.item.name}" is not authorized right now: call vault_authorize first` };
      const v = await fieldValue(backend, f.item, field);
      if (!v) return { ok: false, error: `"${f.item.name}" has no ${field}` };
      remember(String(v), `{{vault:${f.item.name}#${field}}}`);
      const sent = await send(`${f.item.name} ${field}: ${v}\n(From Potter's vault. Delete this message when you are done.)`);
      audit({ action: "send_to_owner", item: f.item.name, field, ok: Boolean(sent) });
      return sent ? { ok: true, sent: `${field} of "${f.item.name}" sent to your chat` } : { ok: false, error: "could not send" };
    },

    /** Anything going back toward the model: every value it has handled, replaced by its reference. */
    scrub(x) { return seen.size ? scrub(x, seen) : x; },
    _authorized: authorized,
  };
}
