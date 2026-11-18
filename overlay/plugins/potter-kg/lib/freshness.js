// v3.9 Staleness: facts that stop being true must leave the graph, and only the owner decides what is still true.
//   expire    a fact with an "until" date in the past is archived (code; no question needed)
//   ask       a small weekly set of "still true?" questions, chosen in code from evidence:
//               situational facts older than 90 days, preferences older than a year (both from captured kind),
//               and facts Potter actually USED recently that Jev judges time-bound (imported facts carry no kind)
//   answer    keep (confirmed, high), archive, or the owner says what changed (the capture path writes it)
// Pure: rows and answers in, plans out. The script applies them.

export const LIMITS = { situationalDays: 90, preferenceDays: 365, confirmedDays: 180, askPerWeek: 5, reaskDays: 60, usedDays: 30 };
const DAY = 86400000;

/** Read the tags the capture path writes into provenance: until:, kind:, confirmed:, and the capture date. */
export function tags(provenance = []) {
  const t = { until: null, kind: null, confirmed: null, captured: null };
  for (const p of provenance) {
    const s = String(p);
    const u = s.match(/\buntil:(\d{4}-\d{2}-\d{2})/); if (u) t.until = u[1];
    const k = s.match(/\bkind:(durable|preference|situational|one_off)/); if (k) t.kind = k[1];
    const c = s.match(/^confirmed:(\d{4}-\d{2}-\d{2})/); if (c && (!t.confirmed || c[1] > t.confirmed)) t.confirmed = c[1];
    const d = s.match(/^chat (\d{4}-\d{2}-\d{2})/); if (d && (!t.captured || d[1] > t.captured)) t.captured = d[1];
  }
  return t;
}

export const relKey = (r) => `${r.source_id}|${r.predicate}|${r.target_id}`;
const age = (date, today) => (date ? (Date.parse(today) - Date.parse(date)) / DAY : Infinity);

/**
 * relations: live rows {id, source_id, predicate, target_id, confidence, provenance[], created, updated}
 * usage: Map key -> last used ISO date; asked: Map key -> last asked ISO date
 * timeBound: Map key -> P(time-bound) from Jev, for used facts without a captured kind
 */
export function plan({ relations, usage = new Map(), asked = new Map(), timeBound = new Map(), today, limits = LIMITS }) {
  const expire = [], ask = [], needJudgement = [];
  for (const r of relations) {
    const t = tags(r.provenance);
    if (t.until && t.until < today) { expire.push({ r, why: `ended ${t.until}` }); continue; }
    const k = relKey(r);
    if (age(asked.get(k), today) < limits.reaskDays) continue;
    const since = t.confirmed ?? t.captured ?? String(r.created ?? "").slice(0, 10);
    if (age(t.confirmed, today) < limits.confirmedDays) continue;
    if (t.kind === "situational" && age(since, today) > limits.situationalDays) { ask.push({ r, why: `temporary fact from ${since}`, score: 3 }); continue; }
    if (t.kind === "preference" && age(since, today) > limits.preferenceDays) { ask.push({ r, why: `preference from ${since}`, score: 2 }); continue; }
    const used = usage.get(k);
    if (!t.kind && used && age(used, today) <= limits.usedDays) {
      const p = timeBound.get(k);
      if (p === undefined) needJudgement.push(r);
      else if (p >= 0.7 && age(since, today) > limits.situationalDays) ask.push({ r, why: `used recently; may have changed since ${since}`, score: 1 + p });
    }
  }
  ask.sort((a, b) => b.score - a.score);
  return { expire, ask: ask.slice(0, limits.askPerWeek), needJudgement };
}

/** One Jev request per fact: does this kind of fact stop being true within a year or so? */
export function timeBoundRequest(text) {
  return {
    state: { fact: text },
    questions: {
      time_bound: {
        type: "noul",
        instructions: { question: "Is `fact` the kind of thing that usually stops being true within a year or two?" },
        criteria: {
          true: { what: "Temporary or changing", examples: ["a child's current team or school year", "a project in progress", "a current car", "a contractor in use now"] },
          false: { what: "Lasting", examples: ["who is married to whom", "a birthday", "who is whose parent", "a home owned for years"] },
        },
      },
    },
  };
}

export function readable(r, nameOf) {
  return `${nameOf(r.source_id)} ${r.predicate.replace(/_/g, " ")} ${nameOf(r.target_id)}`;
}
