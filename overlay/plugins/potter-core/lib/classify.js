// Potter v3.1 Control: what kind of action is this tool call? Pure, no SDK imports (harness-agnostic).
// Risk classes follow the ratified matrix (T11/T12): read | reversible | consequential | forbidden.
// severity "high" marks consequential actions that always need the owner's tap: money, trades, deletions,
// locks and the alarm.

// Verbs are matched against whole words of a name: "notion-search" -> [notion, search],
// "HassClimateSetTemperature" -> [hass, climate, set, temperature], "GMAIL_SEND_EMAIL" -> [gmail, send, email].
const READ_WORDS = new Set("get list search find fetch read query lookup describe retrieve count check status view show history preview inspect live context".split(" "));
const CONSEQ_WORDS = new Set("send post reply forward create insert add update patch modify delete remove trash archive move share invite book reserve pay transfer purchase buy sell order trade cancel accept decline publish upload set turn toggle lock unlock arm disarm execute submit schedule start stop open close".split(" "));
const HIGH_WORDS = new Set("delete remove trash pay transfer purchase buy sell order trade wire withdraw unlock disarm lock arm".split(" "));
const OUTWARD_WORDS = new Set("send reply forward post publish share invite book reserve email".split(" "));
const READ_VERB = /^(get|list|search|find|fetch|read|query|lookup|describe|retrieve|count|check|status|view|show|history|preview|inspect)/i;

export function words(name) {
  return String(name ?? "").replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
}
const PROTECTED_HOME = /(lock|alarm|arm|disarm|valve|water|garage|siren|security)/i;

const READ_TOOLS = new Set(["read", "web_search", "web_fetch", "memory_search", "memory_get", "sessions_history",
  "session_status", "agents_list", "image", "pdf",
  // potter-vault gates its own tools (owner approval for cards and unmarked logins) and never returns a value
  "vault_list", "vault_authorize", "vault_send_to_owner"]);
const REVERSIBLE_TOOLS = new Set(["write", "edit", "apply_patch", "sessions_spawn", "sessions_send", "sessions_yield",
  "sessions_create", "process", "canvas", "tts", "browser", "nodes"]);

const EXEC_RULES = [
  // [pattern, capability, risk, severity]
  [/\bmerrick\b[^|;&]*\bconfirm-departure\b/, "exec:merrick:confirm-departure", "consequential", "high"],
  [/\brm\s+-[a-z]*r[a-z]*\s+(?!\/tmp\b|\.\/|[a-z0-9_.-]+\/?\s*$)\S*\//i, "exec:delete-tree", "consequential", "high"],
  [/\b(place|submit)[-_ ]?order\b|\btrading_precheck\.py\s+orders\b/i, "exec:trade", "consequential", "high"],
  [/\bopenclaw\b[^|;&]*\bcron\s+(add|rm|edit|enable|disable|run)\b/, "exec:openclaw:cron", "consequential", "normal"],
  [/\bpotter-cron\s+(on|off|remove)\b/, "exec:potter-cron", "consequential", "normal"],
  [/\bopenclaw\b[^|;&]*\bconfig\s+(set|patch|unset)\b/, "exec:openclaw:config", "consequential", "normal"],
  [/\bsystemctl\s+(stop|disable|mask|restart)\b/, "exec:systemctl", "consequential", "normal"],
  [/\bgit\s+push\b/, "exec:git-push", "consequential", "normal"],
  [/\bcurl\b[^|;&]*(-X\s*(POST|PUT|PATCH|DELETE)\b|--data\b|--data-binary\b|\s-d\s|--form\b|\s-F\s)/i, "exec:http-write", "consequential", "normal"],
  [/(^|[;&|(]\s*)(sudo\s+)?(sendmail|mailx?|mutt)\s/, "exec:mail", "consequential", "normal"],   // as a command, not a word in text
];

// A shell line that only looks: every command in it is a reader, and nothing is written, moved or removed.
const READERS = new Set("cat head tail ls ll grep egrep rg find wc stat du df ps pgrep which whereis echo printf pwd date jq sort uniq cut tr awk sed file id uname hostname uptime free env printenv basename dirname realpath readlink diff cmp test true".split(" "));
const WRITES = /(^|[^<>&|])>{1,2}|\btee\b|\bsed\s+(-[a-z]*i|--in-place)|\bfind\b[^|;&]*\s-(delete|exec)\b|\bawk\b[^|;&]*system\(/;
export function readOnlyShell(cmd) {
  // Redirects that only merge or discard output (2>&1, >/dev/null, 2>/dev/null, &>/dev/null) write nothing.
  const c = String(cmd ?? "").trim().replace(/\s*(\d|&)?>>?\s*(&\d|\/dev\/null)/g, " ");
  if (!c || WRITES.test(c) || /[`]|\$\(/.test(c)) return false;
  const parts = c.split(/\s*(?:\|\||&&|\||;)\s*/).filter(Boolean);
  return parts.every((p) => {
    const w = p.replace(/^(sudo|env|time|nice)\s+/, "").split(/\s+/);
    if (w[0] === "tailscale") return /^(status|serve status|funnel status|ip|version)\b/.test(w.slice(1).join(" "));
    if (w[0] === "systemctl" || w[0] === "journalctl") return w[0] === "journalctl" || /^(status|is-active|is-enabled|list-units|show)\b/.test(w[1] ?? "");
    if (w[0] === "openclaw") return /\b(list|get|status|show|inspect|runs|health|--version|--help)\b/.test(p) && !/\b(set|patch|unset|add|rm|edit|enable|disable|run|install|restart|stop|start|approve|delete)\b/.test(p.replace(/\bruns\b/, ""));
    return READERS.has(w[0]);
  });
}

function str(v) {
  return typeof v === "string" ? v : "";
}

/** The service's own action names inside a call (Composio passes them as UPPER_SNAKE slugs). */
function slugsIn(params) {
  let text = "";
  try {
    text = JSON.stringify(params ?? {});
  } catch {
    return [];
  }
  return [...new Set(text.match(/\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+){1,}\b/g) ?? [])];
}

function byVerb(name, base) {
  const w = words(String(name).split(/__/).pop());
  if (w.some((x) => HIGH_WORDS.has(x))) return { ...base, risk: "consequential", severity: "high" };
  if (w.some((x) => CONSEQ_WORDS.has(x))) return { ...base, risk: "consequential", severity: "normal" };
  if (w.some((x) => READ_WORDS.has(x))) return { ...base, risk: "read", severity: "normal" };
  return null;
}

/**
 * @param toolName  the host tool id (MCP tools arrive as "<server>__<tool>")
 * @param params    the tool arguments
 * @param opts      { ownerIds?: string[] }
 * @returns { capability, risk, severity, target }
 */
// Potter's own tools that change what Potter believes about the owner: only the owner may apply them.
const OWNER_ONLY = new Set(["memory_review_apply"]);

export function classify(toolName, params = {}, opts = {}) {
  const name = str(toolName);
  if (OWNER_ONLY.has(name)) return { capability: name, risk: "consequential", severity: "normal", target: "" };
  const owners = new Set((opts.ownerIds ?? []).map(String));
  if (READ_TOOLS.has(name)) return { capability: name, risk: "read", severity: "normal", target: "" };

  if (name === "exec") {
    const cmd = str(params.command) || str(params.cmd) || (Array.isArray(params.argv) ? params.argv.join(" ") : "");
    for (const [re, capability, risk, severity] of EXEC_RULES) {
      if (re.test(cmd)) return { capability, risk, severity, target: cmd.slice(0, 80) };
    }
    if (readOnlyShell(cmd)) return { capability: "exec:read", risk: "read", severity: "normal", target: cmd.slice(0, 80) };
    return { capability: "exec", risk: "reversible", severity: "normal", target: cmd.slice(0, 80) };
  }

  if (name === "cron") {
    const action = str(params.action) || str(params.op) || "list";
    return READ_VERB.test(action) || action === "runs"
      ? { capability: `cron:${action}`, risk: "read", severity: "normal", target: "" }
      : { capability: `cron:${action}`, risk: "consequential", severity: "normal", target: str(params.name || params.id) };
  }

  if (name === "message") {
    const action = str(params.action) || "send";
    const to = String(params.to ?? params.target ?? params.chatId ?? params.recipient ?? "");
    if (READ_VERB.test(action)) return { capability: `message:${action}`, risk: "read", severity: "normal", target: to };
    const toOwner = !to || owners.has(to) || [...owners].some((o) => to.endsWith(`:${o}`));
    return { capability: `message:${action}`, risk: toOwner ? "reversible" : "consequential", severity: "normal", target: to };
  }

  if (name === "gateway") return { capability: "gateway", risk: "consequential", severity: "normal", target: str(params.action) };
  if (REVERSIBLE_TOOLS.has(name)) return { capability: name, risk: "reversible", severity: "normal", target: str(params.path || params.file_path) };

  const mcp = name.match(/^(?:mcp__)?([a-z0-9-]+)__(.+)$/i);
  if (mcp) {
    const [, server, tool] = mcp;
    const base = { capability: `mcp:${server}:${tool}`, target: "" };
    const toolWords = words(tool);
    if (/home.?assistant/i.test(server) && PROTECTED_HOME.test(`${tool} ${JSON.stringify(params ?? {})}`)
        && !toolWords.some((x) => READ_WORDS.has(x))) {
      return { ...base, risk: "forbidden", severity: "high" };     // locks, alarms and valves: never by tool call
    }
    // Before the house switch-over the other assistant runs the house: Potter may only READ Home Assistant.
    if (/home.?assistant/i.test(server) && opts.houseReadOnly !== false) {
      const v = byVerb(tool, base);
      if (!v || v.risk !== "read") return { ...base, risk: "forbidden", severity: "high", reason: "house is read-only until the switch-over" };
      return v;
    }
    // A meta-tool that runs other tools (Composio): judge by the slugs it carries, worst first.
    const slugs = slugsIn(params).filter((s) => s !== tool.toUpperCase());
    if (slugs.length) {
      const verdicts = slugs.map((s) => byVerb(s, { capability: `mcp:${server}:${s}`, target: "" })).filter(Boolean);
      const worst = verdicts.find((v) => v.severity === "high") || verdicts.find((v) => v.risk === "consequential")
        || verdicts.find((v) => v.risk === "read");
      if (worst) return worst;
    }
    return byVerb(tool, base) ?? { ...base, risk: "reversible", severity: "normal" };
  }

  // A tool from a local plugin (the knowledge graph, the mail channel...) changes Potter's own state, so it is
  // reversible unless it reaches outside (sends, publishes) or moves money.
  const w = words(name);
  const base = { capability: name, target: "" };
  if (w.some((x) => HIGH_WORDS.has(x))) return { ...base, risk: "consequential", severity: "high" };
  if (w.some((x) => OUTWARD_WORDS.has(x))) return { ...base, risk: "consequential", severity: "normal" };
  if (w.some((x) => READ_WORDS.has(x))) return { ...base, risk: "read", severity: "normal" };
  return { ...base, risk: "reversible", severity: "normal" };
}
