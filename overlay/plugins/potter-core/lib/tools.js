// Agent tools for v3.4 (staged memory, responsibilities) and v3.5 (the Notion outbox). Plain JSON Schema, no SDK.
import * as memory from "./memory.js";
import { outboxPending, settle } from "./durable.js";

const ok = (v) => ({ content: [{ type: "text", text: typeof v === "string" ? v : JSON.stringify(v, null, 2) }], details: v });
const fail = (e) => ({ content: [{ type: "text", text: `Error: ${e?.message ?? e}` }], details: { ok: false, error: String(e?.message ?? e) } });
const obj = (properties, required = []) => ({ type: "object", additionalProperties: false, properties, required });

export const OWNER_ONLY_TOOLS = ["memory_review_apply"];

/** paths: { staged, memory, responsibilities, outbox } */
export function buildTools(paths, { now = () => new Date() } = {}) {
  const defs = [
    { name: "memory_stage",
      description: "Propose something durable to remember about the owner (a fact, a preference, an inferred pattern, or a correction of something remembered wrongly). It is NOT remembered until the owner approves it in the review. Use for anything worth knowing next week; give one or two short examples from the conversation.",
      parameters: obj({
        text: { type: "string", minLength: 3, description: "The fact, preference or pattern, in one sentence." },
        kind: { type: "string", enum: ["fact", "preference", "pattern", "correction"] },
        confidence: { type: "number", minimum: 0, maximum: 1, description: "How sure you are (patterns usually below 0.7)." },
        source: { type: "string", description: "Where it came from, e.g. 'owner said so, 17 Nov'." },
        examples: { type: "array", items: { type: "string" }, maxItems: 2 },
        replaces: { type: "string", description: "For a correction: the remembered text it replaces." },
      }, ["text", "kind"]),
      run: (p) => memory.stage(paths.staged, p, now()) },
    { name: "memory_review",
      description: "The list of proposed memories waiting for the owner's review, numbered, ready to send.",
      parameters: obj({}),
      run: () => memory.reviewText(memory.pending(paths.staged)) },
    { name: "memory_review_apply",
      description: "Apply the owner's review decisions (only what the owner said). Approved items are written to MEMORY.md with their provenance; corrections replace the old line.",
      parameters: obj({ decisions: { type: "array", items: obj({
        n: { type: "integer", minimum: 1, description: "Number in the review list." },
        action: { type: "string", enum: ["approve", "reject", "edit", "downgrade"] },
        text: { type: "string", description: "New text, for edit." } }, ["n", "action"]) } }, ["decisions"]),
      run: (p) => memory.apply(paths.staged, paths.memory, p.decisions, now()) },
    { name: "responsibility_track",
      description: "Start tracking a recurring life system you noticed or the owner mentioned (a bill, a renewal, a seasonal house task, a school deadline): cadence, next due date, a short checklist. Tell the owner you started tracking it.",
      parameters: obj({
        name: { type: "string", minLength: 2 },
        cadence: { type: "string", description: "daily, weekly, monthly, quarterly, yearly, or 'every N days'." },
        next_due: { type: "string", description: "YYYY-MM-DD; omitted means one cadence from today." },
        checklist: { type: "array", items: { type: "string" }, maxItems: 12 },
        source: { type: "string" },
      }, ["name", "cadence"]),
      run: (p) => memory.track(paths.responsibilities, p, now()) },
    { name: "responsibility_due",
      description: "Responsibilities due within the next N days (for briefs and check-ins).",
      parameters: obj({ within_days: { type: "integer", minimum: 0, maximum: 365, default: 7 } }),
      run: (p) => memory.due(paths.responsibilities, p.within_days ?? 7, now()) },
    { name: "responsibility_done",
      description: "Mark a responsibility done this time; its next due date moves on by its cadence.",
      parameters: obj({ name: { type: "string", minLength: 2 } }, ["name"]),
      run: (p) => memory.done(paths.responsibilities, p.name, now()) },
    { name: "notion_outbox_list",
      description: "Notion writes that failed and wait to be retried (tool name and arguments to call again).",
      parameters: obj({}),
      run: () => outboxPending(paths.outbox).map(({ key, tool, params, attempts, last_error }) => ({ key, tool, params, attempts, last_error })) },
    { name: "notion_outbox_settle",
      description: "Record the outcome of retrying one outbox item: ok true when the Notion write now succeeded.",
      parameters: obj({ key: { type: "string" }, ok: { type: "boolean" } }, ["key", "ok"]),
      run: (p) => settle(paths.outbox, p.key, p.ok, now()) ?? { message: "no pending item with that key" } },
  ];
  return defs.map(({ run, ...def }) => ({ ...def, async execute(_id, params) {
    try { return ok(await run(params ?? {})); } catch (e) { return fail(e); }
  } }));
}
