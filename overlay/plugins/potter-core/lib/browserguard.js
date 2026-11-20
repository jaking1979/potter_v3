// Browser control (v3.12): what Potter is about to click, and whether that click commits the owner to something.
// Snapshots label every element ("button \"Place order\" [ref=e12]"); clicks name a ref. The guard remembers the
// labels from the latest snapshots per tab, so a click on "Place order", "Book", "Pay", "Confirm reservation" or
// "Cancel subscription" is consequential (the owner approves it, seeing the label and the page), while reading,
// navigating and typing are not. A click whose label is unknown on a page where a vault value was typed is treated
// as consequential too: the money gate never depends on a label being found.

const READ = new Set(["doctor", "status", "tabs", "snapshot", "screenshot", "text", "requests", "errors", "open", "focus", "navigate",
  "emulate", "start", "stop", "close"]);
export const COMMIT = /\b(place (your )?order|buy( now)?|purchase|pay( now)?|check ?out|complete (order|purchase|booking|reservation)|book( now| it| this)?|reserve|confirm( (booking|reservation|order|purchase|payment|cancellation))?|submit( (order|payment|application|request))?|cancel( (my )?(subscription|membership|plan|account|reservation|booking|order))|unsubscribe|delete account|send( (payment|money|request))?|transfer|donate|subscribe|start (trial|membership|subscription)|sign up|agree and (continue|pay)|i agree)\b/i;

/** Parse ref -> label pairs out of a snapshot (or navigate/act result) text. */
export function labels(text) {
  const out = new Map();
  for (const m of String(text ?? "").matchAll(/(?:^|\n)\s*-?\s*([a-z]+)\s+"([^"\n]{1,120})"[^\n]*?\[ref=([A-Za-z0-9_-]{1,20})\]/g)) {
    out.set(m[3], { role: m[1], label: m[2] });
  }
  return out;
}

export function resultText(result) {
  if (typeof result === "string") return result;
  try {
    const c = result?.content;
    if (Array.isArray(c)) return c.map((x) => x?.text ?? "").join("\n");
    return JSON.stringify(result ?? "");
  } catch { return ""; }
}

export function createGuard() {
  const tabs = new Map();           // `${session}|${targetId}` -> { refs: Map, url, vaultTyped: bool }
  const key = (s, t) => `${s ?? "-"}|${t ?? "-"}`;
  const tab = (s, t) => { const k = key(s, t); if (!tabs.has(k)) tabs.set(k, { refs: new Map(), url: "", vaultTyped: false }); return tabs.get(k); };

  return {
    /** after_tool_call for the browser: learn labels; remember a vault value was typed on this tab. */
    observe(session, params = {}, result) {
      const t = tab(session, params.targetId);
      const text = resultText(result);
      const found = labels(text);
      if (found.size) t.refs = found;
      const url = (typeof params.url === "string" && /^https?:/.test(params.url) ? params.url : null) ?? text.match(/\b(https?:\/\/[^\s"'<>]{4,300})/)?.[1];
      if (url && (params.action === "navigate" || params.action === "open")) { t.url = url; t.vaultTyped = false; }
      while (tabs.size > 200) tabs.delete(tabs.keys().next().value);
    },

    /** Classification for one browser call: { capability, risk, severity, target }. */
    classify(session, params = {}) {
      const action = String(params.action ?? "");
      if (READ.has(action)) return { capability: `browser:${action}`, risk: "read", severity: "normal", target: String(params.url ?? "").slice(0, 120) };
      if (action !== "act") return { capability: `browser:${action || "other"}`, risk: "reversible", severity: "normal", target: "" };
      // OpenClaw act calls can carry the operation inside request. Use that shape when present.
      const act = params.request && typeof params.request === "object" ? params.request : params;
      const kind = String(act.kind ?? act.type ?? "");
      // Seen before the vault substitutes the value (potter-core runs first), so the placeholder is still here.
      if (/\{\{vault:/.test(JSON.stringify(params))) tab(session, params.targetId).vaultTyped = true;
      if (["wait", "hover", "scroll", "type", "fill", "select", "check", "uncheck", "drag", "resize"].includes(kind) && !COMMIT.test(String(act.text ?? ""))) {
        return { capability: `browser:act:${kind}`, risk: "reversible", severity: "normal", target: "" };
      }
      const t = tab(session, params.targetId);
      const refs = [act.ref, ...(Array.isArray(act.refs) ? act.refs : [])].filter(Boolean);
      const named = refs.map((r) => t.refs.get(String(r))).filter(Boolean);
      const label = named.map((n) => `${n.role} "${n.label}"`).join(", ");
      const where = t.url ? ` on ${t.url.replace(/^https?:\/\//, "").slice(0, 80)}` : "";
      if (named.some((n) => COMMIT.test(n.label)) || (kind === "press" && /enter/i.test(String(act.key ?? "")) && t.vaultTyped)) {
        return { capability: "browser:commit", risk: "consequential", severity: "high", target: `${label || "Enter"}${where}` };
      }
      if (!named.length && t.vaultTyped) {
        return { capability: "browser:commit", risk: "consequential", severity: "high", target: `an unlabelled control after a vault value was typed${where}` };
      }
      return { capability: `browser:act:${kind || "click"}`, risk: "reversible", severity: "normal", target: `${label}${where}`.slice(0, 160) };
    },
  };
}
