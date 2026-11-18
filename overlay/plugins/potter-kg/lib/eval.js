// Connection eval: how well a decision model recognises the links already in a real graph.
// Two question designs are asked about the same pairs, so the difference is the design, not the sample:
//   v2  the question v2 shipped: one broad "related?" yes/no plus a flat predicate choice
//   v3  the TypeSafe way (docs.typesafe.ai: How to build with System One): many narrow questions in one
//       request, structured criteria (what / not_for / examples), the owner's context in the state, one
//       yes/no per candidate predicate ("is this statement true of a and b?"), composed in code.
// Pure: rows in, questions out, answers in, metrics out. No database, network or clock.
import { PREDICATE_DEFINITIONS } from "./vocab.js";

// Guidance per predicate. Examples are generic on purpose: nothing here describes a real person or place.
export const PREDICATE_GUIDE = {
  lives_at: { what: "a person has their home at a place", examples: ["a family member lives at the city apartment"] },
  works_on: { what: "a person actively builds, runs or contributes to a project or thing", examples: ["the owner works on a side app"] },
  interested_in: { what: "a person cares about, follows or does b as a hobby or interest", not_for: "a project the person works on",
    examples: ["the owner is interested in skiing", "a child is interested in soccer"] },
  filed_as: { what: "an item is filed or archived under a category", examples: ["a recipe filed as baking"] },
  references: { what: "a document or resource cites or points to b", examples: ["an article references a sleep method"] },
  connects_to: { what: "a general, real link that no more specific predicate captures", not_for: "a link a specific predicate fits" },
  captured_from: { what: "a was extracted or saved from source b", examples: ["a recipe captured from a cooking site"] },
  decided_on: { what: "a person or group made a decision about b", examples: ["the owner decided on a heat pump"] },
  serves: { what: "a provides a function or service for b", examples: ["a script serves the morning brief"] },
  is_part_of: { what: "a is a component, member or section of b", examples: ["a room is part of a house"] },
  supersedes: { what: "a replaces or outdates b", examples: ["a new rule supersedes an old rule"] },
  contradicts: { what: "a conflicts with b", examples: ["two notes give different dates for one event"] },
  runs_on: { what: "software a runs on hardware or platform b", examples: ["an assistant runs on a mini PC"] },
  integrates_with: { what: "system a exchanges data or works together with system b", examples: ["an assistant integrates with a calendar"] },
  controls: { what: "a operates or switches b", examples: ["a hub controls the thermostat"] },
  accessed_by: { what: "a is used or reached by b", examples: ["a shared inbox accessed by the assistant"] },
  uses: { what: "a makes use of tool, service or resource b", examples: ["a project uses a database"] },
  depends_on: { what: "a cannot work without b", examples: ["a job depends on an API key"] },
  runs: { what: "a operates or hosts b", examples: ["a server runs the gateway"] },
  candidate_host_for: { what: "a is being considered as the machine for b", examples: ["a spare laptop as candidate host for a model"] },
  located_in: { what: "a is physically or administratively in place b", examples: ["a device located in the cabin"] },
  monitors: { what: "a watches b and reports on it", examples: ["a sensor monitors a leak point"] },
  married_to: { what: "two people are spouses", examples: [] },
  parent_of: { what: "person a is a parent of person b", examples: [] },
  prefers: { what: "a person likes, enjoys or prefers b (a food, place, brand, way of doing things)", not_for: "a hobby the person does",
    examples: ["the owner prefers Pinot Noir", "a child prefers the window seat"] },
  avoids: { what: "a person dislikes, avoids or cannot have b", examples: ["a family member avoids shellfish"] },
  responsible_for: { what: "a person handles or owns b (a chore, a system, a duty)", examples: ["the owner is responsible for winterizing the cabin"] },
  has_pattern: { what: "a person shows a recurring pattern b, learned from how their days actually went", examples: ["the owner: errands slip on packed days"] },
};

// ---------- sampling (deterministic: a seed, not Math.random) ----------

export function rng(seed = 1) {
  let s = seed >>> 0 || 1;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}

function shuffle(xs, r) {
  const a = [...xs];
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}

/**
 * Positives: live edges, at most `pairs`, spread across predicates (round robin) so a common predicate
 * cannot hide a weak one. Negatives: random pairs with no edge either way and no shared neighbour.
 */
export function sample({ entities, relations, pairs = 200, negatives = 60, seed = 7 }) {
  const r = rng(seed);
  const live = new Map(entities.filter((e) => e.status === "active").map((e) => [e.id, e]));
  const edges = relations.filter((x) => x.status === "active" && live.has(x.source_id) && live.has(x.target_id));
  const byPred = new Map();
  for (const e of shuffle(edges, r)) { if (!byPred.has(e.predicate)) byPred.set(e.predicate, []); byPred.get(e.predicate).push(e); }
  const pos = [];
  const queues = [...byPred.values()];
  while (pos.length < pairs && queues.some((q) => q.length)) {
    for (const q of queues) if (q.length && pos.length < pairs) pos.push(q.shift());
  }
  const nbr = new Map();
  const add = (a, b) => { if (!nbr.has(a)) nbr.set(a, new Set()); nbr.get(a).add(b); };
  for (const e of edges) { add(e.source_id, e.target_id); add(e.target_id, e.source_id); }
  const ids = [...live.keys()];
  const neg = [];
  const seen = new Set();
  for (let tries = 0; neg.length < negatives && tries < negatives * 200 && ids.length > 1; tries++) {
    const a = ids[Math.floor(r() * ids.length)], b = ids[Math.floor(r() * ids.length)];
    const key = [a, b].sort().join("|");
    if (a === b || seen.has(key)) continue;
    const na = nbr.get(a) ?? new Set(), nb = nbr.get(b) ?? new Set();
    if (na.has(b) || [...na].some((x) => nb.has(x))) continue;
    seen.add(key);
    neg.push({ a, b });
  }
  return [
    ...pos.map((e) => ({ a: e.source_id, b: e.target_id, expectRelated: true, expectPredicate: e.predicate })),
    ...neg.map((p) => ({ ...p, expectRelated: false, expectPredicate: "none" })),
  ];
}

/** The owner: the person with the most live edges (a graph is someone's). Overridable by the caller. */
export function findOwner(entities, relations) {
  const deg = new Map();
  for (const r of relations) if (r.status === "active") for (const id of [r.source_id, r.target_id]) deg.set(id, (deg.get(id) ?? 0) + 1);
  const people = entities.filter((e) => e.type === "person" && e.status === "active");
  people.sort((x, y) => (deg.get(y.id) ?? 0) - (deg.get(x.id) ?? 0));
  return people[0] ?? null;
}

/** One entity as the model sees it: its record and up to `cap` neighbours, never the other entity of the pair. */
export function describe(entity, { neighbours, nameOf, excludeId, cap = 10 }) {
  const related = [];
  for (const r of neighbours(entity.id)) {
    const other = r.source_id === entity.id ? r.target_id : r.source_id;
    if (other === excludeId) continue;
    related.push(r.source_id === entity.id ? { predicate: r.predicate, to: nameOf(other) } : { predicate: r.predicate, from: nameOf(other) });
    if (related.length >= cap) break;
  }
  return { id: entity.id, type: entity.type, name: entity.name, description: entity.description || "", related };
}

// ---------- question designs ----------

export function v2Questions() {
  return {
    related: {
      type: "noul",
      instructions: "Are these two entities meaningfully related such that a typed edge belongs in the knowledge graph?",
      criteria: { true: "A real, specific relationship exists that software should model as an edge", false: "No meaningful or model-worthy relationship" },
    },
    predicate: { type: "choice", instructions: "Which predicate best captures the relationship between a and b?",
      criteria: { ...PREDICATE_DEFINITIONS, none: "no meaningful relationship" } },
  };
}

export function v3Questions() {
  const q = {
    direct_link: {
      type: "noul",
      instructions: {
        question: "Do `a` and `b` have a specific, direct relationship?",
        context: "`graph` is the owner's personal knowledge graph. An entity is in it because it matters to the owner.",
        focus: "Judge from the two records, their neighbours and common sense.",
      },
      criteria: {
        true: { what: "One specific relationship holds between them", examples: ["the owner and a hobby the graph records", "a device and the place it is in"] },
        false: { what: "No direct relationship; at most a loose theme in common", not_for: "a real link that is merely unstated in the records",
          examples: ["a dessert recipe and a home server", "a child and an unrelated trading tool"] },
      },
    },
    predicate: {
      type: "choice",
      instructions: { question: "Which relationship type best describes how `a` relates to `b`, read as \"a <type> b\"?" },
      criteria: { ...Object.fromEntries(Object.entries(PREDICATE_GUIDE).map(([k, v]) => [k, v])),
        none: { what: "no listed type describes a direct relationship" } },
    },
    any_fits: {
      type: "noul",
      instructions: { question: "Does any listed relationship type describe how `a` relates to `b`?", types: Object.keys(PREDICATE_GUIDE) },
    },
  };
  for (const [p, g] of Object.entries(PREDICATE_GUIDE)) {
    q[`is_${p}`] = {
      type: "noul",
      instructions: { question: "Is this statement true of `a` and `b`?", statement: PREDICATE_DEFINITIONS[p], meaning: g.what,
        ...(g.not_for ? { not_for: g.not_for } : {}) },
    };
  }
  return q;
}

export function v3State(pair, { owner }) {
  return {
    graph: { owner: owner ? { id: owner.id, name: owner.name } : null, note: "Personal knowledge graph of the owner's life, home and work." },
    a: pair.a, b: pair.b,
  };
}

// ---------- reading answers ----------

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const yes = (a) => num(a?.noul) ?? num(a?.probabilityTrue);

export function readV2(answers = {}) {
  const pr = answers.predicate?.probabilities ?? null;
  return { related: yes(answers.related), predicate: answers.predicate?.choice ?? null, ranked: rank(pr) };
}

/** Composition in code: related = the strongest of the direct-link check and the best predicate statement. */
export function readV3(answers = {}) {
  const per = {};
  for (const p of Object.keys(PREDICATE_GUIDE)) { const v = yes(answers[`is_${p}`]); if (v !== null) per[p] = v; }
  const ranked = rank(per);
  const best = ranked.length ? per[ranked[0]] : null;
  const direct = yes(answers.direct_link);
  const parts = [direct, best].filter((v) => v !== null);
  return {
    related: parts.length ? Math.max(...parts) : null,
    direct, bestStatement: best, anyFits: yes(answers.any_fits),
    predicate: ranked[0] ?? null, ranked,
    choice: answers.predicate?.choice ?? null, choiceConfidence: num(answers.predicate?.confidence),
    choiceRanked: rank(answers.predicate?.probabilities),
  };
}

function rank(probs) {
  if (!probs || typeof probs !== "object") return [];
  return Object.entries(probs).filter(([k, v]) => k !== "none" && num(v) !== null).sort((x, y) => y[1] - x[1]).map(([k]) => k);
}

// ---------- metrics ----------

export function metrics(rows, { field = "related", yesAt = 0.7, noAt = 0.3, mid = 0.5, ranked = "ranked", top = "predicate" } = {}) {
  const ok = rows.filter((r) => num(r[field]) !== null);
  const P = ok.filter((r) => r.expectRelated), N = ok.filter((r) => !r.expectRelated);
  const at = (t) => {
    const tp = P.filter((r) => r[field] >= t).length, fp = N.filter((r) => r[field] >= t).length;
    const fn = P.length - tp;
    const precision = tp + fp ? tp / (tp + fp) : null, recall = P.length ? tp / P.length : null;
    return { tp, fp, fn, tn: N.length - fp, precision: r3(precision), recall: r3(recall),
      f1: precision && recall ? r3((2 * precision * recall) / (precision + recall)) : 0 };
  };
  const band = ok.filter((r) => r[field] > noAt && r[field] < yesAt).length;
  const hits = P.filter((r) => r[field] >= mid);
  const top1 = hits.filter((r) => r[top] === r.expectPredicate).length;
  const top3 = hits.filter((r) => (r[ranked] ?? []).slice(0, 3).includes(r.expectPredicate)).length;
  return {
    answered: ok.length, unanswered: rows.length - ok.length, positives: P.length, negatives: N.length,
    at05: at(mid), at07: at(yesAt), reviewBand: r3(ok.length ? band / ok.length : null),
    predicateTop1: r3(hits.length ? top1 / hits.length : null), predicateTop3: r3(hits.length ? top3 / hits.length : null),
  };
}

/** Recall per predicate, weakest first: where the design fails. */
export function recallByPredicate(rows, { field = "related", at = 0.5 } = {}) {
  const m = new Map();
  for (const r of rows) {
    if (!r.expectRelated || num(r[field]) === null) continue;
    const x = m.get(r.expectPredicate) ?? { n: 0, hit: 0 };
    x.n++; if (r[field] >= at) x.hit++;
    m.set(r.expectPredicate, x);
  }
  return [...m.entries()].map(([p, x]) => ({ predicate: p, n: x.n, recall: r3(x.hit / x.n) })).sort((a, b) => a.recall - b.recall);
}

const r3 = (v) => (v === null || v === undefined ? null : Math.round(v * 1000) / 1000);
