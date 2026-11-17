// Agent tools over a KgStore. No SDK imports: parameters are plain JSON Schema, so this file loads
// under `node --test` and the plugin needs no dependency install.
import { checkConnection } from "./connection.js";
import { ENTITY_TYPES, PREDICATES } from "./vocab.js";

function ok(value) {
  return { content: [{ type: "text", text: JSON.stringify(value, null, 2) }], details: value };
}
function fail(e) {
  const message = e?.message ?? String(e);
  return { content: [{ type: "text", text: `Error: ${message}` }], details: { ok: false, error: message } };
}

const obj = (properties, required = []) => ({ type: "object", additionalProperties: false, properties, required });
const entityId = { type: "string", minLength: 3, description: "Entity id '<type>:<slug>', for example 'concept:knowledge-graphs'." };
const confidence = { type: "string", enum: ["high", "medium", "low"] };
const strings = (description) => ({ type: "array", items: { type: "string" }, description });
const int = (description, minimum, maximum, dflt) => ({ type: "integer", description, minimum, maximum, default: dflt });

export const TOOL_NAMES = ["kg_search", "kg_neighbors", "kg_path", "kg_context", "kg_source", "kg_add", "kg_stats", "kg_check_connection"];

/**
 * @param {{ store: import("./store.js").KgStore | (() => import("./store.js").KgStore), decide?: Function }} deps
 * `decide` is the injected decision call used only by kg_check_connection.
 */
export function buildTools({ store: source, decide }) {
  // A function is called on first use, so a missing or broken file cannot stop the plugin loading.
  let opened;
  const getStore = () => (opened ??= typeof source === "function" ? source() : source);
  const defs = [
    {
      name: "kg_search",
      description:
        "Search the knowledge graph for entities by id, name, alias or description. Exact name/alias matches come " +
        "first. Use it to turn a name into an entity id before kg_neighbors or kg_path.",
      parameters: obj({
        query: { type: "string", minLength: 1, description: "Entity name, alias, id or topic." },
        type: { type: "string", enum: ENTITY_TYPES, description: "Only this entity type." },
        limit: int("Max results.", 1, 50, 10),
      }, ["query"]),
      run: (p, store) => {
        const entities = store.search(p.query, { type: p.type, limit: p.limit ?? 10 });
        return entities.length ? entities : { message: `No entities match "${p.query}".`, entities: [] };
      },
    },
    {
      name: "kg_neighbors",
      description:
        "What is connected to one entity: the entities within max_hops and the relations among them. Use for " +
        "'what is related to X' and 'what does X touch'.",
      parameters: obj({ id: entityId, max_hops: int("Depth; 1 is direct relations only.", 1, 3, 1) }, ["id"]),
      run: (p, store) => store.neighbors(p.id, { maxHops: p.max_hops ?? 1 }),
    },
    {
      name: "kg_path",
      description:
        "Shortest chain of relations between two entities, ignoring direction. Use for impact questions: " +
        "'if X changes, what connects it to Y?'. Returns the entity ids in order and the relation at each step.",
      parameters: obj({ from: entityId, to: entityId, max_depth: int("Longest chain to look for.", 1, 10, 6) }, ["from", "to"]),
      run: (p, store) => {
        const r = store.path(p.from, p.to, { maxDepth: p.max_depth ?? 6 });
        if (!r.ok || r.found) return r;
        return { ...r, message: `No path from ${p.from} to ${p.to} within depth ${r.depth}.` };
      },
    },
    {
      name: "kg_context",
      description:
        "A bounded picture around one entity: its subgraph (max_hops, max_nodes), recent graph changes and totals. " +
        "Use when a brief or answer needs compact background on an entity.",
      parameters: obj({ id: entityId, max_hops: int("Subgraph depth.", 1, 3, 2), max_nodes: int("Node cap.", 1, 200, 60) }, ["id"]),
      run: (p, store) => store.context(p.id, { maxHops: p.max_hops ?? 2, maxNodes: p.max_nodes ?? 60 }),
    },
    {
      name: "kg_source",
      description:
        "Provenance of an entity: its record and every live relation with its sources and confidence. Check this " +
        "before trusting a graph claim; a claim with only 'inference' as source is a guess.",
      parameters: obj({ id: entityId }, ["id"]),
      run: (p, store) => store.sourceView(p.id),
    },
    {
      name: "kg_add",
      description:
        "Add or update entities and relations. Entities need id ('<type>:<slug>') and name; relations need " +
        "source_id, predicate, target_id and both endpoints must exist. Types and predicates are a fixed list " +
        "(see kg_stats). Updating merges: aliases and sources are added to, never wiped. Nothing is deleted.",
      parameters: obj({
        entities: {
          type: "array", description: "Entities to add or update.",
          items: obj({
            id: entityId,
            type: { type: "string", enum: ENTITY_TYPES, description: "Optional; must equal the id prefix." },
            name: { type: "string", minLength: 1 },
            description: { type: "string" },
            aliases: strings("Other names the owner uses for this."),
            confidence,
            provenance: strings("Where this came from, for example a file path or URL. Default ['inference']."),
          }, ["id", "name"]),
        },
        relations: {
          type: "array", description: "Relations to add or update.",
          items: obj({
            source_id: entityId,
            predicate: { type: "string", enum: PREDICATES },
            target_id: entityId,
            confidence,
            provenance: strings("Where this came from. Default ['inference']."),
          }, ["source_id", "predicate", "target_id"]),
        },
      }),
      run: (p, store) => {
        const added = { entities: 0, relations: 0 };
        const errors = [];
        for (const e of p.entities ?? []) {
          try { store.upsertEntity({ ...e, provenance: e.provenance?.length ? e.provenance : ["inference"] }); added.entities++; }
          catch (ex) { errors.push(ex.message); }
        }
        for (const r of p.relations ?? []) {
          try { store.addRelation({ ...r, provenance: r.provenance?.length ? r.provenance : ["inference"] }); added.relations++; }
          catch (ex) { errors.push(ex.message); }
        }
        const s = store.stats();
        return { ok: errors.length === 0, added, errors, stats: { entities: s.entities, relations: s.relations } };
      },
    },
    {
      name: "kg_stats",
      description:
        "Graph health: counts, breakdown by type and predicate, entities with no relations, and the allowed " +
        "entity types and predicates. Check the allowed lists before kg_add.",
      parameters: obj({}),
      run: (_p, store) => ({ ...store.stats(), entity_types: [...ENTITY_TYPES], predicates: [...PREDICATES] }),
    },
    {
      name: "kg_check_connection",
      description:
        "Ask the configured decision model whether two entities should be connected and with which predicate. " +
        "Read-only: it suggests, it never writes. Returns available:false when no decision model is configured.",
      parameters: obj({
        a: { type: "string", minLength: 1, description: "Entity id or name." },
        b: { type: "string", minLength: 1, description: "Entity id or name." },
      }, ["a", "b"]),
      run: (p, store) => checkConnection(store, p.a, p.b, decide),
    },
  ];
  return defs.map(({ run, ...def }) => ({
    ...def,
    async execute(_id, params) {
      try { return ok(await run(params ?? {}, getStore())); } catch (e) { return fail(e); }
    },
  }));
}
