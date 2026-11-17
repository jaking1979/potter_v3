// Potter v3.4 memory and responsibilities (ratified T15/T27, T16; Decision Log 11-15, 35-36, 51-53, 63-66).
// Durable memory is never written silently: Potter STAGES a fact, preference or pattern (with confidence, source and
// one or two short examples); the owner reviews the list (approve / edit / downgrade / reject) and only then is it
// written to MEMORY.md, with its provenance. A factual correction replaces what it corrects. Recurring life systems
// Potter notices are tracked as responsibilities: a cadence, the next due date, a short checklist.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const KINDS = ["fact", "preference", "pattern", "correction"];
const SECTION = { fact: "## Facts", preference: "## Preferences", pattern: "## Patterns (inferred; confidence shown)", correction: "## Facts" };

function readJsonl(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8").split("\n").filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
}
function writeAtomic(path, text) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const tmp = `${path}.tmp${process.pid}`;
  writeFileSync(tmp, text, { mode: 0o600 });
  renameSync(tmp, path);
}
const norm = (s) => String(s ?? "").toLowerCase().replace(/\s+/g, " ").trim();
const idOf = (text) => "m" + createHash("sha256").update(norm(text)).digest("hex").slice(0, 8);

export function stage(path, { text, kind = "fact", confidence = 0.6, source = "", examples = [], replaces = "" }, now = new Date()) {
  const t = String(text ?? "").trim();
  if (t.length < 3) throw new Error("nothing to remember");
  if (!KINDS.includes(kind)) throw new Error(`kind must be one of ${KINDS.join(", ")}`);
  const id = idOf(t);
  const all = readJsonl(path);
  if (all.some((r) => r.id === id && r.status === "pending")) return { id, status: "already staged" };
  const rec = { id, status: "pending", kind, text: t.slice(0, 500), confidence: Math.max(0, Math.min(1, Number(confidence) || 0)),
    source: String(source).slice(0, 200), examples: (examples ?? []).slice(0, 2).map((e) => String(e).slice(0, 200)),
    replaces: String(replaces ?? "").slice(0, 300), staged: now.toISOString() };
  writeAtomic(path, [...all, rec].map((r) => JSON.stringify(r)).join("\n") + "\n");
  return { id, status: "staged" };
}

export function pending(path) {
  return readJsonl(path).filter((r) => r.status === "pending");
}

export function reviewText(items) {
  if (!items.length) return "Nothing is waiting for your review.";
  const lines = items.map((r, i) => {
    const conf = r.kind === "pattern" ? ` (confidence ${Math.round(r.confidence * 100)}%)` : "";
    const ex = r.examples?.length ? `\n   e.g. ${r.examples.join(" / ")}` : "";
    return `${i + 1}. [${r.kind}${conf}] ${r.text}${r.replaces ? `\n   replaces: ${r.replaces}` : ""}${ex}`;
  });
  return ["Memory review: what I would keep about you. Reply with what to do, e.g. \"approve 1 3, reject 2, edit 4: <new text>, downgrade 5\".", ...lines].join("\n");
}

/** decisions: [{ id | n, action: approve|reject|edit|downgrade, text? }]. Returns what changed. */
export function apply(stagePath, memoryPath, decisions, now = new Date()) {
  const all = readJsonl(stagePath);
  const waiting = all.filter((r) => r.status === "pending");
  let mem = existsSync(memoryPath) ? readFileSync(memoryPath, "utf8") : "# Memory\n\nDurable facts the owner approved. Potter proposes; the owner decides.\n";
  const done = [];
  for (const d of decisions ?? []) {
    const r = d.id ? waiting.find((x) => x.id === d.id) : waiting[Number(d.n) - 1];
    if (!r || r.status !== "pending") continue;
    if (d.action === "reject") { r.status = "rejected"; done.push({ id: r.id, action: "rejected" }); continue; }
    if (d.action === "downgrade") { r.kind = "pattern"; r.confidence = Math.min(r.confidence, 0.4); }
    if (d.action === "edit" && d.text) r.text = String(d.text).slice(0, 500);
    if (!["approve", "edit", "downgrade"].includes(d.action)) continue;
    if (r.replaces) {
      const old = norm(r.replaces);
      mem = mem.split("\n").filter((l) => !(l.startsWith("- ") && norm(l).includes(old))).join("\n");
    }
    const head = SECTION[r.kind];
    if (!mem.includes(head)) mem = mem.trimEnd() + `\n\n${head}\n`;
    const conf = r.kind === "pattern" ? ` (confidence ${Math.round(r.confidence * 100)}%)` : "";
    const line = `- ${r.text}${conf} <!-- ${r.id} | ${r.source || "conversation"} | approved ${now.toISOString().slice(0, 10)} -->`;
    const at = mem.indexOf(head) + head.length;
    mem = mem.slice(0, at) + "\n" + line + mem.slice(at);
    r.status = "approved";
    done.push({ id: r.id, action: d.action === "approve" ? "approved" : d.action + "ed and approved" });
  }
  writeAtomic(memoryPath, mem.replace(/\n{3,}/g, "\n\n"));
  writeAtomic(stagePath, all.map((r) => JSON.stringify(r)).join("\n") + "\n");
  return done;
}

// ---- responsibilities ------------------------------------------------------------------------------------------
const DAY = 86400000;
export function nextDue(from, cadence) {
  const d = new Date(from);
  const m = String(cadence).match(/^every (\d+) days?$/);
  if (m) return new Date(d.getTime() + Number(m[1]) * DAY);
  if (cadence === "daily") return new Date(d.getTime() + DAY);
  if (cadence === "weekly") return new Date(d.getTime() + 7 * DAY);
  if (cadence === "monthly") { d.setUTCMonth(d.getUTCMonth() + 1); return d; }
  if (cadence === "quarterly") { d.setUTCMonth(d.getUTCMonth() + 3); return d; }
  if (cadence === "yearly") { d.setUTCFullYear(d.getUTCFullYear() + 1); return d; }
  throw new Error("cadence: daily, weekly, monthly, quarterly, yearly or 'every N days'");
}
function loadResp(path) {
  try { return JSON.parse(readFileSync(path, "utf8")); } catch { return { items: [] }; }
}
export function track(path, { name, cadence, next_due, checklist = [], source = "" }, now = new Date()) {
  const n = String(name ?? "").trim();
  if (!n) throw new Error("a responsibility needs a name");
  nextDue(now, cadence);                                         // validates the cadence
  const db = loadResp(path);
  const due = next_due ? new Date(next_due) : nextDue(now, cadence);
  if (Number.isNaN(due.getTime())) throw new Error("next_due must be a date");
  const item = { name: n, cadence, next_due: due.toISOString().slice(0, 10), checklist: checklist.slice(0, 12).map(String),
    source: String(source).slice(0, 200), since: now.toISOString().slice(0, 10), told_owner: false };
  db.items = [...db.items.filter((i) => norm(i.name) !== norm(n)), item];
  writeAtomic(path, JSON.stringify(db, null, 1));
  return item;
}
export function due(path, withinDays = 7, now = new Date()) {
  const limit = new Date(now.getTime() + withinDays * DAY).toISOString().slice(0, 10);
  return loadResp(path).items.filter((i) => i.next_due <= limit).sort((a, b) => a.next_due.localeCompare(b.next_due));
}
export function done(path, name, now = new Date()) {
  const db = loadResp(path);
  const i = db.items.find((x) => norm(x.name) === norm(name));
  if (!i) throw new Error(`no responsibility named "${name}"`);
  i.last_done = now.toISOString().slice(0, 10);
  i.next_due = nextDue(new Date(Math.max(now.getTime(), new Date(i.next_due).getTime())), i.cadence).toISOString().slice(0, 10);
  writeAtomic(path, JSON.stringify(db, null, 1));
  return i;
}
