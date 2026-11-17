// The knowledge graph store: one plugin-owned SQLite file (node:sqlite, built into Node 24).
// No SDK imports. No global connection: every caller opens its own store and closes it.
// Nothing is ever deleted; entities and relations are archived.
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { findAliasConflicts, mergeLists, rankMatches } from "./alias.js";
import { assertValidEntity, assertValidRelation, strongerConfidence } from "./vocab.js";

// Same tables as v2 (schema version 1), so a v2 graph file opens unchanged.
export const SCHEMA_VERSION = "1";
const DDL = `
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  aliases     TEXT NOT NULL DEFAULT '[]',
  confidence  TEXT NOT NULL DEFAULT 'medium',
  status      TEXT NOT NULL DEFAULT 'active',
  provenance  TEXT NOT NULL DEFAULT '[]',
  created     TEXT NOT NULL,
  updated     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id  TEXT NOT NULL REFERENCES entities(id),
  predicate  TEXT NOT NULL,
  target_id  TEXT NOT NULL REFERENCES entities(id),
  confidence TEXT NOT NULL DEFAULT 'medium',
  status     TEXT NOT NULL DEFAULT 'active',
  provenance TEXT NOT NULL DEFAULT '[]',
  created    TEXT NOT NULL,
  updated    TEXT NOT NULL,
  UNIQUE(source_id, predicate, target_id)
);
CREATE INDEX IF NOT EXISTS idx_entities_type    ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_name    ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_status  ON entities(status);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS idx_relations_pred   ON relations(predicate);
`;

// A relation is "live" when it and both of its endpoints are active.
const LIVE = `
  SELECT r.* FROM relations r
  JOIN entities s ON s.id = r.source_id AND s.status = 'active'
  JOIN entities t ON t.id = r.target_id AND t.status = 'active'
  WHERE r.status = 'active'`;

function parseList(text) {
  try {
    const v = JSON.parse(text || "[]");
    return Array.isArray(v) ? v.map(String) : [];
  } catch {
    return [];
  }
}

function entityRow(row) {
  return row ? { ...row, aliases: parseList(row.aliases), provenance: parseList(row.provenance) } : null;
}

function relationRow(row) {
  return row ? { ...row, provenance: parseList(row.provenance) } : null;
}

export function summarize(e) {
  if (!e) return null;
  return {
    id: e.id, type: e.type, name: e.name, description: e.description || undefined,
    aliases: e.aliases, confidence: e.confidence, status: e.status, updated: e.updated,
  };
}

export function summarizeRelation(r) {
  return { source_id: r.source_id, predicate: r.predicate, target_id: r.target_id, confidence: r.confidence, status: r.status, created: r.created };
}

const clamp = (n, lo, hi, dflt) => (Number.isInteger(n) ? Math.min(hi, Math.max(lo, n)) : dflt);

export class KgStore {
  /** @param {string} path absolute file path, or ":memory:". @param {{ now?: () => Date }} [opts] */
  constructor(path, { now = () => new Date() } = {}) {
    if (!path) throw new Error("potter-kg: a database path is required");
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.file = path;
    this.now = now;
    this.db = new DatabaseSync(path);
    if (path !== ":memory:") this.db.exec("PRAGMA journal_mode=WAL;");
    this.db.exec("PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;");
    this.db.exec(DDL);
    const found = this.getMeta("schema_version");
    if (found === null) {
      this.setMeta("schema_version", SCHEMA_VERSION);
      this.setMeta("created", this.ts());
    } else if (found !== SCHEMA_VERSION) {
      this.db.close();
      throw new Error(`potter-kg: ${path} has schema version ${found}; this plugin reads version ${SCHEMA_VERSION}`);
    }
  }

  ts() { return this.now().toISOString(); }

  close() {
    if (!this.db) return;
    try { this.db.close(); } catch { /* already closed */ }
    this.db = null;
  }

  transaction(fn) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const out = fn();
      this.db.exec("COMMIT");
      return out;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }

  getMeta(key) {
    const row = this.db.prepare("SELECT value FROM meta WHERE key=?").get(key);
    return row ? row.value : null;
  }

  setMeta(key, value) {
    this.db.prepare("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(key, String(value));
  }

  // ---- entities -------------------------------------------------------------------------------

  getEntity(id) {
    return entityRow(this.db.prepare("SELECT * FROM entities WHERE id=?").get(String(id ?? "")));
  }

  activeEntities() {
    return this.db.prepare("SELECT * FROM entities WHERE status='active' ORDER BY updated DESC, id").all().map(entityRow);
  }

  /**
   * Insert, or merge into the existing record. A field left out keeps its stored value; aliases and
   * provenance are unioned, never replaced (v2 wiped them when a caller left them out). An upsert
   * makes an archived entity active again unless `status` says otherwise.
   */
  upsertEntity(input) {
    const { type } = assertValidEntity(input);
    const old = this.getEntity(input.id);
    const name = input.name.trim();
    const ts = this.ts();
    const next = {
      description: input.description?.trim() ? input.description.trim() : old?.description ?? "",
      aliases: mergeLists(old?.aliases, input.aliases, [name]),
      confidence: input.confidence ?? old?.confidence ?? "medium",
      status: input.status ?? "active",
      provenance: mergeLists(old?.provenance, input.provenance),
    };
    this.db.prepare(`
      INSERT INTO entities (id, type, name, description, aliases, confidence, status, provenance, created, updated)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, aliases=excluded.aliases,
        confidence=excluded.confidence, status=excluded.status, provenance=excluded.provenance, updated=excluded.updated
    `).run(input.id, type, name, next.description, JSON.stringify(next.aliases), next.confidence, next.status,
      JSON.stringify(next.provenance), ts, ts);
    return this.getEntity(input.id);
  }

  archiveEntity(id, note) {
    const e = this.getEntity(id);
    if (!e) return { ok: false, error: `no entity ${id}` };
    const provenance = note ? mergeLists(e.provenance, [note]) : e.provenance;
    this.db.prepare("UPDATE entities SET status='archived', provenance=?, updated=? WHERE id=?").run(JSON.stringify(provenance), this.ts(), id);
    return { ok: true };
  }

  // ---- relations ------------------------------------------------------------------------------

  getRelation(source_id, predicate, target_id) {
    return relationRow(this.db.prepare("SELECT * FROM relations WHERE source_id=? AND predicate=? AND target_id=?").get(source_id, predicate, target_id));
  }

  getRelationById(id) {
    return relationRow(this.db.prepare("SELECT * FROM relations WHERE id=?").get(id));
  }

  /** Upsert a relation between two active entities. Provenance is unioned; re-adding revives it. */
  addRelation(input) {
    assertValidRelation(input);
    for (const [role, id] of [["source", input.source_id], ["target", input.target_id]]) {
      const e = this.getEntity(id);
      if (!e) throw new Error(`relation ${role} entity not found: ${id}`);
      if (e.status !== "active") throw new Error(`relation ${role} entity is archived: ${id}`);
    }
    const old = this.getRelation(input.source_id, input.predicate, input.target_id);
    const ts = this.ts();
    this.db.prepare(`
      INSERT INTO relations (source_id, predicate, target_id, confidence, status, provenance, created, updated)
      VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
      ON CONFLICT(source_id, predicate, target_id) DO UPDATE SET
        confidence=excluded.confidence, provenance=excluded.provenance, status='active', updated=excluded.updated
    `).run(input.source_id, input.predicate, input.target_id, input.confidence ?? old?.confidence ?? "medium",
      JSON.stringify(mergeLists(old?.provenance, input.provenance)), ts, ts);
    return { ok: true, source_id: input.source_id, predicate: input.predicate, target_id: input.target_id };
  }

  archiveRelation(source_id, predicate, target_id) {
    const r = this.db.prepare("UPDATE relations SET status='archived', updated=? WHERE source_id=? AND predicate=? AND target_id=?")
      .run(this.ts(), source_id, predicate, target_id);
    return { ok: r.changes > 0 };
  }

  setRelationConfidence(id, confidence, note) {
    const r = this.getRelationById(id);
    if (!r) return { ok: false, error: `no relation ${id}` };
    assertValidRelation({ ...r, confidence });
    this.db.prepare("UPDATE relations SET confidence=?, provenance=?, updated=? WHERE id=?")
      .run(confidence, JSON.stringify(note ? mergeLists(r.provenance, [note]) : r.provenance), this.ts(), id);
    return { ok: true };
  }

  /** Live relations touching an entity, either direction, newest first. */
  relationsOf(id) {
    return this.db.prepare(`${LIVE} AND (r.source_id=? OR r.target_id=?) ORDER BY r.created DESC, r.id DESC`).all(id, id).map(relationRow);
  }

  // ---- search ---------------------------------------------------------------------------------

  /** Exact matches only (id, name, alias): turns "what the owner calls it" into an entity. */
  resolve(query, { type, limit = 5 } = {}) {
    return rankMatches(this.activeEntities(), query, { type, limit: clamp(limit, 1, 100, 5), exactOnly: true });
  }

  /** Exact matches first, then substrings of name, alias and description. */
  search(query, { type, limit = 10 } = {}) {
    return rankMatches(this.activeEntities(), query, { type, limit: clamp(limit, 1, 100, 10) });
  }

  /** An entity by id, or else the best exact-or-fuzzy match. */
  lookup(ref) {
    const direct = this.getEntity(String(ref ?? "").trim());
    if (direct && direct.status === "active") return direct;
    return this.search(ref, { limit: 1 })[0] ?? null;
  }

  // ---- traversal ------------------------------------------------------------------------------

  /** Breadth-first neighbourhood: active entities within maxHops, and the live relations among them. */
  neighbors(id, { maxHops = 1, maxNodes = 200 } = {}) {
    const root = this.getEntity(id);
    if (!root) return { ok: false, error: `no entity ${id}` };
    const hops = clamp(maxHops, 1, 3, 1);
    const cap = clamp(maxNodes, 1, 500, 200);
    const nodes = new Map([[id, root]]);
    const seenEdges = new Map();
    let frontier = [id];
    let truncated = false;
    for (let hop = 0; hop < hops && frontier.length; hop++) {
      const next = [];
      for (const from of frontier) {
        for (const r of this.relationsOf(from)) {
          const other = r.source_id === from ? r.target_id : r.source_id;
          if (!nodes.has(other)) {
            if (nodes.size >= cap) { truncated = true; continue; }
            nodes.set(other, this.getEntity(other));
            next.push(other);
          }
          seenEdges.set(r.id, r);
        }
      }
      frontier = next;
    }
    const edges = [...seenEdges.values()].filter((r) => nodes.has(r.source_id) && nodes.has(r.target_id));
    return {
      ok: true,
      root: summarize(root),
      nodes: [...nodes.values()].map(summarize),
      edges: edges.map(summarizeRelation),
      stats: { nodes: nodes.size, edges: edges.length, hops, truncated },
    };
  }

  /** Shortest undirected path over live relations; `steps` names the relation used at each hop. */
  path(fromId, toId, { maxDepth = 6 } = {}) {
    for (const id of [fromId, toId]) if (!this.getEntity(id)) return { ok: false, error: `no entity ${id}` };
    const depthCap = clamp(maxDepth, 1, 10, 6);
    if (fromId === toId) return { ok: true, found: true, depth: 0, path: [fromId], steps: [] };
    const via = new Map([[fromId, null]]);
    let frontier = [fromId];
    for (let depth = 1; depth <= depthCap && frontier.length; depth++) {
      const next = [];
      for (const head of frontier) {
        for (const r of this.relationsOf(head)) {
          const other = r.source_id === head ? r.target_id : r.source_id;
          if (via.has(other)) continue;
          via.set(other, { from: head, relation: r });
          if (other === toId) {
            const ids = [toId];
            const steps = [];
            for (let cur = toId; via.get(cur); cur = via.get(cur).from) {
              const { from, relation } = via.get(cur);
              ids.unshift(from);
              steps.unshift({ source_id: relation.source_id, predicate: relation.predicate, target_id: relation.target_id });
            }
            return { ok: true, found: true, depth, path: ids, steps };
          }
          next.push(other);
        }
      }
      frontier = next;
    }
    return { ok: true, found: false, depth: depthCap };
  }

  /** A bounded picture around one entity plus what changed lately. */
  context(id, { maxHops = 2, maxNodes = 60, recentDays = 7 } = {}) {
    const sub = this.neighbors(id, { maxHops, maxNodes });
    if (!sub.ok) return sub;
    const since = new Date(this.now().getTime() - clamp(recentDays, 1, 365, 7) * 86400000).toISOString();
    const recent = this.db.prepare(`
      SELECT 'entity' AS kind, id AS ref, name AS label, updated FROM entities WHERE status='active' AND updated >= ?
      UNION ALL
      SELECT 'relation', cast(id AS TEXT), source_id || ' ' || predicate || ' ' || target_id, updated
        FROM relations WHERE status='active' AND updated >= ?
      ORDER BY updated DESC LIMIT 10
    `).all(since, since);
    const s = this.stats();
    return { ok: true, root: sub.root, subgraph: sub, recent, stats: { totalEntities: s.entities, totalRelations: s.relations } };
  }

  /** Provenance: the entity's own record and every live relation with where it came from. */
  sourceView(id) {
    const e = this.getEntity(id);
    if (!e) return { ok: false, error: `no entity ${id}` };
    return {
      ok: true,
      entity: e,
      relations: this.relationsOf(id).map((r) => ({ ...summarizeRelation(r), provenance: r.provenance, updated: r.updated })),
    };
  }

  // ---- health ---------------------------------------------------------------------------------

  orphans() {
    return this.db.prepare(`
      SELECT e.* FROM entities e WHERE e.status='active' AND NOT EXISTS (
        SELECT 1 FROM (${LIVE}) r WHERE r.source_id=e.id OR r.target_id=e.id
      ) ORDER BY e.updated, e.id
    `).all().map(entityRow);
  }

  aliasConflicts() {
    return findAliasConflicts(this.activeEntities());
  }

  /** Live relations that are not yet high confidence, weakest and oldest first. */
  weakRelations() {
    return this.db.prepare(`${LIVE} AND r.confidence IN ('low','medium')
      ORDER BY CASE r.confidence WHEN 'low' THEN 0 ELSE 1 END, r.updated, r.id`).all().map(relationRow);
  }

  stats({ orphanLimit = 100 } = {}) {
    const one = (sql) => this.db.prepare(sql).get().c;
    const orphans = this.orphans().map((e) => e.id);
    return {
      ok: true,
      entities: one("SELECT count(*) c FROM entities WHERE status='active'"),
      relations: one(`SELECT count(*) c FROM (${LIVE})`),
      archivedEntities: one("SELECT count(*) c FROM entities WHERE status='archived'"),
      byType: this.db.prepare("SELECT type, count(*) c FROM entities WHERE status='active' GROUP BY type ORDER BY c DESC, type").all(),
      byPredicate: this.db.prepare(`SELECT predicate, count(*) c FROM (${LIVE}) GROUP BY predicate ORDER BY c DESC, predicate`).all(),
      orphanCount: orphans.length,
      orphans: orphans.slice(0, orphanLimit),
    };
  }

  // ---- merge ----------------------------------------------------------------------------------

  /**
   * Fold `dupId` into `keepId`: its live relations are re-pointed at the kept entity, its name and
   * aliases become aliases there, and it is archived with a "merged-into" note. v2 only archived the
   * duplicate, which silently dropped every relation it had. One transaction; nothing is deleted.
   */
  mergeEntities(keepId, dupId, { note = "curator-merge" } = {}) {
    if (keepId === dupId) throw new Error("cannot merge an entity into itself");
    const keep = this.getEntity(keepId);
    const dup = this.getEntity(dupId);
    if (!keep || keep.status !== "active") throw new Error(`merge target is missing or archived: ${keepId}`);
    if (!dup || dup.status !== "active") throw new Error(`merge source is missing or archived: ${dupId}`);
    return this.transaction(() => {
      let moved = 0;
      for (const r of this.relationsOf(dupId)) {
        const source_id = r.source_id === dupId ? keepId : r.source_id;
        const target_id = r.target_id === dupId ? keepId : r.target_id;
        this.archiveRelation(r.source_id, r.predicate, r.target_id);
        if (source_id === target_id) continue;
        const existing = this.getRelation(source_id, r.predicate, target_id);
        const confidence = existing?.status === "active" ? strongerConfidence(existing.confidence, r.confidence) : r.confidence;
        this.addRelation({ source_id, predicate: r.predicate, target_id, confidence, provenance: mergeLists(r.provenance, [note]) });
        moved++;
      }
      this.upsertEntity({
        id: keepId, name: keep.name, aliases: [dup.name, ...dup.aliases], provenance: mergeLists(dup.provenance, [note]),
        description: keep.description || dup.description,
      });
      this.archiveEntity(dupId, `merged-into:${keepId}`);
      return { ok: true, kept: keepId, archived: dupId, relationsMoved: moved };
    });
  }
}

export function openStore(path, opts) {
  return new KgStore(path, opts);
}
