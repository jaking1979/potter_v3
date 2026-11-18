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
import { createGuard } from "./lib/browserguard.js";
import { decide, isEvalSession, trustOf } from "./lib/control.js";
import { tryHouse } from "./lib/fastpath.js";
import { plan, requestText, tableDomains } from "./lib/governor.js";
import { names, search } from "./lib/context.js";
import { createDecisionClient } from "./lib/jev-client.js";
import { append, recent, verify } from "./lib/receipts.js";
import { chooseClass, costOf, loadClasses, recordSpend, spentToday, toOverride } from "./lib/broker.js";
import { alreadyDone, effectKey, enqueue, inDoubt, isOutboxRetry } from "./lib/durable.js";
import { buildTools } from "./lib/tools.js";
import { assemble, buildIndex, fresh } from "./lib/context.js";
import { loadGraph, recall, stale } from "./lib/kgrecall.js";
import { enqueueMessage, shouldQueue } from "./lib/capturequeue.js";
import { DOMAINS } from "./lib/governor.js";

function escalationsToday(path) {
  if (!existsSync(path)) return 0;
  const day = new Date().toISOString().slice(0, 10);
  return readFileSync(path, "utf8").split("\n")
    .filter((l) => l.startsWith(`{"ts":"${day}`) && l.includes('"kind":"broker"') && l.includes('"cls":"escalation"')).length;
}

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

// Jev with the owner's key, directly (the request format v2 used). OpenClaw's own decision service is used only when
// there is no key, and any failure there is a "no answer", never an exception into the turn.
function jevDecider(api, cfg) {
  const key = envFile(cfg.jevEnvFile || "/etc/potter/secrets/jev.env").OPENROUTER_API_KEY;
  if (key) {
    const client = createDecisionClient({ apiKey: key, fetchImpl: globalThis.fetch });
    const model = decisionModel(cfg.policyPath || "");
    return async ({ state, questions }) => {
      const o = await client.ask({ model, state, questions, timeoutMs: 2500 });
      return o.ok ? { ok: true, answers: o.answers } : { ok: false, reason: o.kind + (o.status ? `:${o.status}` : "") };
    };
  }
  const evaluate = api.runtime?.decisions?.evaluate;
  if (typeof evaluate !== "function") return undefined;
  return async ({ purpose, state, questions }) => {
    try {
      const o = await evaluate.call(api.runtime.decisions, { state, questions }, { purpose, rubricVersion: "1", timeoutMs: 2500 });
      return o?.status === "ok" ? { ok: true, answers: o.result?.answers ?? {} } : { ok: false, reason: o?.reason ?? o?.status };
    } catch (e) {
      return { ok: false, reason: e?.name ?? "host-decision-error" };
    }
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
    const ws = cfg.workspace || "";
    const spend = join(dir, "spend.jsonl");
    const outbox = join(dir, "outbox-notion.json");
    const runClass = new Map();

    // ---- v3.4 / v3.5 tools ----------------------------------------------------------------------------------------
    if (ws) {
      for (const tool of buildTools({ staged: join(ws, "state", "memory", "staged.jsonl"), memory: join(ws, "MEMORY.md"),
        responsibilities: join(ws, "state", "responsibilities.json"), outbox })) api.registerTool(tool);
    }

    // ---- v3.1 Control ----------------------------------------------------------------------------------------
    const browser = createGuard();                 // v3.12: which browser clicks commit the owner to something
    api.on("before_tool_call", async (event, ctx) => {
      const cls = event?.toolName === "browser" ? browser.classify(ctx?.sessionKey, event?.params)
        : classify(event?.toolName, event?.params, { ownerIds });
      if (isEvalSession(ctx) && cls.risk !== "read") {
        return { block: true, blockReason: "evaluation run: only reading is allowed; describe what you would do instead (potter-core)" };
      }
      const trust = trustOf(ctx);
      let d = decide(cls, trust, cfg);
      const effect = cls.risk === "consequential" ? effectKey(event?.toolName, event?.params) : null;
      if (effect && isOutboxRetry(outbox, effect)) {
        d = { action: "allow", reason: "retry of a queued Notion write (already decided)", receipt: true };   // v3.5
      } else if (effect && d.action === "allow") {
        const prior = alreadyDone(receipts, effect);
        if (prior) d = { action: "approve", reason: `this exact action already succeeded on ${prior.ts.slice(0, 16).replace("T", " ")} UTC; do it again?`, receipt: true };
      }
      if (d.receipt) {
        const rec = append(receipts, { phase: "intent", tool: event?.toolName, capability: cls.capability, risk: cls.risk,
          severity: cls.severity, trust, decision: d.would ? `shadow:${d.would}` : d.action, reason: d.reason, target: cls.target,
          agent: ctx?.agentId ?? null, effect });
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

    api.on("after_tool_call", async (event, ctx) => {
      if (event?.toolName === "browser") browser.observe(ctx?.sessionKey, event.params, event.result);
      const intent = event?.toolCallId ? pending.get(event.toolCallId) : undefined;
      if (!intent) return;
      pending.delete(event.toolCallId);
      const ok = event.error ? false : (event.result !== undefined ? true : "unknown");
      const c = classify(event.toolName, event.params, { ownerIds });
      if (ok === false && c.risk === "consequential" && /^mcp:notion/i.test(c.capability)) {
        enqueue(outbox, { tool: event.toolName, params: event.params, error: event.error });              // v3.5 outbox
      }
      append(receipts, { phase: "result", tool: event.toolName, intent, ok, error: event.error ? String(event.error).slice(0, 160) : null,
        ms: event.durationMs ?? null, capability: classify(event.toolName, event.params, { ownerIds }).capability, target: "" });
    });

    // ---- v3.3 model broker -----------------------------------------------------------------------------------------
    if (cfg.broker?.enabled !== false) {
      api.on("before_model_resolve", async (event, ctx) => {
        if (ctx?.trigger === "cron" || (ctx?.agentId && !agents.has(ctx.agentId))) return;  // jobs name their model
        const text = requestText({ prompt: event?.prompt, currentUserMessage: event?.currentUserMessage });
        const classes = loadClasses(cfg.policyPath || "");
        const capUsd = cfg.broker?.escalationDailyCapUsd ?? 3;
        const spentUsd = spentToday(spend, { cls: "escalation" });
        const overCount = escalationsToday(govLog) >= (cfg.broker?.escalationDailyMax ?? 20);
        const pick = chooseClass(text, { domains: tableDomains(text), escalationSpentUsd: overCount ? capUsd : spentUsd, escalationCapUsd: capUsd });
        if (ctx?.runId) runClass.set(ctx.runId, pick.cls);
        // escalation_today is what the daily escalation cap counts; day_total is every model call recorded today.
        log(govLog, { kind: "broker", cls: pick.cls, reason: pick.reason, escalation_today_usd: Number(spentUsd.toFixed(4)),
          escalation_cap_usd: capUsd, day_total_usd: Number(spentToday(spend).toFixed(4)) });
        if (pick.cls === "chat" || cfg.broker?.mode === "shadow") return;
        return toOverride(classes[pick.cls]) ?? undefined;
      });
      api.on("llm_output", async (event, ctx) => {
        const u = event?.usage ?? event?.lastAssistant?.usage ?? {};
        const num = (...v) => v.find((x) => typeof x === "number" && Number.isFinite(x)) ?? null;
        recordSpend(spend, { cls: (ctx?.runId && runClass.get(ctx.runId)) || (ctx?.trigger === "cron" ? "job_turn" : "chat"),
          model: [event?.provider, event?.model].filter(Boolean).join("/") || null,
          inputTokens: num(u.input, u.inputTokens, u.input_tokens, u.prompt_tokens),
          outputTokens: num(u.output, u.outputTokens, u.output_tokens, u.completion_tokens),
          costUsd: costOf(u) ?? num(u.cost?.total) });
      });
    }

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
      // v3.6 Brain v1: the owner's files indexed once, rebuilt only when one changes (checked at most every 30 s).
      let index = null, checked = 0;
      const getIndex = () => {
        if (!ws || cfg.brain?.enabled === false) return null;
        const t = Date.now();
        if (!index || (t - checked > 30000 && !fresh(index))) index = buildIndex(ws);
        if (t - checked > 30000) checked = t;
        return index;
      };
      // The knowledge graph, read-only, reloaded when its file changes (checked at most every 30 s).
      let graph = null, graphChecked = 0;
      const getGraph = () => {
        if (cfg.kg?.enabled === false || !cfg.kg?.dbPath || !existsSync(cfg.kg.dbPath)) return null;
        const t = Date.now();
        if (!graph || (t - graphChecked > 30000 && stale(graph))) {
          try { graph = loadGraph(cfg.kg.dbPath, { owner: cfg.kg.owner }); } catch { graph = null; }
        }
        if (t - graphChecked > 30000) graphChecked = t;
        return graph;
      };
      api.on("before_prompt_build", async (event, ctx) => {
        if (ctx?.agentId && !agents.has(ctx.agentId)) return;
        const text = requestText(event);
        // The graph learns from what the owner says (processed by the potter-kg capture job).
        const queued = cfg.kg?.capture !== false && Boolean(cfg.kg?.queuePath) && shouldQueue(text, ctx, ownerIds) && enqueueMessage(cfg.kg.queuePath, text, ctx ?? {});
        // File search shortlist (keyword table domains only: the Jev plan is not back yet), judged by Jev in the
        // same request as the graph facts. The plan and that request run at the same time: no added wait.
        let shortlist = [];
        try {
          const idx = getIndex();
          const ex = [...new Set(tableDomains(text).flatMap((id) => DOMAINS.find((d) => d.id === id)?.recall ?? []))];
          if (idx && text.trim()) shortlist = search(idx, [text, ...ex].join(" "), 4, { boost: names(text) });
        } catch { shortlist = []; }
        const [p, kg] = await Promise.all([
          plan(text, { decide: decider, timeoutMs: cfg.governor?.timeoutMs ?? 2500 }),
          recall({ graph: getGraph(), text, decide: decider, timeoutMs: cfg.governor?.timeoutMs ?? 2500, budget: cfg.kg?.budget ?? 2500, passages: shortlist })
            .catch(() => ({ context: "", entities: [], considered: 0, kept: [], passagesKept: shortlist, source: "error", ms: 0 })),
        ]);
        let wc = { context: "", sources: [], chars: 0 };
        const t0 = Date.now();
        try {
          const doms = p.domains.map((id) => DOMAINS.find((d) => d.id === id)).filter(Boolean);
          const rules = [...new Set(doms.flatMap((d) => d.rules ?? []))];
          const expand = [...new Set(doms.flatMap((d) => d.recall ?? []))];
          wc = assemble({ text, rules, expand, index: getIndex(), ws, tz: cfg.timezone || "UTC", budget: cfg.brain?.budget ?? 7000, hits: kg.passagesKept });
        } catch { /* a failure means no working context, never a blocked turn */ }
        log(govLog, { kind: "plan", domains: p.domains, source: p.source, ms: p.latencyMs, chars: text.length, error: p.error ?? null,
          context_chars: wc.chars, recalled: wc.sources.map((x) => x.path), build_ms: Date.now() - t0,
          kg_entities: kg.entities, kg_considered: kg.considered, kg_kept: kg.kept.length, kg_source: kg.source, kg_ms: kg.ms,
          files_considered: shortlist.length, files_kept: kg.passagesKept?.length ?? 0, kg_queued: queued, sender_known: Boolean(ctx?.senderId),
          kg_error: kg.error ?? null });
        // Which graph facts were used: the freshness job asks about used facts that may have changed.
        if (cfg.kg?.usagePath && kg.kept.some((f) => f.key)) {
          try {
            appendFileSync(cfg.kg.usagePath, kg.kept.filter((f) => f.key).map((f) => JSON.stringify({ ts: new Date().toISOString(), key: f.key })).join("\n") + "\n", { mode: 0o600 });
          } catch { /* usage is a hint, never a blocker */ }
        }
        const out = [p.context, kg.context, wc.context].filter(Boolean).join("\n\n");
        return out ? { prependContext: out } : undefined;
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
        const doubt = inDoubt(receipts);
        const today = spentToday(spend);
        const head = [`Receipts: ${v.count}, chain ${v.ok ? "intact" : "BROKEN at line " + v.brokenAt}. Model spend today: $${today.toFixed(2)}.`];
        if (doubt.length) head.push(`In doubt (started, no recorded result): ${doubt.slice(-5).map((r) => `${r.ts.slice(5, 16).replace("T", " ")} ${r.capability}`).join("; ")}. Check these happened once.`);
        return { text: [...head, ...lines].join("\n") };
      },
    });
  },
});
