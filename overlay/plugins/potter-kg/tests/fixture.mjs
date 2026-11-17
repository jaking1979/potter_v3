// A small made-up graph shared by the tests. Nothing here describes a real person or place.
import { openStore } from "../lib/store.js";

export const T0 = Date.parse("2026-01-01T00:00:00Z");

/** An in-memory store whose clock the test controls: clock.t is epoch ms. */
export function freshStore(clock = { t: T0 }) {
  const store = openStore(":memory:", { now: () => new Date(clock.t) });
  return { store, clock };
}

export function seeded(clock = { t: T0 }) {
  const { store } = freshStore(clock);
  const E = (id, name, extra = {}) => store.upsertEntity({ id, name, provenance: ["seed"], ...extra });
  E("person:ada", "Ada Example", { aliases: ["Ada", "the owner"], confidence: "high" });
  E("person:bo", "Bo Example", { aliases: ["Bo"] });
  E("place:cabin", "Hill Cabin", { aliases: ["the cabin", "upstate place"], description: "Weekend house in the hills" });
  E("device:laptop", "Spare Laptop", { aliases: ["host-a.example"], description: "Runs the local model" });
  E("system:assistant", "Assistant Gateway", { description: "The assistant's gateway process" });
  E("system:home-hub", "Home Hub", { description: "Home automation controller" });
  E("concept:overthinking", "Overthinking", { description: "Rumination and how to interrupt it" });
  E("concept:stoicism", "Stoicism");
  E("concept:cbti", "CBT-I", { aliases: ["sleep therapy"] });
  E("resource:sleep-course", "Sleep Course");
  E("interest:baking", "Baking");
  const R = (source_id, predicate, target_id, extra = {}) => store.addRelation({ source_id, predicate, target_id, provenance: ["seed"], ...extra });
  R("person:ada", "married_to", "person:bo", { confidence: "high" });
  R("person:ada", "lives_at", "place:cabin");
  R("device:laptop", "candidate_host_for", "system:assistant");
  R("system:assistant", "integrates_with", "system:home-hub");
  R("system:home-hub", "located_in", "place:cabin");
  R("concept:overthinking", "connects_to", "concept:stoicism", { confidence: "low" });
  R("resource:sleep-course", "references", "concept:cbti");
  R("concept:cbti", "connects_to", "concept:overthinking");
  return store;
}
