import assert from "node:assert/strict";
import { test } from "node:test";
import { createBackend, describe } from "../lib/bw.js";
import { createVault } from "../lib/core.js";
import { findRefs, mayReceive, scrub, substitute } from "../lib/refs.js";

const ITEMS = [
  { id: "1", name: "Con Ed", type: 1, notes: "", login: { username: "owner@example.com", password: "S3cret-Pass!", uris: [{ uri: "https://www.coned.com/login" }] } },
  { id: "2", name: "Amex Virtual", type: 3, notes: "", card: { number: "4111111111111111", code: "123", expMonth: "4", expYear: "2028", cardholderName: "A Owner" } },
  { id: "3", name: "Library", type: 1, notes: "potter:auto", login: { username: "reader", password: "books4ever" } },
];
const backend = { items: async () => ({ ok: true, items: ITEMS }), totp: async () => "000111" };
function vault(t = { now: 0 }) {
  const log = [], sent = [];
  const v = createVault({ backend, audit: (r) => log.push(r), send: async (m) => { sent.push(m); return true; }, now: () => t.now });
  return { v, log, sent, t };
}

test("references are found at any depth, substituted, and scrubbed back out", () => {
  const p = { steps: [{ type: "fill", value: "{{vault:Con Ed#password}}" }], note: "x" };
  assert.deepEqual(findRefs(p).map((r) => [r.item, r.field]), [["Con Ed", "password"]]);
  assert.equal(substitute(p, () => "VALUE").steps[0].value, "VALUE");
  assert.equal(scrub({ a: "said S3cret-Pass! twice: S3cret-Pass!" }, new Map([["S3cret-Pass!", "{{vault:Con Ed#password}}"]])).a,
    "said {{vault:Con Ed#password}} twice: {{vault:Con Ed#password}}");
});

test("only form and request tools may receive a value; shells, messages, files and memory never", () => {
  assert.ok(mayReceive("browser"));
  assert.ok(mayReceive("browser_act"));
  for (const t of ["exec", "message", "write", "memory_stage", "notion__notion-update-page", "agentmail__send_message", "vault_list", "some_tool"]) assert.ok(!mayReceive(t), t);
});

test("list shows names, kinds and fields, never a value", async () => {
  const { v } = vault();
  const out = JSON.stringify(await v.list());
  assert.ok(out.includes("Con Ed") && out.includes("password") && out.includes("www.coned.com"));
  for (const secret of ["S3cret-Pass!", "4111111111111111", "123\"", "books4ever", "owner@example.com"]) assert.ok(!out.includes(secret), secret);
});

test("approval: cards always, logins unless potter:auto", async () => {
  const { v } = vault();
  assert.equal((await v.needsApproval("Amex Virtual")).approval, true);
  assert.equal((await v.needsApproval("Con Ed")).approval, true);
  assert.equal((await v.needsApproval("Library")).approval, false);
  assert.match((await v.needsApproval("Nope")).error, /no item named/);
});

test("fill: blocked until authorized, then substituted for the allowed tool only, and it expires", async () => {
  const { v, log, t } = vault();
  const p = { action: "type", ref: "e12", text: "{{vault:Con Ed#password}}" };
  assert.match((await v.prepare("browser", p)).reason, /not authorized/);
  await v.authorize("Con Ed", "pay the electric bill");
  const ok = await v.prepare("browser", p, "call-1");
  assert.equal(ok.params.text, "S3cret-Pass!");
  assert.equal(p.text, "{{vault:Con Ed#password}}");                            // the model's own params are untouched
  assert.ok((await v.prepare("exec", { command: "echo {{vault:Con Ed#password}}" })).block);
  assert.ok(!JSON.stringify(log).includes("S3cret-Pass!"));                       // the audit holds names, not values
  t.now = 16 * 60000;
  assert.match((await v.prepare("browser", p)).reason, /not authorized/);
  assert.equal(await v.prepare("browser", { text: "no refs" }), undefined);
});

test("whatever passed through is scrubbed from results and messages; card fields are formatted", async () => {
  const { v } = vault();
  await v.authorize("Amex Virtual", "checkout");
  const r = await v.prepare("browser", { fields: ["{{vault:Amex Virtual#number}}", "{{vault:Amex Virtual#expiry}}", "{{vault:Amex Virtual#code}}"] });
  assert.deepEqual(r.params.fields, ["4111111111111111", "04/28", "123"]);
  const echoed = v.scrub({ role: "toolResult", content: [{ type: "text", text: "Typed 4111111111111111 into the card field" }] });
  assert.equal(echoed.content[0].text, "Typed {{vault:Amex Virtual#number}} into the card field");
});

test("send to owner: only when authorized; the model learns 'sent', the value goes to the chat", async () => {
  const { v, sent } = vault();
  assert.equal((await v.sendToOwner("Con Ed", "username")).ok, false);
  await v.authorize("Con Ed", "owner asked");
  const r = await v.sendToOwner("Con Ed", "username");
  assert.equal(r.ok, true);
  assert.ok(!JSON.stringify(r).includes("owner@example.com"));
  assert.match(sent[0], /^Con Ed username: owner@example\.com/);
});

test("backend: logs in with the API key, unlocks with the password from the environment, never passes it as an argument", async () => {
  const calls = [];
  const runImpl = async (bin, args, env) => {
    calls.push(args.join(" "));
    if (args[0] === "status") return { ok: true, out: JSON.stringify({ status: "unauthenticated" }) };
    if (args[0] === "unlock") return { ok: true, out: "SESSIONKEY\n" };
    if (args[0] === "list") return { ok: true, out: JSON.stringify(ITEMS) };
    return { ok: true, out: "" };
  };
  const b = createBackend({ env: { BW_CLIENTID: "id", BW_CLIENTSECRET: "s", BW_PASSWORD: "master", BW_SERVER: "https://vault.example.ts.net" }, runImpl });
  const r = await b.items();
  assert.equal(r.items.length, 3);
  assert.ok(calls.includes("login --apikey") && calls.includes("unlock --passwordenv BW_PASSWORD --raw"));
  assert.ok(!calls.some((c) => c.includes("master")));
  assert.equal(describe(ITEMS[1]).kind, "card");
});

const PERSON = { id: "9", name: "Josh ID", type: 4, notes: "", identity: { firstName: "Sam", lastName: "Owner", phone: "555-0100",
  ssn: "123-45-6789", passportNumber: "X1234567", licenseNumber: "D7654321" }, fields: [{ name: "dob", value: "1980-01-02" }] };
const HOUSE = { id: "10", name: "Bay Ridge — Home", type: 4, notes: "", identity: { address1: "1 Example St", city: "Springfield", state: "NY", postalCode: "11209" },
  fields: [{ name: "gate_code", value: "4321" }] };
const idBackend = { items: async () => ({ ok: true, items: [PERSON, HOUSE] }), totp: async () => null };

test("identity items are read natively; the list shows which fields are sensitive, never a value", async () => {
  const v = createVault({ backend: idBackend, audit: () => {}, send: async () => true, now: () => 0 });
  const out = (await v.list()).items;
  const p = out.find((x) => x.name === "Josh ID");
  assert.equal(p.kind, "identity");
  assert.ok(p.fields.includes("full_name") && p.fields.includes("phone") && p.fields.includes("ssn"));
  assert.deepEqual(p.sensitive.sort(), ["dob", "license_number", "passport_number", "ssn"]);
  assert.ok(out.find((x) => x.name === "Bay Ridge — Home").fields.includes("postal_code"));
  assert.ok(!JSON.stringify(out).includes("123-45-6789") && !JSON.stringify(out).includes("1 Example St"));
});

test("an approved identity fills ordinary fields; each sensitive field needs its own approval, for one use", async () => {
  const log = [];
  const v = createVault({ backend: idBackend, audit: (r) => log.push(r), send: async () => true, now: () => 0 });
  assert.equal((await v.needsApproval("Josh ID")).approval, true);
  assert.deepEqual((await v.needsApproval("Josh ID", ["ssn", "phone"])).sensitive, ["ssn"]);
  await v.authorize("Josh ID", "shipping form");
  assert.equal((await v.prepare("browser", { text: "{{vault:Josh ID#full_name}} {{vault:Josh ID#phone}}" })).params.text, "Sam Owner 555-0100");
  assert.match((await v.prepare("browser", { text: "{{vault:Josh ID#ssn}}" })).reason, /needs its own approval/);
  await v.authorize("Josh ID", "tax form", ["ssn"]);
  assert.equal((await v.prepare("browser", { text: "{{vault:Josh ID#ssn}}" })).params.text, "123-45-6789");
  assert.match((await v.prepare("browser", { text: "{{vault:Josh ID#ssn}}" })).reason, /needs its own approval/);   // used once
  await v.authorize("Bay Ridge — Home", "delivery address");
  assert.equal((await v.prepare("browser", { text: "{{vault:Bay Ridge — Home#postal_code}}" })).params.text, "11209");
  assert.ok(!JSON.stringify(log).includes("123-45-6789"));
});

test("the deferred-tool wrapper is left alone; the value is filled at the inner browser call, never at the wrapper", async () => {
  const { v, log } = vault();
  await v.authorize("Con Ed", "log in");
  const wrapped = { id: "browser", args: { action: "act", request: { kind: "type", ref: "e3", text: "{{vault:Con Ed#password}}" } } };
  assert.equal(await v.prepare("dispatch_action", wrapped), undefined);            // passes through untouched
  assert.equal(wrapped.args.request.text, "{{vault:Con Ed#password}}");
  assert.equal(log.at(-1).action, "deferred");
  assert.equal(log.at(-1).inner, "browser");
  const inner = await v.prepare("browser", wrapped.args);                         // the harness re-runs the hook here
  assert.equal(inner.params.request.text, "S3cret-Pass!");
  assert.ok((await v.prepare("exec", { command: "echo {{vault:Con Ed#password}}" })).block);   // a wrapped shell is still refused
  assert.ok(!JSON.stringify(log).includes("S3cret-Pass!"));
});
