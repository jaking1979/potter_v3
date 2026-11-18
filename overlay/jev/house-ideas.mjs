#!/usr/bin/env node
// House ideas filter: the model proposes automations from the evidence pack; Jev keeps only ideas that the evidence
// supports, that are safe, and that are new. One request per idea, three narrow yes/no questions; code decides.
//   stdin: {"evidence": <merrick insights --json>, "existing": ["<automation or rejected idea>", ...],
//           "ideas": [{"title": "...", "what": "...", "cites": "<the numbers it rests on>"}]}
//   stdout: {"kept": [...], "dropped": [{"title", "why"}]}
// env: POTTER_JEV_ENV, POTTER_MODEL_POLICY. Without a key: exit 1, nothing printed.
import { readFileSync } from "node:fs";
import { createDecider } from "./index.js";

export function ideaRequest(idea, evidence, existing) {
  return {
    state: { idea, evidence: { ready: evidence?.ready ?? [], coldest_seen_f: evidence?.coldest_seen_f, days_of_data: evidence?.days_of_data }, existing },
    questions: {
      supported: { type: "noul", instructions: { question: "Do the numbers in `evidence` support `idea` as stated (not just make it plausible)?",
        focus: "An idea about conditions the evidence has not seen yet (colder weather, longer periods) is not supported." } },
      safe: { type: "noul", instructions: { question: "Is `idea` safe: no locks, alarms, garage doors or water valves, no heating setpoint below 50 F or above 74 F, and nothing that could let pipes freeze?" } },
      is_new: { type: "noul", instructions: { question: "Is `idea` different from everything in `existing` (not the same automation or a rejected idea reworded)?" } },
    },
  };
}

const p = (a) => (typeof a?.noul === "number" ? a.noul : typeof a?.probabilityTrue === "number" ? a.probabilityTrue : null);

export function keep(answers) {
  const s = p(answers?.supported), f = p(answers?.safe), n = p(answers?.is_new);
  if (f === null || f < 0.9) return "not clearly safe";
  if (s === null || s < 0.7) return "not supported by the data yet";
  if (n === null || n < 0.7) return "already exists or was rejected";
  return null;
}

export async function filter(input, decide) {
  const kept = [], dropped = [];
  for (const idea of (input.ideas ?? []).slice(0, 10)) {
    const out = await decide(ideaRequest(idea, input.evidence, input.existing ?? []));
    const why = out?.ok ? keep(out.answers) : "could not be checked";
    if (why) dropped.push({ title: idea.title, why }); else kept.push(idea);
  }
  return { kept, dropped };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const env = { ...process.env };
  try {
    for (const line of readFileSync(env.POTTER_JEV_ENV || "", "utf8").split("\n")) { const m = line.match(/^([A-Z_]+)=(.*)$/); if (m) env[m[1]] = m[2]; }
  } catch { /* no key file */ }
  const decide = createDecider({ env, policyPath: env.POTTER_MODEL_POLICY, taskClass: "decision" });
  if (!decide) process.exit(1);
  const input = JSON.parse(readFileSync(0, "utf8") || "{}");
  filter(input, decide).then((r) => process.stdout.write(JSON.stringify(r) + "\n"));
}
