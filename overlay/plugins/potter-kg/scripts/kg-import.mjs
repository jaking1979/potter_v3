#!/usr/bin/env node
// kg-import: bring another graph file (v2's) into the live graph. Adds; never overwrites or deletes.
//
//   node kg-import.mjs --from <graph file> --into <graph file> [--dry-run]
//
// The source is opened READ-ONLY. An entity already in the live graph keeps its record (aliases are merged).
// A relation already there is left alone. Everything imported is tagged "import:<source file name>", so it can
// be found or archived later. Entities and relations outside the controlled vocabulary are skipped and counted.
// Clinical or patient material is never imported (Potter's wall), and is counted, not printed.
// Exit codes: 0 done; 2 bad arguments or unreadable file.
import { basename, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { ENTITY_TYPES, PREDICATES } from "../lib/vocab.js";
import { mergeLists } from "../lib/alias.js";

export const CLINICAL = /\b(patients?|ehr|phi|hipaa|clinical notes?|progress notes?|intake forms?|treatment plans?|diagnos[ei]s)\b/i;

const list = (s) => { try { const v = JSON.parse(s ?? "[]"); return Array.isArray(v) ? v : []; } catch { return []; } };

export function importGraph(src, dst, { tag, dryRun = false, now = new Date().toISOString() } = {}) {
  const counts = { entities: 0, entitiesMerged: 0, relations: 0, relationsKept: 0, skippedVocab: 0, skippedClinical: 0, skippedDangling: 0 };
  const ents = src.prepare("SELECT * FROM entities").all();
  const skip = new Set();
  const getE = dst.prepare("SELECT id, aliases, provenance FROM entities WHERE id=?");
  const insE = dst.prepare(`INSERT INTO entities (id, type, name, description, aliases, confidence, status, provenance, created, updated)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
  const updAliases = dst.prepare("UPDATE entities SET aliases=?, updated=? WHERE id=?");
  dst.exec("BEGIN IMMEDIATE");
  try {
    for (const e of ents) {
      if (!ENTITY_TYPES.includes(e.type)) { counts.skippedVocab++; skip.add(e.id); continue; }
      if (CLINICAL.test(`${e.id} ${e.name} ${e.description} ${e.provenance}`)) { counts.skippedClinical++; skip.add(e.id); continue; }
      const have = getE.get(e.id);
      if (have) {
        const merged = mergeLists(list(have.aliases), list(e.aliases));
        if (merged.length !== list(have.aliases).length && !dryRun) updAliases.run(JSON.stringify(merged), now, e.id);
        counts.entitiesMerged++;
        continue;
      }
      if (!dryRun) insE.run(e.id, e.type, e.name, e.description ?? "", e.aliases ?? "[]", e.confidence ?? "medium", e.status ?? "active",
        JSON.stringify([...list(e.provenance), tag]), e.created ?? now, now);
      counts.entities++;
    }
    const exists = dst.prepare("SELECT 1 FROM entities WHERE id=?");
    const getR = dst.prepare("SELECT 1 FROM relations WHERE source_id=? AND predicate=? AND target_id=?");
    const insR = dst.prepare(`INSERT INTO relations (source_id, predicate, target_id, confidence, status, provenance, created, updated)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)`);
    const known = (id) => !skip.has(id) && (exists.get(id) || (dryRun && ents.some((x) => x.id === id)));
    for (const r of src.prepare("SELECT * FROM relations").all()) {
      if (!PREDICATES.includes(r.predicate)) { counts.skippedVocab++; continue; }
      if (skip.has(r.source_id) || skip.has(r.target_id)) { counts.skippedClinical++; continue; }
      if (!known(r.source_id) || !known(r.target_id)) { counts.skippedDangling++; continue; }
      if (getR.get(r.source_id, r.predicate, r.target_id)) { counts.relationsKept++; continue; }
      if (!dryRun) insR.run(r.source_id, r.predicate, r.target_id, r.confidence ?? "medium", r.status ?? "active",
        JSON.stringify([...list(r.provenance), tag]), r.created ?? now, now);
      counts.relations++;
    }
    dst.exec(dryRun ? "ROLLBACK" : "COMMIT");
  } catch (e) {
    dst.exec("ROLLBACK");
    throw e;
  }
  return counts;
}

export async function main(argv, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }) {
  let a;
  try {
    a = parseArgs({ args: argv, options: { from: { type: "string" }, into: { type: "string" }, "dry-run": { type: "boolean", default: false } } }).values;
  } catch (e) { io.err(`kg-import: ${e.message}\n`); return 2; }
  if (!a.from || !a.into) { io.err("kg-import: --from and --into are required\n"); return 2; }
  if (resolve(a.from) === resolve(a.into)) { io.err("kg-import: --from and --into are the same file\n"); return 2; }
  const { DatabaseSync } = await import("node:sqlite");
  const { openStore } = await import("../lib/store.js");
  let src, store;
  try {
    src = new DatabaseSync(resolve(a.from), { readOnly: true });
    store = openStore(resolve(a.into));                  // creates the schema if the live graph is new
  } catch (e) { io.err(`kg-import: ${e.message}\n`); return 2; }
  try {
    const c = importGraph(src, store.db, { tag: `import:${basename(a.from)}`, dryRun: a["dry-run"] });
    io.out(`${a["dry-run"] ? "Would import" : "Imported"} ${c.entities} entities and ${c.relations} relations. ` +
      `Already there: ${c.entitiesMerged} entities (aliases merged), ${c.relationsKept} relations. ` +
      `Skipped: ${c.skippedVocab} outside the vocabulary, ${c.skippedClinical} clinical, ${c.skippedDangling} pointing at missing entities.\n`);
    return 0;
  } finally {
    src.close();
    store.close();
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).then((code) => { process.exitCode = code; });
}
