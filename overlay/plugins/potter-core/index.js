// potter-core (v3.1 Control + v3.2 Brain): the v3 layer on top of the harness. All logic lives in lib/ (pure,
// harness-agnostic, tested with `node --test`); this file only connects it to OpenClaw's hooks.
//   before_tool_call   Control: classify, decide (allow | owner approval | block), write the "before" receipt
//   after_tool_call    Control: write the "after" receipt
//   before_agent_reply Brain: recognised house requests answered by Merrick's fast path, no main model
//   before_prompt_build Brain: the Context Governor's turn plan
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import { classify } from "./lib/classify.js";
import { decide, trustOf } from "./lib/control.js";
import { tryHouse } from "./lib/fastpath.js";
import { plan, requestText, tableDomains } from "./lib/governor.js";
import { createDecisionClient } from "./lib/jev-client.js";
import { append, recent, verify } from "./lib/receipts.js";

function envFile(path) {
  const out = {};
  try {
    for (const line of readFileSync(path, "utf8").split("\n")) {
      const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)$/);
      if (m) out[m[1]] = m[2];
    }
  } catch { /* no key: Jev stays off */ }
  return out;
}

function decisionModel(policyPath) {
  try {
    return JSON.parse(readFileSync(policyPath, "utf8")).classes?.decision?.model ?? "~typesafe/jev-latest";
  } catch {
    return "~typesafe/jev-latest";
  }
}

function jevDecider(api, cfg) {
  const evaluate = api.runtime?.decisions?.evaluate;
  if (typeof evaluate === "function") {
    return async ({ purpose, state, questions }) => {
      const o = await evaluate.call(api.runtime.decisions, { state, questions }, { purpose, rubricVersion: "1", timeoutMs: 2500 });
      return o?.status === "ok" ? { ok: true, answers: o.result?.answers ?? {} } : { ok: false, reason: o?.reason ?? o?.status };
    };
  }
  const key = envFile(cfg.jevEnvFile || "/etc/potter/secrets/jev.env").OPENROUTER_API_KEY;
  if (!key) return undefined;
  const client = createDecisionClient({ apiKey: key, fetchImpl: globalThis.fetch });
  const model = decisionModel(cfg.policyPath || "");
  return async ({ state, questions }) => {
    const o = await client.ask({ model, state, questions, timeoutMs: 2500 });
    return o.ok ? { ok: true, answers: o.answers } : { ok: false, reason: o.kind };
  };
}

function log(path, rec) {
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    appendFileSync(path, JSON.stringify({ ts: new Date().toISOString(), ...rec }) + "\n", { mode: 0o600 });
  } catch { /* telemetry never breaks a turn */ }
}

export default definePluginEntry({
  id: "potter-core",
  name: "Potter Core (v3 control and brain)",
  description: "Action control with owner approvals and receipts; context governor and System-One fast path.",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    const dir = cfg.stateDir || join(resolveStateDir(), "potter-core");
    const receipts = join(dir, "receipts.jsonl");
    const govLog = join(dir, "governor.jsonl");
    const agents = new Set(cfg.agents ?? ["potter"]);
    const ownerIds = (cfg.ownerIds ?? []).map(String);
    const decider = cfg.governor?.jev === false ? undefined : jevDecider(api, cfg);
    const pending = new Map();

    // ---- v3.1 Control ----------------------------------------------------------------------------------------
    api.on("before_tool_call", async (event, ctx) => {
      const cls = classify(event?.toolName, event?.params, { ownerIds });
      const trust = trustOf(ctx);
      const d = decide(cls, trust, cfg);
      if (d.receipt) {
        const rec = append(receipts, { phase: "intent", tool: event?.toolName, capability: cls.capability, risk: cls.risk,
          severity: cls.severity, trust, decision: d.would ? `shadow:${d.would}` : d.action, reason: d.reason, target: cls.target,
          agent: ctx?.agentId ?? null });
        if (event?.toolCallId) pending.set(event.toolCallId, rec.hash);
      }
      if (d.action === "block") return { block: true, blockReason: `${d.reason} (potter-core)` };
      if (d.action === "approve") {
        return { requireApproval: {
          title: `Approve: ${cls.capability}`,
          description: `${d.reason}. ${cls.target ? "Target: " + cls.target.slice(0, 80) + ". " : ""}Nothing has run yet.`,
          severity: cls.severity === "high" ? "critical" : "warning",
          timeoutMs: cfg.approvalTimeoutMs ?? 15 * 60 * 1000,
          allowedDecisions: ["allow-once", "deny"],
          onResolution: (decision) => append(receipts, { phase: "approval", tool: event?.toolName, capability: cls.capability,
            risk: cls.risk, severity: cls.severity, trust, decision, target: cls.target }),
        } };
      }
      return undefined;
    }, { priority: 90 });

    api.on("after_tool_call", async (event) => {
      const intent = event?.toolCallId ? pending.get(event.toolCallId) : undefined;
      if (!intent) return;
      pending.delete(event.toolCallId);
      const ok = event.error ? false : (event.result !== undefined ? true : "unknown");
      append(receipts, { phase: "result", tool: event.toolName, intent, ok, error: event.error ? String(event.error).slice(0, 160) : null,
        ms: event.durationMs ?? null, capability: classify(event.toolName, event.params, { ownerIds }).capability, target: "" });
    });

    // ---- v3.2 Brain: System-One fast path ----------------------------------------------------------------------
    if (cfg.fastPath?.enabled !== false) {
      api.on("before_agent_reply", async (event, ctx) => {
        if (ctx?.agentId && !agents.has(ctx.agentId)) return;
        const text = requestText(event);
        if (!/^\s*confirm departure\b/i.test(text) && !tableDomains(text).includes("house")) return;
        const t0 = Date.now();
        const got = await tryHouse(text, { isOwner: ctx?.requester?.senderIsOwner, merrick: cfg.fastPath?.merrick || "merrick" });
        log(govLog, { kind: "fastpath", handled: Boolean(got), intent: got?.intent ?? null, outcome: got?.outcome ?? null, ms: Date.now() - t0 });
        if (!got) return;
        if (got.intent && !/^check_|house_status|refuse_protected|heating_estimate/.test(got.intent)) {
          append(receipts, { phase: "fastpath", tool: "merrick", capability: `merrick:${got.intent}`, risk: "consequential",
            severity: got.intent === "confirm_departure" ? "high" : "normal", trust: trustOf(ctx), decision: got.outcome, target: "" });
        }
        return { handled: true, reply: { text: got.text } };
      }, { eligibleTriggers: ["user"] });
    }

    // ---- v3.2 Brain: Context Governor --------------------------------------------------------------------------
    if (cfg.governor?.enabled !== false) {
      api.on("before_prompt_build", async (event, ctx) => {
        if (ctx?.agentId && !agents.has(ctx.agentId)) return;
        const text = requestText(event);
        const p = await plan(text, { decide: decider, timeoutMs: cfg.governor?.timeoutMs ?? 2500 });
        log(govLog, { kind: "plan", domains: p.domains, source: p.source, ms: p.latencyMs, chars: text.length, error: p.error ?? null });
        return p.context ? { prependContext: p.context } : undefined;
      });
    }

    // ---- /receipts: what Potter did, for the owner ---------------------------------------------------------------
    api.registerCommand({
      name: "receipts",
      description: "Show Potter's last actions with their receipts (before and after) and check the log is intact.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async () => {
        const v = verify(receipts);
        const rows = recent(receipts, 12, (r) => r.phase !== "result" || r.ok !== true);
        const lines = rows.map((r) => `${r.ts.slice(5, 16).replace("T", " ")} ${r.phase} ${r.capability} → ${r.decision ?? (r.ok === true ? "ok" : r.ok)}${r.target ? " (" + r.target + ")" : ""}`);
        return { text: [`Receipts: ${v.count}, chain ${v.ok ? "intact" : "BROKEN at line " + v.brokenAt}.`, ...lines].join("\n") };
      },
    });
  },
});
