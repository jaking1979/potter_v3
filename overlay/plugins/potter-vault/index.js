// potter-vault: Potter uses the owner's chosen secrets without seeing them. OpenClaw wiring only; the rules are in
// lib/core.js (harness-agnostic) and the backend in lib/bw.js (Bitwarden CLI against a self-hosted Vaultwarden).
import { execFile } from "node:child_process";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import { createBackend } from "./lib/bw.js";
import { createVault } from "./lib/core.js";

function envFile(path) {
  const out = {};
  try {
    for (const line of readFileSync(path, "utf8").split("\n")) {
      const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)$/);
      if (m) out[m[1]] = m[2];
    }
  } catch { /* not set up */ }
  return out;
}

const text = (v) => ({ content: [{ type: "text", text: typeof v === "string" ? v : JSON.stringify(v, null, 2) }], details: v });
const obj = (properties, required = []) => ({ type: "object", additionalProperties: false, properties, required });

export default definePluginEntry({
  id: "potter-vault",
  name: "Potter Vault",
  description: "Uses the owner's chosen logins and cards without the model ever seeing a value.",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    const dir = cfg.stateDir || join(resolveStateDir(), "potter-vault");
    const auditPath = join(dir, "audit.jsonl");
    const audit = (rec) => {
      try { mkdirSync(dir, { recursive: true, mode: 0o700 }); appendFileSync(auditPath, JSON.stringify({ ts: new Date().toISOString(), ...rec }) + "\n", { mode: 0o600 }); }
      catch { /* audit never blocks */ }
    };
    // The key file is read on first use (and again until it exists), so finishing setup needs no gateway restart.
    let real = null;
    const backend = {
      get() {
        if (real) return real;
        const env = envFile(cfg.envFile || "/etc/potter/secrets/vault.env");
        if (!(env.BW_CLIENTID && env.BW_CLIENTSECRET && env.BW_PASSWORD)) return null;
        real = createBackend({ bin: cfg.bwPath || "bw", env: { ...env, BITWARDENCLI_APPDATA_DIR: cfg.appDataDir || join(dir, "bw") } });
        return real;
      },
      items: async (fresh) => backend.get()?.items(fresh) ?? { ok: false, reason: "not set up yet: the owner runs the vault setup" },
      totp: async (id) => backend.get()?.totp(id) ?? null,
    };
    const send = (message) => new Promise((res) => {
      if (!cfg.ownerTarget) return res(false);
      execFile(cfg.openclawBin || "openclaw", ["message", "send", "--channel", "telegram", "--target", String(cfg.ownerTarget), "--message", message],
        { timeout: 20000 }, (err) => res(!err));
    });
    const vault = createVault({ backend, audit, send, minutes: cfg.authorizeMinutes ?? 15 });

    api.registerTool({ name: "vault_list",
      description: "What is in Potter's vault: item names, kind (login/card/note), which fields exist, the site. Never a value.",
      parameters: obj({}), async execute() { return text(await vault.list()); } });
    api.registerTool({ name: "vault_authorize",
      description: "Ask to use one vault item for the next few minutes (the owner approves cards always, logins unless marked potter:auto). Then write {{vault:<item name>#<field>}} in a browser form field; the real value is filled in for you and you never see it.",
      parameters: obj({ item: { type: "string" }, purpose: { type: "string", description: "Why, in a few plain words (recorded; no secrets)." },
        fields: { type: "array", items: { type: "string" }, description: "Sensitive fields this use needs (ssn, passport_number, license_number, dob...): each is approved for one use only." } }, ["item", "purpose"]),
      async execute(_id, p) { return text(await vault.authorize(p.item, p.purpose, p.fields)); } });
    api.registerTool({ name: "vault_send_to_owner",
      description: "Send one field of an authorized vault item straight to the owner's chat (for example a passport number they asked for). You only learn that it was sent.",
      parameters: obj({ item: { type: "string" }, field: { type: "string" } }, ["item", "field"]),
      async execute(_id, p) { return text(await vault.sendToOwner(p.item, p.field)); } });

    // Approval for authorize and send-to-owner; substitution for everything else. Runs after potter-core's control.
    api.on("before_tool_call", async (event) => {
      const name = event?.toolName;
      if (name === "vault_authorize" || name === "vault_send_to_owner") {
        const n = await vault.needsApproval(event.params?.item ?? "", name === "vault_send_to_owner" ? [event.params?.field] : event.params?.fields);
        if (n.error) return { block: true, blockReason: n.error };
        if (name === "vault_send_to_owner" && !n.sensitive.length && vault._authorized.has(String(event.params.item).trim().toLowerCase())) return undefined;
        if (!n.approval) return undefined;
        return { requireApproval: {
          title: `Use from the vault: ${n.describe.name}`,
          description: `${n.describe.kind === "card" ? "A payment card" : n.describe.kind === "identity" ? "An identity" : "A login"}${n.describe.site ? " for " + n.describe.site : ""}. ` +
            (n.sensitive.length ? `Including, for one use: ${n.sensitive.join(", ")}. ` : "") +
            `Why: ${String(event.params?.purpose ?? "send it to you").slice(0, 120)}. Potter will not see the value.`,
          severity: n.describe.kind === "card" || n.sensitive.length ? "critical" : "warning",
          timeoutMs: cfg.approvalTimeoutMs ?? 10 * 60 * 1000,
          allowedDecisions: ["allow-once", "deny"],
          onResolution: (decision) => audit({ action: "approval", item: n.describe.name, decision }),
        } };
      }
      const r = await vault.prepare(name, event?.params, event?.toolCallId);
      if (!r) return undefined;
      if (r.block) return { block: true, blockReason: `${r.reason} (potter-vault)` };
      return { params: r.params };
    }, { priority: 10 });

    // Nothing that passed through goes back to the model or into the transcript.
    api.on("tool_result_persist", (event) => (event?.message ? { message: vault.scrub(event.message) } : undefined));
    api.on("before_message_write", (event) => (event?.message ? { message: vault.scrub(event.message) } : undefined));
  },
});
