// Potter v3.10 chief-of-staff loop: forecast the day, check it in the evening, learn patterns, review the week.
//   forecast  the morning brief writes state/forecasts/<date>.json: the day's shape and 2-4 checkable predictions
//   evening   the evening check-in adds what actually happened (in the owner's words) to the same file
//   score     Jev judges each prediction against the evening record (one narrow yes/no each, in one request)
//   patterns  code turns repeated outcomes into patterns with evidence and confidence, written to the graph
//             (pattern entities linked to the owner), so recall brings them to every later turn
// Pure: files' contents in, decisions out. No model, network or clock of its own.

export const SHAPES = ["light", "normal", "packed"];
export const CONF = ["low", "medium", "high"];
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

export const norm = (s) => String(s ?? "").toLowerCase().replace(/[^\p{L}\p{N} ]+/gu, " ").replace(/\s+/g, " ").trim();

/** Keep a forecast only as far as it is well formed. */
export function cleanForecast(f) {
  if (!f || !/^\d{4}-\d{2}-\d{2}$/.test(f.date ?? "")) return null;
  const items = (Array.isArray(f.predictions) ? f.predictions : []).slice(0, 6).map((p, i) => ({
    id: String(p?.id ?? `p${i + 1}`),
    claim: String(p?.claim ?? "").trim().slice(0, 300),
    item: norm(p?.item ?? "").slice(0, 80),
    type: p?.type === "at_risk" ? "at_risk" : "will_happen",
    confidence: CONF.includes(p?.confidence) ? p.confidence : "medium",
  })).filter((p) => p.claim);
  const ev = f.evening && typeof f.evening === "object" ? {
    text: String(f.evening.text ?? "").slice(0, 3000),
    shape: SHAPES.includes(f.evening.shape) ? f.evening.shape : null,
  } : null;
  return { date: f.date, shape: SHAPES.includes(f.shape) ? f.shape : null, predictions: items, evening: ev && ev.text.trim() ? ev : null,
    outcomes: f.outcomes && typeof f.outcomes === "object" ? f.outcomes : null };
}

/** One Jev request: did each prediction come true, according to the evening record? */
export function outcomeRequest(f) {
  const questions = {};
  f.predictions.forEach((p, i) => {
    questions[`came_true_${i}`] = {
      type: "noul",
      instructions: {
        question: `According to \`evening\`, did \`predictions[${i}].claim\` come true?`,
        focus: "Only what the evening record says happened. Not mentioned means unknown, not false.",
      },
      criteria: {
        true: { what: "The record says it happened as predicted" },
        false: { what: "The record says it did not happen, or happened differently" },
      },
    };
    questions[`mentioned_${i}`] = {
      type: "noul",
      instructions: { question: `Does \`evening\` say anything about \`predictions[${i}].item\` or \`predictions[${i}].claim\`?` },
    };
  });
  return { state: { date: f.date, predictions: f.predictions.map((p) => ({ claim: p.claim, item: p.item })), evening: f.evening.text }, questions };
}

const yes = (a) => (typeof a?.noul === "number" ? a.noul : typeof a?.probabilityTrue === "number" ? a.probabilityTrue : null);

/** hit | miss | unknown per prediction. Not mentioned is unknown, whatever "came true" says. */
export function readOutcomes(f, answers = {}) {
  const out = {};
  f.predictions.forEach((p, i) => {
    const m = yes(answers[`mentioned_${i}`]), t = yes(answers[`came_true_${i}`]);
    out[p.id] = m === null || t === null || m < 0.5 ? "unknown" : t >= 0.7 ? "hit" : t <= 0.3 ? "miss" : "unknown";
  });
  return out;
}

/** Wilson-style label: a pattern needs enough evidence and a clear rate. */
export function confidenceOf(k, n) {
  if (n >= 4 && k / n >= 0.75) return "high";
  if (n >= 3 && k / n >= 0.6) return "medium";
  return null;
}

/**
 * Patterns from the scored forecasts of the last `days` days (newest last). Each: { key, name, description, confidence, k, n }.
 *   slips       the same item predicted to happen (or flagged at risk) and it did not, 2+ times
 *   weekday     a weekday that runs packed (or light) in most recent weeks
 *   calibration how often high-confidence forecasts come true, once there are enough of them
 *   shape       whether the day's shape forecast is usually right
 */
export function patterns(forecasts, { today, days = 28 } = {}) {
  const since = new Date(Date.parse(today) - days * 86400000).toISOString().slice(0, 10);
  const fs = forecasts.filter((f) => f && f.date >= since && f.date <= today && f.outcomes);
  const out = [];

  const items = new Map();
  for (const f of fs) for (const p of f.predictions) {
    const o = f.outcomes[p.id];
    if (!p.item || o === "unknown" || o === undefined) continue;
    const x = items.get(p.item) ?? { n: 0, slip: 0, label: p.item };
    x.n++;
    if ((p.type === "will_happen" && o === "miss") || (p.type === "at_risk" && o === "hit")) x.slip++;
    items.set(p.item, x);
  }
  for (const [item, x] of items) {
    const c = x.slip >= 2 ? confidenceOf(x.slip, x.n) ?? (x.slip >= 2 ? "medium" : null) : null;
    if (c) out.push({ key: `slip-${item}`, name: `${cap(x.label)} keeps slipping`, confidence: c, k: x.slip, n: x.n,
      description: `Slipped ${x.slip} of ${x.n} days it was planned (last ${days} days). Plan it early or make it smaller.` });
  }

  const byDay = new Map();
  for (const f of fs) {
    if (!f.evening?.shape) continue;
    const w = new Date(`${f.date}T12:00:00Z`).getUTCDay();
    const x = byDay.get(w) ?? { n: 0, packed: 0, light: 0 };
    x.n++; if (f.evening.shape === "packed") x.packed++; if (f.evening.shape === "light") x.light++;
    byDay.set(w, x);
  }
  for (const [w, x] of byDay) {
    for (const s of ["packed", "light"]) {
      const c = confidenceOf(x[s], x.n);
      if (c && x.n >= 3) out.push({ key: `weekday-${WEEKDAYS[w].toLowerCase()}-${s}`, name: `${WEEKDAYS[w]}s run ${s}`, confidence: c, k: x[s], n: x.n,
        description: `${x[s]} of the last ${x.n} ${WEEKDAYS[w]}s were ${s}.` });
    }
  }

  const hi = fs.flatMap((f) => f.predictions.filter((p) => p.confidence === "high").map((p) => f.outcomes[p.id])).filter((o) => o === "hit" || o === "miss");
  if (hi.length >= 6) {
    const k = hi.filter((o) => o === "hit").length;
    out.push({ key: "calibration-high", name: `My high-confidence forecasts come true ${Math.round((100 * k) / hi.length)}% of the time`,
      confidence: hi.length >= 10 ? "high" : "medium", k, n: hi.length,
      description: k / hi.length < 0.7 ? "Lower confidence on day forecasts until this improves." : "Day forecasts at high confidence can be trusted." });
  }

  const sh = fs.filter((f) => f.shape && f.evening?.shape);
  if (sh.length >= 5) {
    const k = sh.filter((f) => f.shape === f.evening.shape).length;
    out.push({ key: "calibration-shape", name: `My read of the day's shape is right ${Math.round((100 * k) / sh.length)}% of the time`,
      confidence: sh.length >= 10 ? "high" : "medium", k, n: sh.length,
      description: k / sh.length < 0.6 ? "Days are often heavier or lighter than they look in the morning." : "Morning reads of the day are usually right." });
  }
  return out;
}

const cap = (s) => String(s).charAt(0).toUpperCase() + String(s).slice(1);

/** The week in numbers, for the Sunday review. */
export function weekSummary(forecasts, { today }) {
  const since = new Date(Date.parse(today) - 7 * 86400000).toISOString().slice(0, 10);
  const fs = forecasts.filter((f) => f && f.date > since && f.date <= today);
  let hit = 0, miss = 0, unknown = 0;
  const slipped = new Map();
  for (const f of fs) for (const p of f.predictions) {
    const o = f.outcomes?.[p.id] ?? "unknown";
    if (o === "hit") hit++; else if (o === "miss") miss++; else unknown++;
    if ((p.type === "will_happen" && o === "miss") || (p.type === "at_risk" && o === "hit")) slipped.set(p.item || p.claim, (slipped.get(p.item || p.claim) ?? 0) + 1);
  }
  return { days: fs.length, withEvening: fs.filter((f) => f.evening).length, hit, miss, unknown,
    accuracy: hit + miss ? Math.round((100 * hit) / (hit + miss)) : null,
    slipped: [...slipped.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5).map(([item, n]) => ({ item, times: n })) };
}
