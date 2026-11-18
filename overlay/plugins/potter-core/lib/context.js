// Potter v3.6 Brain v1 (00 Brain Architecture): before the main model thinks, build its working context.
//   recall   the passages of the owner's own files that matter for this request (a local BM25 index: no network,
//            milliseconds), skipping files the harness already injects every turn
//   rules    the full text of the rules the request's domains need, instead of "go read X"
//   state    the date and time where the owner is, what is due, what waits for the owner, today's working notes
// A hard character budget keeps it focus, not bulk. Nothing relevant found: nothing added.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

// Files the harness already puts in every prompt (OpenClaw workspace bootstrap): never repeated.
export const INJECTED = new Set(["AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md", "USER.md", "HEARTBEAT.md", "rules/INDEX.md"]);
// Where the owner's own knowledge lives. Machinery (skills, state, reference copies of v2) is not recalled.
export const RECALL_ROOTS = ["MEMORY.md", "preferences", "rules", "learnings", "memory"];
const SKIP = /(^|\/)(global|reference|skills|state|node_modules)\//;
// Sections that are lists of keywords or bookkeeping match almost any request and say nothing: never recalled.
const META_HEAD = /^(retrieval triggers?|triggers?|last updated|keywords|tags|see also|changelog|history|sources?)\b/i;
const SECRETISH = /(sk-[A-Za-z0-9_-]{8,}|\d{5,12}:[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9._-]{20,}|tskey-[A-Za-z0-9-]+)/g;
const STOP = new Set(("a an and are as at be but by can could did do does for from had has have how i if in into is it its " +
  "me my of on or our so than that the their them then there these they this to was we were what when where which who why " +
  "will with would you your about just should get got any all some potter please tell show").split(" "));

export function tokens(text) {
  return String(text ?? "").toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/)
    .map((w) => w.replace(/'s$/, "").replace(/'/g, "")).filter((w) => w.length > 2 && !STOP.has(w))
    .map((w) => (w.length > 4 && !/ss$/.test(w) ? w.replace(/ies$/, "y").replace(/s$/, "") : w));   // wines -> wine, stories -> story
}

/** Split a markdown file into passages: one per heading section, long sections split by paragraph. */
export function passages(path, text, max = 900) {
  const out = [];
  let head = "";
  for (const block of String(text).split(/\n(?=#{1,4} )/)) {
    const m = block.match(/^(#{1,4}) (.+)/);
    if (m) head = m[2].trim();
    let buf = "";
    for (const para of block.split(/\n\s*\n/)) {
      if ((buf + para).length > max && buf) { out.push({ path, head, text: buf.trim() }); buf = ""; }
      buf += para + "\n\n";
    }
    if (buf.trim()) out.push({ path, head, text: buf.trim() });
  }
  return out.filter((p) => p.text.replace(/^#.*$/gm, "").trim().length > 20);
}

function walk(root, rel, files) {
  const full = join(root, rel);
  let st;
  try { st = statSync(full); } catch { return; }
  if (st.isDirectory()) {
    for (const n of readdirSync(full)) if (!n.startsWith(".")) walk(root, join(rel, n), files);
  } else if (/\.md$/.test(rel) && !INJECTED.has(rel) && !SKIP.test(rel)) {
    files.push({ rel, mtime: st.mtimeMs, size: st.size });
  }
}

/** A BM25 index over the owner's files. Cheap to rebuild; `fresh()` says whether any file changed. */
export function buildIndex(workspace, roots = RECALL_ROOTS) {
  const files = [];
  for (const r of roots) walk(workspace, r, files);
  const docs = [];
  for (const f of files) {
    if (f.size > 400000) continue;
    let text;
    try { text = readFileSync(join(workspace, f.rel), "utf8"); } catch { continue; }
    for (const p of passages(f.rel, text).filter((x) => !META_HEAD.test(x.head))) {
      const toks = tokens(`${p.head} ${p.head} ${p.text}`);
      // Mostly code or tool syntax (how the previous assistant called things) is machinery, not knowledge of the owner.
      const code = (p.text.match(/```[\s\S]*?```/g) ?? []).join("").length + (p.text.match(/^\s*\|.*\|\s*$/gm) ?? []).join("").length * 0;
      const weight = code > p.text.length * 0.4 || /\b(tool name|parameters|inboxId|mcp__|curl |--[a-z-]+)\b/.test(p.text) ? 0.35 : 1;
      if (toks.length) docs.push({ ...p, toks, len: toks.length, weight });
    }
  }
  const df = new Map();
  for (const d of docs) for (const t of new Set(d.toks)) df.set(t, (df.get(t) ?? 0) + 1);
  const avg = docs.reduce((s, d) => s + d.len, 0) / Math.max(1, docs.length);
  const stamp = files.map((f) => `${f.rel}:${f.mtime}`).join("|");
  return { workspace, roots, docs, df, avg, stamp, built: Date.now() };
}

export function fresh(index) {
  const files = [];
  for (const r of index.roots) walk(index.workspace, r, files);
  return files.map((f) => `${f.rel}:${f.mtime}`).join("|") === index.stamp;
}

/** Names in the request (a person, a place, a brand): the words that carry its meaning. */
export function names(text) {
  const out = new Set();
  const t = String(text ?? "");
  for (const m of t.matchAll(/\b([A-Z][a-z]{2,})\b/g)) {
    if (/(^|[.?!:]\s+)$/.test(t.slice(0, m.index))) continue;          // a capital starting a sentence is not a name
    const w = m[1].toLowerCase();
    if (!STOP.has(w) && !["what", "how", "should", "remind", "tell", "show", "set", "can", "did", "who", "where", "when", "why", "is", "are"].includes(w)) out.add(tokens(w)[0] ?? w);
  }
  return out;
}

export function search(index, query, k = 4, { minScore = 1.0, relative = 0.5, boost = names(query) } = {}) {
  const q = [...new Set(tokens(query))];
  if (!q.length || !index.docs.length) return [];
  const N = index.docs.length, k1 = 1.4, b = 0.75;
  const scored = index.docs.map((d) => {
    let s = 0;
    for (const t of q) {
      const n = index.df.get(t);
      if (!n) continue;
      const tf = d.toks.filter((x) => x === t).length;
      if (!tf) continue;
      const idf = Math.log(1 + (N - n + 0.5) / (n + 0.5));
      s += (boost.has(t) ? 3 : 1) * idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * d.len / index.avg));
    }
    // A request that names someone is about them: a passage that never mentions any named person counts for less.
    const named = boost.size ? [...boost].some((t) => d.toks.includes(t)) : true;
    return { d, s: s * (d.weight ?? 1) * (named ? 1 : 0.3) };
  }).filter((x) => x.s >= minScore).sort((a, b) => b.s - a.s);
  const top = scored[0]?.s ?? 0;
  const strong = scored.filter((x) => x.s >= top * relative);          // weak matches far below the best are noise
  const seen = new Set();
  const out = [];
  for (const { d, s } of strong) {                 // at most two passages from one file
    const n = [...seen].filter((p) => p === d.path).length;
    if (n >= 2) continue;
    seen.add(d.path);
    out.push({ path: d.path, head: d.head, text: d.text, score: Number(s.toFixed(2)) });
    if (out.length >= k) break;
  }
  return out;
}

function read(path, max) {
  try { return readFileSync(path, "utf8").slice(0, max); } catch { return ""; }
}

/** What the owner's situation is right now, from Potter's own state files. */
export function stateBlock(ws, { now = new Date(), tz = "UTC", dueWithin = 3 } = {}) {
  const lines = [];
  let local = now.toISOString();
  try {
    local = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "long", year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit" }).format(now);
  } catch { /* bad zone: ISO */ }
  lines.push(`Now: ${local} (${tz}).`);
  try {
    const db = JSON.parse(read(join(ws, "state", "responsibilities.json"), 200000) || "{}");
    const limit = new Date(now.getTime() + dueWithin * 86400000).toISOString().slice(0, 10);
    const due = (db.items ?? []).filter((i) => i.next_due <= limit).slice(0, 5);
    if (due.length) lines.push("Due soon: " + due.map((i) => `${i.name} (${i.next_due})`).join("; ") + ".");
  } catch { /* none */ }
  const staged = read(join(ws, "state", "memory", "staged.jsonl"), 400000).split("\n").filter((l) => l.includes('"status":"pending"')).length;
  if (staged) lines.push(`${staged} proposed memor${staged === 1 ? "y waits" : "ies wait"} for the owner's review.`);
  const notes = read(join(ws, "state", "compiling-doc.md"), 1200).trim();
  if (notes && notes.split("\n").length > 2) lines.push("Today's working notes (head):\n" + notes.split("\n").slice(0, 12).join("\n"));
  return lines.join("\n");
}

/** Full text of the rule files the domains need (global rules only; personal ones arrive through recall). */
export function ruleTexts(ws, rules, budget) {
  const out = [];
  let left = budget;
  for (const r of rules) {
    const t = read(join(ws, "rules", r), 6000).trim();
    if (!t || t.length > left) continue;
    out.push(`--- rules/${r} ---\n${t}`);
    left -= t.length;
  }
  return out;
}

/**
 * @returns { context, sources: [{path, head, score}], chars }
 */
export function assemble({ text, rules = [], expand = [], index, ws, now, tz, budget = 7000, hits: given }) {
  const parts = [];
  const sources = [];
  const st = ws ? stateBlock(ws, { now, tz }) : "";
  if (st) parts.push("## Your situation now\n" + st);
  let left = budget - st.length;
  // `given`: passages already judged relevant (Jev); otherwise the search's own best matches.
  const hits = given ?? (index ? search(index, [text, ...expand].join(" "), 4, { boost: names(text) }) : []);
  const recall = [];
  for (const h of hits) {
    const t = h.text.replace(SECRETISH, "(secret)");
    if (t.length + 60 > left * 0.7) continue;
    recall.push(`From ${h.path}${h.head ? " — " + h.head : ""}:\n${t}`);
    sources.push({ path: h.path, head: h.head, score: h.score });
    left -= t.length + 60;
  }
  if (recall.length) parts.push("## What you already know that bears on this\n" + recall.join("\n\n"));
  const rt = ws ? ruleTexts(ws, rules, Math.max(0, left)) : [];
  if (rt.length) parts.push("## Rules for this kind of request\n" + rt.join("\n\n"));
  if (!recall.length && !rt.length) return { context: parts.length ? parts.join("\n\n") : "", sources, chars: parts.join("").length };
  const context = "Working context (Potter Context Governor; built for this request from your own files):\n\n" + parts.join("\n\n") +
    "\n\nUse this first. Look further only if it does not answer the request.";
  return { context, sources, chars: context.length };
}

export function rel(ws, p) {
  return relative(ws, p);
}

export function exists(p) {
  return existsSync(p);
}
