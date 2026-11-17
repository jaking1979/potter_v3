// potter-guardrails (v3): deterministic guards that run in the Gateway, outside the model.
//  1. Hard wall: any tool call that references an excluded id (for example the EHR page) is blocked.
//  2. Docs-consult gate: building or changing automation/config requires a recent read of the docs.
//  3. Config write guard: openclaw.json may only be changed through the CLI.
import { homedir } from "node:os";
import { join } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { audit, decide } from "./guards.js";

const docsReadAt = new Map(); // in memory on purpose: a restart clears it, which fails closed

export default definePluginEntry({
  id: "potter-guardrails",
  name: "Potter Guardrails",
  description: "Deterministic guards: hard wall, docs-consult-before-build, config write guard.",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    const log = cfg.auditLog || join(homedir(), ".openclaw", "potter-guardrails-audit.log");
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const verdict = decide(
          { tool: event?.toolName, params: event?.params, derivedPaths: event?.derivedPaths, sessionKey: ctx?.sessionKey },
          cfg,
          docsReadAt,
          Date.now(),
        );
        if (!verdict) return;
        audit(log, { guard: verdict.guard, tool: event?.toolName ?? "unknown", action: "block" });
        return { block: true, blockReason: verdict.reason + " (potter-guardrails)" };
      },
      { priority: 100 },
    );
  },
});
