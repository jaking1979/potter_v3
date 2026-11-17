// Pure guard predicates for potter-guardrails (v3). No SDK imports, so this file is unit-testable on its own.
// Rewritten from Potter v2's plugin: same three guards, but nothing is hard-coded to one person or one machine.
import { appendFileSync } from "node:fs";

export function normalize(value) {
  return String(value).toLowerCase().replace(/-/g, "");
}

/** Clean a configured list of excluded ids (for example a Notion page id). Too-short entries are dropped. */
export function cleanIds(ids) {
  if (!Array.isArray(ids)) return [];
  return ids.map((x) => normalize(x).trim()).filter((x) => /^[a-z0-9]{16,}$/.test(x));
}

/** Hard wall: does this tool call mention any excluded id? Dash- and case-insensitive, scans all params. */
export function referencesExcluded(params, derivedPaths, excludedIds) {
  const ids = cleanIds(excludedIds);
  if (ids.length === 0) return false;
  let blob = "";
  try {
    blob = JSON.stringify(params ?? {});
  } catch {
    blob = String(params);
  }
  if (Array.isArray(derivedPaths) && derivedPaths.length) blob += " " + derivedPaths.join(" ");
  const hay = normalize(blob);
  return ids.some((id) => hay.includes(id));
}

/** Append one audit line per guard decision. Never logs params or content. A logging failure changes nothing. */
export function audit(logPath, entry) {
  if (!logPath) return;
  try {
    appendFileSync(logPath, JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n", { mode: 0o600 });
  } catch {
    /* ignored on purpose */
  }
}

// ---- docs-consult-before-build gate -----------------------------------------------------------------------
export const DOCS_READ_WINDOW_MS = 10 * 60 * 1000;

/** Reading OpenClaw's own docs, the generated docs index, or the Potter manual counts as a consult. */
export function isQualifyingDocsPath(path) {
  if (typeof path !== "string" || path.length === 0) return false;
  return (
    /\/node_modules\/openclaw\/docs\//.test(path) ||
    /(^|\/)openclaw-docs-index\.md$/.test(path) ||
    /\/potter-manual\//.test(path)
  );
}

const BUILD_VERBS = {
  cron: new Set(["add", "update"]),
  gateway: new Set(["patch", "apply", "restart"]),
  skill_workshop: new Set(["create", "update", "apply"]),
};

export function isBuildVerb(tool, params) {
  if (typeof tool !== "string") return false;
  const actions = BUILD_VERBS[tool];
  if (!actions) return false;
  const action = params && typeof params === "object" && typeof params.action === "string" ? params.action : undefined;
  return action !== undefined && actions.has(action);
}

const BUILD_EXEC_PATTERNS = [
  /\bopenclaw\b[^\n|&;]*\bcron\s+(create|add|edit|update)\b/,
  /\bopenclaw\b[^\n|&;]*\bgateway\s+(restart|stop|start|patch|apply)\b/,
  /\bsystemctl\b[^\n|&;]*(?:edit|restart|stop|start|enable|disable)\b/,
  /\bcrontab\b/,
];

export function isBuildExecCommand(command) {
  if (typeof command !== "string" || command.length === 0) return false;
  return BUILD_EXEC_PATTERNS.some((re) => re.test(command));
}

// ---- config-file write guard ------------------------------------------------------------------------------
/** The gateway config file of any profile: <home>/.openclaw[-profile]/openclaw.json (absolute or ~ form). */
export function isConfigPath(p) {
  if (typeof p !== "string" || p.length === 0) return false;
  return /^(~|\/)(.*\/)?\.openclaw[^/]*\/openclaw\.json$/.test(p);
}

export function isConfigDirectWrite(tool, params) {
  if (!params || typeof params !== "object") return false;
  if (tool === "write" || tool === "edit") return isConfigPath(params.path) || isConfigPath(params.file);
  if (tool === "apply_patch") {
    if (isConfigPath(params.path)) return true;
    const input = typeof params.input === "string" ? params.input : "";
    return input.includes("openclaw.json");
  }
  if (tool === "exec") {
    const cmd = typeof params.command === "string" ? params.command : "";
    if (!/openclaw\.json/.test(cmd)) return false;
    return (
      /sed\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*i/.test(cmd) ||
      /(^|[^<>])>>?\s*\S*openclaw\.json/.test(cmd) ||
      /\btee\b[^|;&]*openclaw\.json/.test(cmd) ||
      /\b(cp|mv)\b[^|;&]*\s\S*openclaw\.json\s*($|[;&|])/.test(cmd)
    );
  }
  return false;
}

/** One decision for one tool call. Returns {guard, reason} to block, or null to allow. Pure. */
export function decide({ tool, params, derivedPaths, sessionKey }, cfg, docsReadAt, now) {
  if (referencesExcluded(params, derivedPaths, cfg.excludedIds)) {
    return { guard: "HARD_WALL", reason: "Hard wall: this tool call references an excluded page or record and was blocked." };
  }
  if (cfg.docsGate !== false) {
    if (tool === "read" && isQualifyingDocsPath(params?.path)) {
      if (sessionKey) docsReadAt.set(sessionKey, now);
    } else if (isBuildVerb(tool, params) || (tool === "exec" && isBuildExecCommand(params?.command))) {
      const last = sessionKey ? docsReadAt.get(sessionKey) : undefined;
      if (!(last !== undefined && now - last <= DOCS_READ_WINDOW_MS)) {
        return {
          guard: "DOCS_CONSULT_GATE",
          reason:
            "Docs-consult gate: read OpenClaw's own docs (or state/openclaw-docs-index.md) before building or " +
            "changing automation or config, then retry this call.",
        };
      }
    }
  }
  if (cfg.configWriteGuard !== false && isConfigDirectWrite(tool, params)) {
    return {
      guard: "CONFIG_WRITE_GUARD",
      reason: "Config write guard: change openclaw.json only with `openclaw config set` or `openclaw config patch`.",
    };
  }
  return null;
}
