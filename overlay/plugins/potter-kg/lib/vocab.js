// Controlled vocabulary of the knowledge graph, and the checks every write goes through.
// Pure: no SDK, no database, no I/O.

export const ENTITY_TYPES = [
  "person", "place", "project", "interest", "resource", "event", "decision",
  "automation", "concept", "device", "system", "recipe", "pattern",
];

// Each predicate reads "a <predicate> b". The text is the rubric a decision model sees.
export const PREDICATE_DEFINITIONS = {
  lives_at: "a resides at b (a person lives at a place)",
  works_on: "a works on b (a person works on a project or thing)",
  interested_in: "a has an interest in b",
  filed_as: "a is filed or archived under b",
  references: "a references or cites b",
  connects_to: "generic link between a and b",
  captured_from: "a was captured or extracted from source b",
  decided_on: "a made a decision about b",
  serves: "a provides a function for b",
  is_part_of: "a is part of b",
  supersedes: "a replaces or outdates b",
  contradicts: "a conflicts with b",
  runs_on: "a (software) runs on b (hardware or system)",
  integrates_with: "a integrates with b",
  controls: "a controls b",
  accessed_by: "a is accessed by b",
  uses: "a uses b",
  depends_on: "a depends on b",
  runs: "a runs or operates b",
  candidate_host_for: "a is a candidate host for b",
  located_in: "a is located in b",
  monitors: "a monitors b",
  married_to: "a is married to b",
  parent_of: "a is parent of b",
  prefers: "a (a person) likes or prefers b",
  avoids: "a (a person) dislikes, avoids or cannot have b",
  responsible_for: "a (a person) is responsible for b (a chore, system or duty)",
  has_pattern: "a (a person) shows recurring pattern b in how their days go",
};

export const PREDICATES = Object.keys(PREDICATE_DEFINITIONS);
export const PREDICATE_CRITERIA = { ...PREDICATE_DEFINITIONS, none: "no meaningful relationship" };

export const CONFIDENCE_LEVELS = ["high", "medium", "low"];
export const STATUSES = ["active", "archived"];

const SLUG = /^[a-z0-9][a-z0-9._-]*$/;

/** "person:ada" -> { type: "person", slug: "ada" }, or null when the id is not well formed. */
export function parseEntityId(id) {
  if (typeof id !== "string") return null;
  const i = id.indexOf(":");
  if (i < 1) return null;
  const type = id.slice(0, i);
  const slug = id.slice(i + 1);
  if (!ENTITY_TYPES.includes(type) || !SLUG.test(slug) || id.length > 200) return null;
  return { type, slug };
}

export function assertValidEntity(e) {
  const parsed = parseEntityId(e?.id);
  if (!parsed) {
    throw new Error(`entity.id must look like "<type>:<slug>" (lower case, type one of: ${ENTITY_TYPES.join(", ")}); got ${JSON.stringify(e?.id)}`);
  }
  if (e.type !== undefined && e.type !== parsed.type) {
    throw new Error(`entity ${e.id}: type "${e.type}" does not match the id prefix "${parsed.type}"`);
  }
  if (typeof e.name !== "string" || !e.name.trim()) throw new Error(`entity ${e.id}: name is required`);
  if (e.confidence !== undefined && !CONFIDENCE_LEVELS.includes(e.confidence)) {
    throw new Error(`entity ${e.id}: confidence must be high|medium|low`);
  }
  if (e.status !== undefined && !STATUSES.includes(e.status)) throw new Error(`entity ${e.id}: status must be active|archived`);
  for (const key of ["aliases", "provenance"]) {
    if (e[key] !== undefined && !(Array.isArray(e[key]) && e[key].every((x) => typeof x === "string"))) {
      throw new Error(`entity ${e.id}: ${key} must be a list of strings`);
    }
  }
  return parsed;
}

export function assertValidRelation(r) {
  if (typeof r?.source_id !== "string" || typeof r?.target_id !== "string") {
    throw new Error("relation requires source_id and target_id");
  }
  const label = `relation ${r.source_id} ${r.predicate} ${r.target_id}`;
  if (!PREDICATES.includes(r.predicate)) {
    throw new Error(`${label}: predicate must be one of: ${PREDICATES.join(", ")}`);
  }
  if (r.source_id === r.target_id) throw new Error(`${label}: an entity cannot relate to itself`);
  if (r.confidence !== undefined && !CONFIDENCE_LEVELS.includes(r.confidence)) {
    throw new Error(`${label}: confidence must be high|medium|low`);
  }
  if (r.provenance !== undefined && !(Array.isArray(r.provenance) && r.provenance.every((x) => typeof x === "string"))) {
    throw new Error(`${label}: provenance must be a list of strings`);
  }
  return true;
}

/** The stronger of two confidence levels (unknown values count as weakest). */
export function strongerConfidence(a, b) {
  const rank = (c) => { const i = CONFIDENCE_LEVELS.indexOf(c); return i < 0 ? CONFIDENCE_LEVELS.length : i; };
  return rank(a) <= rank(b) ? a : b;
}
