// "Should these two entities be connected, and how?" as a typed decision. Pure: the decision call
// itself is injected, so this file knows nothing about endpoints, keys or the SDK.
import { PREDICATE_CRITERIA } from "./vocab.js";

/** OpenClaw's decision role calls a yes/no question "boolean"; the endpoint v2 called it "noul". */
export function buildConnectionQuestions({ booleanType = "boolean" } = {}) {
  return {
    related: {
      type: booleanType,
      instructions: "Are these two entities meaningfully related such that a typed edge belongs in the knowledge graph?",
      criteria: {
        true: "A real, specific relationship exists that software should model as an edge",
        false: "No meaningful or model-worthy relationship",
      },
    },
    predicate: {
      type: "choice",
      instructions: "Which predicate best captures the relationship between a and b?",
      criteria: PREDICATE_CRITERIA,
    },
  };
}

/** One entity with its direct neighbourhood, which is what makes the predicate choice accurate. */
export function entityForDecision(store, entity, excludeId, cap = 10) {
  const related = [];
  for (const r of store.relationsOf(entity.id)) {
    const otherId = r.source_id === entity.id ? r.target_id : r.source_id;
    if (otherId === excludeId) continue;
    related.push({ entity: store.getEntity(otherId)?.name ?? otherId, predicate: r.predicate });
    if (related.length >= cap) break;
  }
  return { id: entity.id, type: entity.type, name: entity.name, description: entity.description || "", related };
}

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Accepts both answer spellings: { probabilityTrue } (OpenClaw) and { noul } (v2 endpoint). */
export function normalizeConnectionAnswers(answers = {}) {
  const rel = answers.related ?? {};
  const pred = answers.predicate ?? {};
  const probabilities = pred.probabilities && typeof pred.probabilities === "object" ? pred.probabilities : null;
  return {
    related: num(rel.probabilityTrue) ?? num(rel.noul),
    predicate: typeof pred.choice === "string" ? pred.choice : null,
    predicateConfidence: num(pred.confidence) ?? (probabilities && typeof pred.choice === "string" ? num(probabilities[pred.choice]) : null),
    probabilities,
  };
}

/**
 * Suggest, never write. `decide({ purpose, state, questions })` resolves to
 * { ok: true, answers, model? } or { ok: false, reason }.
 */
export async function checkConnection(store, a, b, decide, opts = {}) {
  const A = store.lookup(a);
  if (!A) return { error: `could not resolve "${a}"` };
  const B = store.lookup(b);
  if (!B) return { error: `could not resolve "${b}"` };
  const ids = { a: { id: A.id, name: A.name }, b: { id: B.id, name: B.name } };
  if (A.id === B.id) return { ...ids, error: "both references resolve to the same entity" };
  if (typeof decide !== "function") return { ...ids, available: false, reason: "no decision model is configured" };
  let out;
  try {
    out = await decide({
      purpose: "potter-kg.check-connection",
      state: { a: entityForDecision(store, A, B.id), b: entityForDecision(store, B, A.id) },
      questions: buildConnectionQuestions(opts),
    });
  } catch (e) {
    out = { ok: false, reason: e?.message ?? String(e) };
  }
  if (!out?.ok) return { ...ids, available: false, reason: out?.reason ?? "decision unavailable" };
  return { ...ids, available: true, ...normalizeConnectionAnswers(out.answers), model: out.model ?? null };
}
