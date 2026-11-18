// Potter v3 Brain: the knowledge graph is read on every request, not when the model remembers to.
//   match      people, places, projects named in the request (graph names and aliases)
//   candidates their direct facts, strongest first, capped
//   relevance  one Jev request: one narrow yes/no per candidate ("does this fact help with this request?"),
//              composed in code (docs.typesafe.ai: classifying RAG passages / routing by probability)
//   render     what survives, labelled by confidence; without Jev, the high-confidence facts only
// The graph file is opened READ-ONLY; potter-kg owns it. A failure means no graph context, never a blocked turn.
import { statSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

const key = (s) => String(s ?? "").trim().toLowerCase().replace(/[^\p{L}\p{N}' -]+/gu, " ").replace(/\s+/g, " ").trim();
// Words that are names of things in many graphs but in a request are only ordinary words.
const COMMON = new Set(("the a an and or of to in on at for with my our your me we you it this that is are was be do " +
  "what how when where who why can could should would will please today tomorrow home house work day week time " +
  "plan plans note notes list lists task tasks thing things email mail call text dinner lunch").split(" "));

const PHRASE = {
  lives_at: "lives at", works_on: "works on", interested_in: "is interested in", filed_as: "is filed as",
  references: "references", connects_to: "is connected to", captured_from: "was captured from", decided_on: "decided on",
  serves: "serves", is_part_of: "is part of", supersedes: "supersedes", contradicts: "contradicts", runs_on: "runs on",
  integrates_with: "integrates with", controls: "controls", accessed_by: "is accessed by", uses: "uses",
  depends_on: "depends on", runs: "runs", candidate_host_for: "is a candidate host for", located_in: "is located in",
  monitors: "monitors", married_to: "is married to", parent_of: "is a parent of",
  prefers: "prefers", avoids: "avoids", responsible_for: "is responsible for",
};
const RANK = { high: 0, medium: 1, low: 2 };

function parseList(s) {
  try { const v = JSON.parse(s ?? "[]"); return Array.isArray(v) ? v : []; } catch { return []; }
}

/** Load the live part of the graph once. Cheap for thousands of entities. */
export function loadGraph(path, { owner: ownerId } = {}) {
  const st = statSync(path);
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    db.exec("PRAGMA busy_timeout=2000;");
    const entities = new Map();
    for (const e of db.prepare("SELECT id, type, name, description, aliases, confidence FROM entities WHERE status='active'").all()) {
      entities.set(e.id, { ...e, aliases: parseList(e.aliases) });
    }
    const adj = new Map();
    for (const r of db.prepare("SELECT source_id, predicate, target_id, confidence, provenance, updated FROM relations WHERE status='active'").all()) {
      if (!entities.has(r.source_id) || !entities.has(r.target_id)) continue;
      const rel = { ...r, provenance: parseList(r.provenance) };
      for (const id of [r.source_id, r.target_id]) { if (!adj.has(id)) adj.set(id, []); adj.get(id).push(rel); }
    }
    for (const list of adj.values()) {
      list.sort((x, y) => (RANK[x.confidence] ?? 1) - (RANK[y.confidence] ?? 1) || String(y.updated).localeCompare(String(x.updated)));
    }
    const keys = new Map();
    const put = (k, id) => { if (!k || k.length < 3 || COMMON.has(k)) return; if (!keys.has(k)) keys.set(k, new Set()); keys.get(k).add(id); };
    for (const e of entities.values()) {
      put(key(e.name), e.id);
      for (const a of e.aliases) put(key(a), e.id);
      // "person:ada" also answers to "ada"
      const slug = e.id.split(":")[1];
      if (slug && !slug.includes("-")) put(key(slug), e.id);
    }
    // The owner: the most connected person (a personal graph is someone's). "I" and "my" mean them.
    let owner = ownerId && entities.has(ownerId) ? ownerId : null, best = owner ? Infinity : -1;
    for (const e of entities.values()) if (e.type === "person" && (adj.get(e.id)?.length ?? 0) > best) { owner = e.id; best = adj.get(e.id)?.length ?? 0; }
    return { path, stamp: `${st.mtimeMs}:${st.size}`, entities, adj, keys, maxWords: 5, owner };
  } finally {
    db.close();
  }
}

export function stale(graph) {
  try { const st = statSync(graph.path); return `${st.mtimeMs}:${st.size}` !== graph.stamp; } catch { return true; }
}

/** Entities named in the text: longest match wins its words; ambiguous names return every match. */
export function match(graph, text, { max = 5, owner = graph.owner } = {}) {
  const words = String(text ?? "").split(/\s+/).filter(Boolean);
  const used = new Array(words.length).fill(false);
  const found = [];
  for (let n = Math.min(graph.maxWords, words.length); n >= 1; n--) {
    for (let i = 0; i + n <= words.length; i++) {
      if (used.slice(i, i + n).some(Boolean)) continue;
      const k = key(words.slice(i, i + n).join(" "));
      const k2 = k.replace(/'s$/, "");
      const ids = graph.keys.get(k) ?? graph.keys.get(k2);
      if (!ids) continue;
      for (let j = i; j < i + n; j++) used[j] = true;
      for (const id of ids) if (!found.some((f) => f.id === id)) found.push({ id, words: n, said: words.slice(i, i + n).join(" ") });
    }
  }
  const deg = (id) => graph.adj.get(id)?.length ?? 0;
  const out = found.sort((a, b) => b.words - a.words || deg(b.id) - deg(a.id)).slice(0, max);
  // A first-person request is also about the owner; named entities come first.
  if (owner && graph.entities.has(owner) && !out.some((f) => f.id === owner) && /\b(i|i'm|i've|my|me|we|our|us)\b/i.test(String(text)) && out.length < max) {
    out.push({ id: owner, words: 0, said: "(first person)" });
  }
  return out;
}

/** Candidate facts about the matched entities, strongest first. */
export function candidates(graph, matched, { perEntity = 10, total = 30 } = {}) {
  const name = (id) => graph.entities.get(id)?.name ?? id;
  const facts = [];
  const seen = new Set();
  for (const m of matched) {
    const e = graph.entities.get(m.id);
    if (!e) continue;
    if (e.description && e.description.trim().length > 3) {
      facts.push({ text: `${e.name} (${e.type}): ${e.description.trim().slice(0, 300)}`, confidence: e.confidence || "medium", kind: "about", about: e.id });
    }
    let n = 0;
    for (const r of graph.adj.get(m.id) ?? []) {
      const k = `${r.source_id}|${r.predicate}|${r.target_id}`;
      if (seen.has(k)) continue;
      seen.add(k);
      facts.push({ text: `${name(r.source_id)} ${PHRASE[r.predicate] ?? r.predicate.replace(/_/g, " ")} ${name(r.target_id)}.`,
        confidence: r.confidence || "medium", kind: "relation", about: m.id, source: r.provenance[0] ?? null });
      if (++n >= perEntity) break;
    }
  }
  return facts.slice(0, total).map((f, i) => ({ ...f, i }));
}

/** One Jev request: an independent yes/no per fact, all evaluated against the same request. */
export function relevanceRequest(text, facts) {
  const state = { request: String(text).slice(0, 1500), facts: facts.map((f) => f.text) };
  const questions = {};
  for (const f of facts) {
    questions[`f${f.i}`] = {
      type: "noul",
      instructions: {
        question: `Does \`facts[${f.i}]\` help answer or act on \`request\`?`,
        focus: "Useful background for this specific request, not merely about the same person or thing.",
      },
      criteria: {
        true: { what: "Knowing it changes, informs or personalises the answer", examples: ["the person's school when asked about their schedule"] },
        false: { what: "True, but beside the point for this request", examples: ["a recipe someone saved, when asked about their ski race"] },
      },
    };
  }
  return { state, questions };
}

const p = (a) => (typeof a?.noul === "number" ? a.noul : typeof a?.probabilityTrue === "number" ? a.probabilityTrue : null);

/** Keep what Jev judged useful; without answers, only the high-confidence facts. */
export function select(facts, answers, { keepAt = 0.5, max = 12, fallbackMax = 8 } = {}) {
  if (answers && typeof answers === "object" && facts.some((f) => p(answers[`f${f.i}`]) !== null)) {
    return { source: "jev", kept: facts.map((f) => ({ ...f, p: p(answers[`f${f.i}`]) })).filter((f) => f.p !== null && f.p >= keepAt)
      .sort((a, b) => b.p - a.p).slice(0, max) };
  }
  return { source: "fallback", kept: facts.filter((f) => f.confidence === "high").slice(0, fallbackMax) };
}

export function render(kept, { budget = 2500 } = {}) {
  if (!kept.length) return "";
  const lines = [];
  let left = budget;
  for (const f of kept) {
    const line = `- ${f.confidence === "high" ? "" : "(unconfirmed) "}${f.text}`;
    if (line.length > left) break;
    lines.push(line);
    left -= line.length + 1;
  }
  return lines.length ? "## What your knowledge graph says\n" + lines.join("\n") +
    "\nUnconfirmed facts are leads, not facts: check before relying on them." : "";
}

/** The whole read path. `decide({purpose, state, questions})` is optional. */
// `passages`: file search hits to judge in the same request (re-ranking: a cheap search makes the shortlist,
// Jev decides what reaches the model). Returned as `passagesKept`. Without Jev, every passage is kept.
export async function recall({ graph, text, decide, timeoutMs = 2500, budget = 2500, passages = [] }) {
  const t0 = Date.now();
  const none0 = (extra = {}) => ({ context: "", entities: [], considered: 0, kept: [], passagesKept: [], source: "none", ms: Date.now() - t0, ...extra });
  const none = (extra = {}) => none0({ passagesKept: passages, ...extra });
  if (!String(text ?? "").trim()) return none();
  const matched = graph ? match(graph, text) : [];
  const kgFacts = matched.length ? candidates(graph, matched) : [];
  const files = passages.map((h) => ({ text: `${h.path}${h.head ? " — " + h.head : ""}: ${String(h.text).slice(0, 400)}`, kind: "file", hit: h }));
  const facts = [...kgFacts, ...files].map((f, i) => ({ ...f, i }));
  if (!facts.length) return none({ entities: matched.map((m) => m.id) });
  let answers = null, error = null;
  if (decide) {
    try {
      const out = await Promise.race([
        decide({ purpose: "potter-kg-recall", ...relevanceRequest(text, facts) }),
        new Promise((r) => setTimeout(() => r({ ok: false, reason: "timeout" }), timeoutMs)),
      ]);
      if (out?.ok) answers = out.answers; else error = out?.reason ?? "no answer";
    } catch (e) { error = e?.name ?? "error"; }
  }
  const s = select(facts.filter((f) => f.kind !== "file"), answers);
  const fileJudged = answers && files.length && facts.some((f) => f.kind === "file" && p(answers[`f${f.i}`]) !== null);
  const passagesKept = fileJudged
    ? facts.filter((f) => f.kind === "file" && (p(answers[`f${f.i}`]) ?? 0) >= 0.5).map((f) => f.hit)
    : passages;
  return { context: render(s.kept, { budget }), entities: matched.map((m) => m.id), considered: kgFacts.length,
    kept: s.kept, passagesConsidered: files.length, passagesKept, source: answers ? "jev" : s.source, ms: Date.now() - t0, ...(error ? { error } : {}) };
}
