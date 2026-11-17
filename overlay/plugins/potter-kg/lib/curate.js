// The curator: keeps the graph tidy. Two parts.
//   Deterministic (always): scan for orphans, same-name duplicates and weak relations; record the run.
//   Decisions (only when a decision call is injected): ask one bounded question per finding and, with
//   `apply`, act on confident answers. Nothing is deleted; everything it does is an archive or a merge.
// No SDK, no network, no clock of its own: store, decide and now are all passed in.

export const DEFAULTS = {
  maxCalls: 30,            // decision calls per run, across all passes
  maxConsecutiveErrors: 3, // stop asking after this many failures in a row
  orphanGraceDays: 7,      // a new entity is not an orphan until its relations had time to arrive
  reviewDays: 30,          // a finding judged "leave it" is not asked about again for this long
  archiveOrphanAt: 0.6,
  mergeAt: 0.7,
  upgradeAt: 0.7,
  rejectAt: 0.8,
};

const DAY = 86400000;
const QUESTIONS = {
  orphan: {
    instructions: "This knowledge-graph entity has no relations to anything. Should it be kept or archived?",
    criteria: { keep: "Plausibly useful later even without relations", archive: "No value without relations" },
  },
  alias: {
    instructions: "These entities share a name or alias. Are they the same thing recorded twice?",
    criteria: { distinct: "Different things that happen to share a name", merge: "The same thing; one record should remain" },
  },
  relation: {
    instructions: "Is this knowledge-graph relation correct?",
    criteria: {
      keep: "Plausible as stated, but not certain",
      upgrade: "Clearly correct; deserves high confidence",
      reject: "Wrong or meaningless",
    },
  },
};

/** The deterministic part: what needs attention right now. Reads only. */
export function scan(store, { now = new Date(), orphanGraceDays = DEFAULTS.orphanGraceDays } = {}) {
  const cutoff = new Date(now.getTime() - orphanGraceDays * DAY).toISOString();
  return {
    stats: (({ entities, relations, archivedEntities, orphanCount }) => ({ entities, relations, archivedEntities, orphanCount }))(store.stats()),
    orphans: store.orphans().filter((e) => e.updated <= cutoff),
    aliasConflicts: store.aliasConflicts(),
    weakRelations: store.weakRelations(),
  };
}

function reviewedRecently(store, key, now, reviewDays) {
  const at = store.getMeta(key);
  return Boolean(at) && now.getTime() - Date.parse(at) < reviewDays * DAY;
}

/**
 * @param store   KgStore
 * @param opts.decide  optional async ({ purpose, state, questions }) => { ok, answers: { decision: { choice, confidence } } }
 * @param opts.apply   write confident decisions (default false: propose only)
 * @returns a run record with counts and ids only (no descriptions, no model text)
 */
export async function curate(store, opts = {}) {
  const o = { ...DEFAULTS, ...Object.fromEntries(Object.entries(opts).filter(([, v]) => v !== undefined)) };
  const now = o.now ?? new Date();
  const found = scan(store, { now, orphanGraceDays: o.orphanGraceDays });
  const run = {
    at: now.toISOString(),
    mode: typeof o.decide === "function" ? (o.apply ? "apply" : "propose") : "deterministic",
    found: { orphans: found.orphans.length, aliasConflicts: found.aliasConflicts.length, weakRelations: found.weakRelations.length },
    before: found.stats,
    calls: 0, errors: 0, stopped: null,
    decisions: [], actions: [],
  };

  let streak = 0;
  // One question, one answer: { choice, confidence } or null when the call failed or the budget is spent.
  const ask = async (kind, state) => {
    if (run.stopped) return null;
    if (run.calls >= o.maxCalls) { run.stopped = "call-budget"; return null; }
    run.calls++;
    let out;
    try {
      out = await o.decide({ purpose: `potter-kg.curate.${kind}`, state, questions: { decision: { type: "choice", ...QUESTIONS[kind] } } });
    } catch (e) {
      out = { ok: false };
    }
    const d = out?.ok ? out.answers?.decision : null;
    if (!d || typeof d.choice !== "string" || !(d.choice in QUESTIONS[kind].criteria)) {
      run.errors++;
      if (++streak >= o.maxConsecutiveErrors) run.stopped = "decision-errors";
      return null;
    }
    streak = 0;
    const confidence = typeof d.confidence === "number" ? d.confidence
      : typeof d.probabilities?.[d.choice] === "number" ? d.probabilities[d.choice] : 0;
    return { choice: d.choice, confidence };
  };
  const stamp = (key) => { if (o.apply) store.setMeta(key, now.toISOString()); };
  const act = (action, fn) => { if (o.apply) { fn(); run.actions.push(action); } };

  if (typeof o.decide === "function") {
    for (const e of found.orphans) {
      const key = `curator:orphan:${e.id}`;
      if (reviewedRecently(store, key, now, o.reviewDays)) continue;
      const d = await ask("orphan", { entity: { id: e.id, type: e.type, name: e.name, description: e.description } });
      if (!d) { if (run.stopped) break; continue; }
      run.decisions.push({ pass: "orphan", id: e.id, ...d });
      if (d.choice === "archive" && d.confidence >= o.archiveOrphanAt) {
        act({ action: "archive-entity", id: e.id }, () => store.archiveEntity(e.id, "curator-orphan"));
      } else stamp(key);
    }

    for (const c of found.aliasConflicts) {
      if (run.stopped) break;
      const key = `curator:alias:${c.ids.join("|")}`;
      if (reviewedRecently(store, key, now, o.reviewDays)) continue;
      const entities = c.ids.map((id) => store.getEntity(id)).filter((e) => e?.status === "active");
      if (entities.length < 2) continue; // an earlier action in this run already settled it
      const d = await ask("alias", {
        shared: c.key,
        entities: entities.map((e) => ({ id: e.id, type: e.type, name: e.name, description: e.description, aliases: e.aliases })),
      });
      if (!d) continue;
      run.decisions.push({ pass: "alias", ids: c.ids, ...d });
      if (d.choice === "merge" && d.confidence >= o.mergeAt) {
        // Keep the best-connected record; ties go to the oldest.
        const ranked = entities.map((e) => ({ e, n: store.relationsOf(e.id).length }))
          .sort((x, y) => y.n - x.n || x.e.created.localeCompare(y.e.created) || x.e.id.localeCompare(y.e.id));
        const keep = ranked[0].e.id;
        for (const { e } of ranked.slice(1)) {
          act({ action: "merge", kept: keep, archived: e.id }, () => store.mergeEntities(keep, e.id));
        }
      } else stamp(key);
    }

    for (const r of found.weakRelations) {
      if (run.stopped) break;
      const key = `curator:relation:${r.id}`;
      if (reviewedRecently(store, key, now, o.reviewDays)) continue;
      const live = store.getRelationById(r.id);
      const [s, t] = [store.getEntity(r.source_id), store.getEntity(r.target_id)];
      if (live?.status !== "active" || s?.status !== "active" || t?.status !== "active") continue;
      const d = await ask("relation", {
        relation: { source: s.name, source_type: s.type, predicate: r.predicate, target: t.name, target_type: t.type },
        confidence: r.confidence,
        provenance: r.provenance,
      });
      if (!d) continue;
      const ref = { source_id: r.source_id, predicate: r.predicate, target_id: r.target_id };
      run.decisions.push({ pass: "relation", ...ref, ...d });
      if (d.choice === "upgrade" && d.confidence >= o.upgradeAt) {
        act({ action: "upgrade-relation", ...ref }, () => store.setRelationConfidence(r.id, "high", "curator-upgrade"));
      } else if (d.choice === "reject" && d.confidence >= o.rejectAt) {
        act({ action: "archive-relation", ...ref }, () => store.archiveRelation(r.source_id, r.predicate, r.target_id));
      } else stamp(key);
    }
  }

  const s = store.stats();
  run.after = { entities: s.entities, relations: s.relations, archivedEntities: s.archivedEntities, orphanCount: s.orphanCount };
  // Configured, asked, and never once answered: that is a failure the schedule watchdog should see.
  run.ok = !(run.calls > 0 && run.errors === run.calls);
  return run;
}
