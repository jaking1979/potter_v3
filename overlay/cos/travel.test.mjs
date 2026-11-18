import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { clean, due, liveStatus, main, merge, zonedEpoch } from "./travel.mjs";

const EMAIL = "Your JetBlue booking is confirmed. Confirmation ABCDEF. Flight B6 1023, Thu Nov 26 2026, JFK 07:15 to BUF 08:45. Passenger: A Owner.";
const SEG = { kind: "flight", carrier: "JetBlue", number: "B6 1023", date: "2026-11-26", from: "JFK", to: "BUF", depart: "07:15", arrive: "08:45", confirmation: "ABCDEF", change: "new" };

test("only facts that are in the email survive: invented flight numbers, airports and codes are dropped", () => {
  assert.equal(clean({ segments: [SEG] }, EMAIL).length, 1);
  assert.equal(clean({ segments: [{ ...SEG, number: "B6 999" }] }, EMAIL).length, 0);
  assert.equal(clean({ segments: [{ ...SEG, to: "LAX" }] }, EMAIL).length, 0);
  assert.equal(clean({ segments: [{ ...SEG, confirmation: "ZZZZZZ" }] }, EMAIL)[0].confirmation, "");
  assert.equal(clean({ segments: [{ ...SEG, date: "tomorrow" }] }, EMAIL).length, 0);
});

test("local airport time is converted correctly across time zones", () => {
  assert.equal(new Date(zonedEpoch("2026-11-26", "07:15", "America/New_York")).toISOString(), "2026-11-26T12:15:00.000Z");
  assert.equal(new Date(zonedEpoch("2026-07-01", "07:15", "America/Los_Angeles")).toISOString(), "2026-07-01T14:15:00.000Z");
});

test("merge: a new booking is added; a later email with a new time is a change; a cancellation is recorded", () => {
  const trips = { segments: [] };
  assert.equal(merge(trips, [SEG])[0].what, "added");
  const ch = merge(trips, [{ ...SEG, depart: "09:05" }])[0];
  assert.deepEqual([ch.what, ch.before.depart], ["changed", "07:15"]);
  assert.equal(merge(trips, [{ ...SEG, change: "cancelled" }])[0].what, "cancelled");
});

test("reminders: check-in at 24 h, leave at 3 h, each once, none for cancelled flights", () => {
  const trips = { segments: [], sent: {} };
  merge(trips, [SEG]);
  const t = zonedEpoch("2026-11-26", "07:15", "America/New_York");
  assert.deepEqual(due(trips, { now: t - 30 * 3600e3 }), []);
  const c = due(trips, { now: t - 23 * 3600e3 });
  assert.match(c[0].text, /^Check-in for B6 1023 JFK→BUF 2026-11-26 07:15 is open \(confirmation ABCDEF\)/);
  trips.sent[c[0].key] = "x";
  assert.deepEqual(due(trips, { now: t - 22 * 3600e3 }), []);
  assert.match(due(trips, { now: t - 2.5 * 3600e3 })[0].text, /departure in 3 hours/);
  trips.segments[0].status = "cancelled";
  assert.deepEqual(due(trips, { now: t - 2.5 * 3600e3 }), []);
});

test("live status: cancellation and real delays alert; small wobbles do not", async () => {
  const resp = (f) => async () => ({ ok: true, json: async () => ({ flights: [f] }) });
  const base = { scheduled_out: "2026-11-26T12:15:00Z", estimated_out: "2026-11-26T12:20:00Z", gate_origin: "B22" };
  assert.equal((await liveStatus(SEG, { apiKey: "k", fetchImpl: resp(base) })).alert, null);
  assert.match((await liveStatus(SEG, { apiKey: "k", fetchImpl: resp({ ...base, estimated_out: "2026-11-26T13:00:00Z" }) })).alert, /delayed about 45 minutes/);
  assert.match((await liveStatus(SEG, { apiKey: "k", fetchImpl: resp({ ...base, cancelled: true }) })).alert, /CANCELLED/);
});

test("end to end: add from an email, list, watch sends the check-in reminder once", async () => {
  const dir = mkdtempSync(join(tmpdir(), "trv-"));
  const sent = [];
  const deps = { extract: async () => ({ ok: true, json: { segments: [SEG] } }), send: async (m) => { sent.push(m); return true; }, stdin: EMAIL };
  const out = [];
  const io = { out: (x) => out.push(x), err: (x) => out.push(x) };
  assert.equal(await main(["add", "--dir", dir], {}, io, deps), 0);
  assert.match(out.pop(), /1 change\(s\): added flight:B61023:2026-11-26/);
  const t = zonedEpoch("2026-11-26", "07:15", "America/New_York");
  await main(["watch", "--dir", dir], {}, io, { ...deps, now: t - 20 * 3600e3 });
  await main(["watch", "--dir", dir], {}, io, { ...deps, now: t - 19 * 3600e3 });
  assert.equal(sent.filter((m) => m.startsWith("Check-in")).length, 1);
  writeFileSync(join(dir, "travel-inbox.jsonl"), JSON.stringify({ id: "m9", text: EMAIL.replace("07:15", "09:05") }) + "\n");
  await main(["capture", "--dir", dir], {}, io, { ...deps, extract: async () => ({ ok: true, json: { segments: [{ ...SEG, depart: "09:05" }] } }) });
  assert.match(sent.pop(), /^Trip change: B6 1023 2026-11-26: depart 07:15 → 09:05\./);
  assert.equal(readFileSync(join(dir, "travel-inbox.jsonl"), "utf8"), "");
});
