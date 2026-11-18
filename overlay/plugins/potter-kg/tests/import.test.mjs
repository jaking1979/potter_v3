import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { openStore } from "../lib/store.js";
import { main } from "../scripts/kg-import.mjs";

function graph(dir, name, fill) {
  const s = openStore(join(dir, name));
  fill(s);
  s.close();
  return join(dir, name);
}

test("imports what is missing, merges aliases, never overwrites, skips clinical and dangling, tags the source", async () => {
  const dir = mkdtempSync(join(tmpdir(), "kgi-"));
  const from = graph(dir, "old.sqlite", (s) => {
    s.upsertEntity({ id: "person:ada", name: "Ada Example", aliases: ["Ada", "A."], provenance: ["v2"] });
    s.upsertEntity({ id: "place:cabin", name: "Hill Cabin", provenance: ["v2"] });
    s.upsertEntity({ id: "resource:intake", name: "Patient intake forms", provenance: ["v2"] });
    s.addRelation({ source_id: "person:ada", predicate: "lives_at", target_id: "place:cabin", provenance: ["v2"] });
    s.addRelation({ source_id: "person:ada", predicate: "uses", target_id: "resource:intake", provenance: ["v2"] });
  });
  const into = graph(dir, "live.sqlite", (s) => {
    s.upsertEntity({ id: "person:ada", name: "Ada (live record)", aliases: ["Ada"], provenance: ["v3"] });
  });
  const out = [];
  assert.equal(await main(["--from", from, "--into", into, "--dry-run"], { out: (x) => out.push(x), err: () => {} }), 0);
  assert.match(out[0], /^Would import 1 entities and 1 relations/);
  assert.equal(await main(["--from", from, "--into", into], { out: (x) => out.push(x), err: () => {} }), 0);
  assert.match(out[1], /Imported 1 entities and 1 relations\. Already there: 1 entities .*Skipped: 0 outside the vocabulary, 2 clinical/);
  const s = openStore(into);
  const ada = s.getEntity("person:ada");
  assert.equal(ada.name, "Ada (live record)");                                   // the live record wins
  assert.deepEqual(ada.aliases, ["Ada", "A."]);
  assert.equal(s.getEntity("resource:intake"), null);
  assert.ok(s.getEntity("place:cabin").provenance.includes("import:old.sqlite"));
  s.close();
  assert.equal(await main(["--from", from, "--into", from], { out: () => {}, err: () => {} }), 2);
});
