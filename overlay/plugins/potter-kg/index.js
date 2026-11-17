// potter-kg (v3): a model of how the owner's things relate (entities, relations, provenance).
// It owns one SQLite file and nothing else; if that file is lost, Potter still works.
// All logic lives in lib/ (no SDK imports, tested with `node --test`); this file only wires it in.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import { resolveDbPath } from "./lib/paths.js";
import { openStore } from "./lib/store.js";
import { buildTools } from "./lib/tools.js";

// kg_check_connection goes through OpenClaw's own decision-model role when the host has one.
// The plugin holds no key and makes no HTTP call of its own.
function hostDecider(api, agentId) {
  const evaluate = api.runtime?.decisions?.evaluate;
  if (typeof evaluate !== "function") return undefined;
  return async ({ purpose, state, questions }) => {
    const outcome = await evaluate.call(api.runtime.decisions, { state, questions },
      { ...(agentId ? { agentId } : {}), purpose, rubricVersion: "1", timeoutMs: 15000 });
    if (outcome?.status !== "ok") return { ok: false, reason: outcome?.reason ?? outcome?.status ?? "unavailable" };
    return { ok: true, answers: outcome.result?.answers ?? {}, model: outcome.result?.model ?? null };
  };
}

export default definePluginEntry({
  id: "potter-kg",
  name: "Potter Knowledge Graph",
  description: "Typed entities, relations and provenance in one plugin-owned SQLite file.",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    // Resolved and opened on the first tool call, so a bad path fails that call, not the gateway start.
    const store = () => openStore(resolveDbPath({ explicit: cfg.dbPath, env: process.env, stateDir: resolveStateDir() }));
    for (const tool of buildTools({ store, decide: hostDecider(api, cfg.decisionAgentId) })) api.registerTool(tool);
  },
});
