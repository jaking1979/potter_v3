// The knowledge graph's write path: what the owner says becomes typed, sourced facts.
// Pattern (docs.typesafe.ai: memory system, screen with a Noul first, SDE cascade, entity alignment):
//   1 screen    one Jev request per message: lasting fact? which kind? owner's own statement? clinical?
//   2 extract   a cheap model proposes facts in the controlled vocabulary, each with its exact source quote
//   3 check     one Jev request per fact, many narrow questions: supported by the quote? which link type
//               (structured Choice + "any fits")? does it replace an existing fact?
//   4 align     the fact's entities against the graph: exact names first; near matches get a 3-level Score
//   5 route     code decides: write (high if the owner said it), stage for the owner's review, or drop
// Pure: the store, decide() and extract() are passed in. Nothing here knows an endpoint, key or clock.
import { PREDICATE_DEFINITIONS, PREDICATES, ENTITY_TYPES } from "./vocab.js";
import { PREDICATE_CHOICE } from "./connection.js";

export const THRESH = { remember: 0.5, sensitive: 0.5, ownerStated: 0.7, supported: 0.7, reviewFloor: 0.3, replaces: 0.7, choice: 0.6 };
// A person lives at one place at a time, a device sits in one place: a new fact of these kinds replaces the old.
export const EXCLUSIVE = new Set(["lives_at", "located_in", "runs_on"]);

const yes = (a) => (typeof a?.noul === "number" ? a.noul : typeof a?.probabilityTrue === "number" ? a.probabilityTrue : null);

// ---------- 1. screen ----------

export function screenRequest(message, { owner, date }) {
  return {
    state: { message: String(message).slice(0, 2000), owner: owner ? { name: owner.name } : null, date },
    questions: {
      remember: {
        type: "noul",
        instructions: {
          question: "Does `message` state a fact worth remembering about the owner's life, people, home, work, plans or preferences?",
          focus: "A statement of fact, preference, plan or change. Not a question, a command to do something now, or chit-chat.",
        },
        criteria: {
          true: { what: "States something that will still matter in a week", examples: ["Mia switched to the U14 ski team", "We don't eat pork", "The plumber is Dave, 555-0100"] },
          false: { what: "Nothing lasting to remember", examples: ["What's the weather?", "Thanks!", "Send me the brief again"] },
        },
      },
      kind: {
        type: "choice",
        instructions: { question: "What kind of fact does `message` state?" },
        criteria: {
          durable: { what: "True for years: relationships, birthdays, where someone lives, what a device is", examples: ["Edie's birthday is April 16"] },
          preference: { what: "A like, dislike, habit or way the owner wants things done", examples: ["I prefer the aisle seat"] },
          situational: { what: "True for a while, then stops: a season, a project phase, a temporary arrangement", examples: ["Mia is applying to high schools this fall"] },
          one_off: { what: "About one moment only", examples: ["I'm running late today"] },
        },
      },
      owner_stated: {
        type: "noul",
        instructions: { question: "Is the owner stating this directly, in their own words, rather than quoting or guessing?" },
        criteria: { true: { what: "A direct statement by the owner" }, false: { what: "Forwarded text, a quote, a guess or a question" } },
      },
      sensitive: {
        type: "noul",
        instructions: { question: "Is `message` about clinical work, patients, therapy clients or someone's medical or mental-health details?" },
        criteria: { true: { what: "Clinical, patient or health information" }, false: { what: "Ordinary life, family, home or work logistics" } },
      },
    },
  };
}

export function readScreen(answers = {}) {
  const r = { remember: yes(answers.remember), ownerStated: yes(answers.owner_stated), sensitive: yes(answers.sensitive),
    kind: answers.kind?.choice ?? null, kindConfidence: answers.kind?.confidence ?? null };
  r.proceed = r.remember !== null && r.remember >= THRESH.remember && (r.sensitive ?? 0) < THRESH.sensitive && r.kind !== "one_off";
  return r;
}

// ---------- 2. extract (the prompt and schema for a cheap model) ----------

export function extractionPrompt(message, { owner, date, known = [] }) {
  const preds = PREDICATES.map((p) => `${p}: ${PREDICATE_DEFINITIONS[p]}`).join("\n");
  return [
    { role: "system", content:
      "You turn one message into knowledge-graph facts. Output JSON only, matching the schema. " +
      "Use ONLY these entity types: " + ENTITY_TYPES.join(", ") + ". Use ONLY these predicates (a <predicate> b):\n" + preds + "\n" +
      "Each fact needs `quote`: the exact words of the message that state it (copied, not paraphrased). " +
      "Refer to the owner as \"" + (owner?.name ?? "the owner") + "\" when the message says I, me or my. " +
      "Use a name from `known` when the message means that thing. Give `until` (YYYY-MM-DD) only when the message says when it stops. " +
      "No facts the message does not state. Fewer, correct facts beat many guesses. Nothing clinical, ever." },
    { role: "user", content: JSON.stringify({ date, message: String(message).slice(0, 2000), known: known.slice(0, 30) }) },
  ];
}

export const EXTRACTION_SCHEMA = {
  name: "kg_facts", strict: true,
  schema: {
    type: "object", additionalProperties: false, required: ["facts"],
    properties: {
      facts: {
        type: "array", maxItems: 8,
        items: {
          type: "object", additionalProperties: false, required: ["subject", "predicate", "object", "quote", "until"],
          properties: {
            subject: { type: "object", additionalProperties: false, required: ["name", "type", "description"],
              properties: { name: { type: "string" }, type: { type: "string", enum: ENTITY_TYPES }, description: { type: "string" } } },
            predicate: { type: "string", enum: PREDICATES },
            object: { type: "object", additionalProperties: false, required: ["name", "type", "description"],
              properties: { name: { type: "string" }, type: { type: "string", enum: ENTITY_TYPES }, description: { type: "string" } } },
            quote: { type: "string" },
            until: { type: "string" },
          },
        },
      },
    },
  },
};

/** Keep only facts that are well formed and whose quote really is in the message. */
export function cleanFacts(raw, message) {
  const m = String(message).toLowerCase().replace(/\s+/g, " ");
  const out = [];
  for (const f of raw?.facts ?? []) {
    if (!f || !PREDICATES.includes(f.predicate)) continue;
    if (!ENTITY_TYPES.includes(f.subject?.type) || !ENTITY_TYPES.includes(f.object?.type)) continue;
    if (!String(f.subject?.name ?? "").trim() || !String(f.object?.name ?? "").trim()) continue;
    const q = String(f.quote ?? "").toLowerCase().replace(/\s+/g, " ").trim();
    if (q.length < 3 || !m.includes(q)) continue;                    // a quote that is not in the message is invented
    out.push({ ...f, until: /^\d{4}-\d{2}-\d{2}$/.test(f.until ?? "") ? f.until : "" });
  }
  return out;
}

// ---------- 4. align entities ----------

export function slugify(name) {
  return String(name).toLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "x";
}

/** Exact name or alias of the same type wins; otherwise near matches (to ask Jev about) or a new id. */
export function alignCandidates(store, ent) {
  const exact = store.resolve(ent.name, { type: ent.type, limit: 3 });
  if (exact.length === 1) return { use: exact[0].id };
  if (exact.length > 1) return { ask: exact.slice(0, 3) };
  const near = store.search(ent.name, { type: ent.type, limit: 3 });
  if (near.length) return { ask: near };
  return { create: newId(store, ent) };
}

export function newId(store, ent) {
  const base = `${ent.type}:${slugify(ent.name)}`;
  if (!store.getEntity(base)) return base;
  for (let i = 2; i < 50; i++) if (!store.getEntity(`${base}-${i}`)) return `${base}-${i}`;
  return `${base}-${Date.now()}`;
}

export function alignQuestions(ent, candidates) {
  const q = {};
  candidates.forEach((c, i) => {
    q[`same_${i}`] = {
      type: "score",
      instructions: { question: `Is \`new_entity\` the same real thing as \`candidates[${i}]\`?` },
      criteria: [
        { level: "different", what: "Different things that share words in their names" },
        { level: "unsure", what: "Could be the same; not enough to tell" },
        { level: "same", what: "Clearly the same person, place or thing recorded twice" },
      ],
    };
  });
  return { state: { new_entity: { name: ent.name, type: ent.type, description: ent.description ?? "" },
    candidates: candidates.map((c) => ({ id: c.id, name: c.name, type: c.type, description: c.description ?? "", aliases: c.aliases ?? [] })) }, questions: q };
}

/** Same only when Jev is confident; unsure means a new entity, flagged in its provenance for the curator. */
export function readAlign(answers, candidates) {
  let best = null;
  candidates.forEach((c, i) => {
    const a = answers?.[`same_${i}`];
    if (typeof a?.score === "number" && a.score >= 1.5 && (a.confidence ?? 0) >= 0.6 && (!best || a.score > best.score)) best = { id: c.id, score: a.score };
  });
  return best;
}

// ---------- 3. check each fact ----------

export function checkRequest(fact, { message, existing = [] }) {
  return {
    state: { message: String(message).slice(0, 2000), fact: { subject: fact.subject.name, predicate: fact.predicate, object: fact.object.name, quote: fact.quote,
      reads: `${fact.subject.name} ${PREDICATE_DEFINITIONS[fact.predicate].replace(/^a /, "").replace(/ b\b.*$/, "")} ${fact.object.name}` },
      existing },
    questions: {
      supported: {
        type: "noul",
        instructions: { question: "Is `fact` stated or clearly implied by `fact.quote` in `message`?", focus: "Only what the message says; no outside assumptions." },
        criteria: { true: { what: "The quote says this" }, false: { what: "The quote does not say this, or says something different" } },
      },
      predicate: {
        type: "choice",
        instructions: { question: "Which relationship type best describes how `fact.subject` relates to `fact.object`, read as \"a <type> b\"?" },
        criteria: PREDICATE_CHOICE,
      },
      any_fits: { type: "noul", instructions: { question: "Does any listed relationship type describe how `fact.subject` relates to `fact.object`?" } },
      ...(existing.length ? {
        replaces: {
          type: "noul",
          instructions: { question: "Does `fact` replace or update one of the `existing` facts (the old one is no longer true)?" },
          criteria: { true: { what: "The old fact stopped being true", examples: ["moved to a new school", "a new plumber"] },
            false: { what: "Both can be true at once, or they are unrelated" } },
        },
      } : {}),
    },
  };
}

export function route(fact, answers, screen) {
  const supported = yes(answers?.supported);
  const anyFits = yes(answers?.any_fits);
  const replaces = yes(answers?.replaces);
  let predicate = fact.predicate;
  const ch = answers?.predicate;
  if (ch?.choice && ch.choice !== "none" && ch.choice !== predicate && (ch.confidence ?? 0) >= THRESH.choice && PREDICATES.includes(ch.choice)) predicate = ch.choice;
  const noFit = (ch?.choice === "none" && (ch.confidence ?? 0) >= THRESH.choice) || (anyFits !== null && anyFits < THRESH.reviewFloor);
  if (supported === null) return { action: "stage", predicate, why: "no check" };
  if (noFit || supported < THRESH.reviewFloor) return { action: "drop", predicate, why: noFit ? "no link type fits" : "not supported by the quote" };
  if (supported < THRESH.supported) return { action: "stage", predicate, why: "uncertain support" };
  const owner = (screen?.ownerStated ?? 0) >= THRESH.ownerStated;
  return { action: "write", predicate, confidence: owner ? "high" : "medium", replaces: replaces !== null && replaces >= THRESH.replaces };
}

// ---------- the whole message ----------

/**
 * deps: store (potter-kg store), decide({state, questions}) -> {ok, answers, costUsd}, extract(messages) -> {ok, json, costUsd}
 * Returns { written: [...], staged: [...], dropped: [...], screen, cost }.
 */
export async function captureMessage(item, { store, decide, extract, owner, date }) {
  const res = { written: [], staged: [], dropped: [], screen: null, cost: 0 };
  const add = (o) => { if (typeof o?.costUsd === "number") res.cost += o.costUsd; };
  const s = await decide(screenRequest(item.text, { owner, date })); add(s);
  if (!s?.ok) { res.error = `screen: ${s?.reason ?? "failed"}`; return res; }
  res.screen = readScreen(s.answers);
  if (!res.screen.proceed) return res;

  const known = [];
  for (const w of String(item.text).match(/\b[A-Z][\p{L}'-]+(?: [A-Z][\p{L}'-]+)*/gu) ?? []) {
    for (const e of store.resolve(w, { limit: 2 })) if (!known.some((k) => k.name === e.name)) known.push({ name: e.name, type: e.type });
  }
  if (owner && !known.some((k) => k.name === owner.name)) known.unshift({ name: owner.name, type: "person" });
  const x = await extract(extractionPrompt(item.text, { owner, date, known })); add(x);
  if (!x?.ok) { res.error = `extract: ${x?.reason ?? "failed"}`; return res; }
  const facts = cleanFacts(x.json, item.text);

  for (const f of facts) {
    const ids = {};
    for (const role of ["subject", "object"]) {
      const ent = f[role];
      if (owner && ent.type === "person" && ent.name === owner.name) { ids[role] = owner.id; continue; }
      const a = alignCandidates(store, ent);
      if (a.use) ids[role] = a.use;
      else if (a.create) ids[role] = { create: a.create, ent };
      else {
        const al = await decide(alignQuestions(ent, a.ask)); add(al);
        const best = al?.ok ? readAlign(al.answers, a.ask) : null;
        ids[role] = best ? best.id : { create: newId(store, ent), ent, unsure: a.ask.map((c) => c.id) };
      }
    }
    const subjId = typeof ids.subject === "string" ? ids.subject : ids.subject.create;
    const existing = typeof ids.subject === "string"
      ? store.relationsOf(subjId).filter((r) => r.source_id === subjId && (r.predicate === f.predicate || EXCLUSIVE.has(f.predicate)))
        .slice(0, 6).map((r) => `${store.getEntity(r.source_id)?.name} ${r.predicate.replace(/_/g, " ")} ${store.getEntity(r.target_id)?.name}`)
      : [];
    const c = await decide(checkRequest(f, { message: item.text, existing })); add(c);
    const r = route(f, c?.ok ? c.answers : null, res.screen);
    const entry = { fact: f, predicate: r.predicate, why: r.why, ids, quote: f.quote, kind: res.screen.kind, at: item.ts };
    if (r.action === "drop") { res.dropped.push(entry); continue; }
    if (r.action === "stage") { res.staged.push(entry); continue; }
    entry.confidence = r.confidence;
    entry.replaces = r.replaces;
    res.written.push(entry);
  }
  return res;
}

/** Apply one written (or owner-approved staged) entry to the store. */
export function applyEntry(store, entry, { source, confidence }) {
  const note = `${source}: "${String(entry.quote).slice(0, 200)}"${entry.fact.until ? ` until:${entry.fact.until}` : ""}${entry.kind ? ` kind:${entry.kind}` : ""}`;
  const idOf = (role) => {
    const v = entry.ids[role];
    if (typeof v === "string") return v;
    const e = v.ent;
    const prov = [note, ...(v.unsure ? [`possible duplicate of: ${v.unsure.join(", ")}`] : [])];
    store.upsertEntity({ id: v.create, name: e.name.trim(), description: e.description ?? "", provenance: prov, confidence: "medium" });
    return v.create;
  };
  const s = idOf("subject"), o = idOf("object");
  if (s === o) return { ok: false, error: "subject and object are the same" };
  const replaced = [];
  if (entry.replaces && EXCLUSIVE.has(entry.predicate)) {
    for (const r of store.relationsOf(s)) {
      if (r.source_id === s && r.predicate === entry.predicate && r.target_id !== o) {
        store.archiveRelation(r.source_id, r.predicate, r.target_id);
        replaced.push(r.target_id);
      }
    }
  }
  store.addRelation({ source_id: s, predicate: entry.predicate, target_id: o, confidence, provenance: [note] });
  return { ok: true, source_id: s, predicate: entry.predicate, target_id: o, replaced };
}
