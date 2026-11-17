// Alias handling and name resolution. Pure: works on plain entity objects.

/** Comparison key for a name or alias: trimmed, lower case, inner whitespace collapsed. */
export function aliasKey(s) {
  return String(s ?? "").trim().toLowerCase().replace(/\s+/g, " ");
}

/** Union of string lists, first spelling wins, compared by aliasKey; `exclude` keys are dropped. */
export function mergeLists(existing = [], incoming = [], exclude = []) {
  const seen = new Set(exclude.map(aliasKey));
  const out = [];
  for (const raw of [...existing, ...incoming]) {
    const text = String(raw ?? "").trim();
    const key = aliasKey(text);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(text);
  }
  return out;
}

// Best match first. Exact matches are what "resolve" means; the rest are search fallbacks.
export const MATCH_ORDER = ["exact-id", "name", "alias", "name-substring", "alias-substring", "description"];

/** How `query` matches `entity`, or null. */
export function matchKind(entity, query) {
  const q = aliasKey(query);
  if (!q) return null;
  if (aliasKey(entity.id) === q) return "exact-id";
  const name = aliasKey(entity.name);
  const aliases = (entity.aliases ?? []).map(aliasKey);
  if (name === q) return "name";
  if (aliases.includes(q)) return "alias";
  if (name.includes(q)) return "name-substring";
  if (aliases.some((a) => a.includes(q))) return "alias-substring";
  if (aliasKey(entity.description).includes(q)) return "description";
  return null;
}

/** Entities matching `query`, best match first; ties keep input order. */
export function rankMatches(entities, query, { type, limit = 10, exactOnly = false } = {}) {
  const hits = [];
  for (const e of entities) {
    if (type && e.type !== type) continue;
    const match = matchKind(e, query);
    if (!match) continue;
    if (exactOnly && MATCH_ORDER.indexOf(match) > MATCH_ORDER.indexOf("alias")) continue;
    hits.push({ ...e, match });
  }
  hits.sort((a, b) => MATCH_ORDER.indexOf(a.match) - MATCH_ORDER.indexOf(b.match));
  return hits.slice(0, Math.max(1, limit));
}

/**
 * Entities of the same type that share a name or alias: candidates for "these are one thing".
 * Returns [{ key, type, ids }] with ids sorted; each id set is reported once.
 */
export function findAliasConflicts(entities) {
  const byKey = new Map();
  for (const e of entities) {
    const keys = new Set([e.name, ...(e.aliases ?? [])].map(aliasKey).filter((k) => k.length >= 2));
    for (const k of keys) {
      const slot = `${e.type}\u0000${k}`;
      if (!byKey.has(slot)) byKey.set(slot, { key: k, type: e.type, ids: [] });
      byKey.get(slot).ids.push(e.id);
    }
  }
  const seen = new Set();
  const out = [];
  for (const c of byKey.values()) {
    if (c.ids.length < 2) continue;
    c.ids.sort();
    const sig = c.ids.join("|");
    if (seen.has(sig)) continue;
    seen.add(sig);
    out.push(c);
  }
  return out.sort((a, b) => a.ids.join("|").localeCompare(b.ids.join("|")));
}
