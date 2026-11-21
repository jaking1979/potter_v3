#!/usr/bin/env node
// Jev for Merrick's router (v3.2): the owner's words on stdin -> {"intent", "confidence", "zone"} on stdout.
// Called only for requests Merrick's own table did not recognise; the router accepts the answer only for a known
// intent above its confidence floor, and always takes the temperature from the owner's words, never from here.
// No key, no house file or any failure: prints nothing and exits 1, so the router falls back as before.
//
// env: POTTER_JEV_ENV (file with OPENROUTER_API_KEY=...), POTTER_MODEL_POLICY (policy json), MERRICK_HOUSE
import { readFileSync } from "node:fs";
import { createDecisionClient } from "./client.js";

export const INTENTS = {
  set_temperature: "Change a thermostat setpoint now to a number the owner gave",
  turn_off: "Turn heating or cooling off",
  hold_away: "Put the house in its away or vacancy hold",
  check_temperature: "Ask how warm or cold a room or the thermostat is",
  check_perimeter: "Ask whether doors or windows are open or closed",
  house_status: "Ask for an overall status of the house",
  heating_estimate: "Ask how long heating to a temperature will take",
  close_up: "Close up or leave the house (departure check)",
  refuse_protected: "Operate a lock, alarm, water valve or garage",
  bedtime: "The owner is going to bed (the bedtime routine)",
  coming_up: "The owner is coming up to the house at some time",
  arrived: "The owner has just arrived at the house",
  night_report: "Ask how last night went in the house",
  not_home: "Not about the house at all",
};

function read(path) {
  try { return readFileSync(path, "utf8"); } catch { return ""; }
}

export function questions(zones) {
  return {
    intent: { type: "choice", instructions: "Which kind of house request is this?", criteria: INTENTS },
    zone: { type: "choice", instructions: "Which heating zone does it name? Choose none if no zone is named.",
      criteria: Object.fromEntries([...zones.map((z) => [z, `The ${z.replace(/_/g, " ")} zone`]), ["none", "No zone named"]]) },
  };
}

export function toRoute(answers = {}) {
  const i = answers.intent ?? {};
  const z = answers.zone ?? {};
  const intent = typeof i.choice === "string" ? i.choice : null;
  if (!intent || !(intent in INTENTS)) return null;
  const confidence = typeof i.confidence === "number" ? i.confidence : (i.probabilities?.[intent] ?? 0);
  return { intent, confidence, zone: typeof z.choice === "string" && z.choice !== "none" ? z.choice : null };
}

async function main() {
  const text = read("/dev/stdin").trim().slice(0, 1000);
  const key = (read(process.env.POTTER_JEV_ENV || "").match(/^OPENROUTER_API_KEY=(.+)$/m) || [])[1];
  if (!text || !key) process.exit(1);
  let zones = [];
  try { zones = Object.keys(JSON.parse(read(process.env.MERRICK_HOUSE || "")).zones || {}); } catch { /* none */ }
  let model = "~typesafe/jev-latest";
  try { model = JSON.parse(read(process.env.POTTER_MODEL_POLICY || "")).classes.decision.model || model; } catch { /* default */ }
  const out = await createDecisionClient({ apiKey: key, fetchImpl: globalThis.fetch })
    .ask({ model, state: { request: text }, questions: questions(zones), timeoutMs: 3000 });
  const route = out.ok ? toRoute(out.answers) : null;
  if (!route) process.exit(1);
  process.stdout.write(JSON.stringify(route));
}

if (import.meta.url === `file://${process.argv[1]}`) main();
