import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as g from "./guards.js";

const ID = "aaaabbbbccccddddeeeeffff00001111";
const cfg = { excludedIds: [ID] };

test("hard wall matches dashed, uppercased and nested forms, and nothing else", () => {
  assert.equal(g.referencesExcluded({ page: ID }, [], [ID]), true);
  assert.equal(g.referencesExcluded({ a: { b: ["x", "AAAABBBB-CCCC-DDDD-EEEE-FFFF00001111"] } }, [], [ID]), true);
  assert.equal(g.referencesExcluded({}, ["/notion/" + ID], [ID]), true);
  assert.equal(g.referencesExcluded({ page: "aaaabbbbccccddddeeeeffff00001112" }, [], [ID]), false);
  assert.equal(g.referencesExcluded({ page: ID }, [], []), false);
  assert.equal(g.referencesExcluded({ page: "anything" }, [], ["short", "", null]), false);
});

test("decide: hard wall wins over everything and applies to every tool", () => {
  const m = new Map();
  for (const tool of ["read", "exec", "notion_fetch", "web_fetch"]) {
    assert.equal(g.decide({ tool, params: { q: ID }, sessionKey: "s" }, cfg, m, 0).guard, "HARD_WALL");
  }
});

test("docs gate: build verbs are blocked until a docs read, then allowed for 10 minutes, per session", () => {
  const m = new Map();
  const build = { tool: "exec", params: { command: "openclaw --profile x cron add --name a" }, sessionKey: "s1" };
  assert.equal(g.decide(build, cfg, m, 1000).guard, "DOCS_CONSULT_GATE");
  assert.equal(g.decide({ tool: "read", params: { path: "/opt/x/node_modules/openclaw/docs/automation/cron-jobs.md" }, sessionKey: "s1" }, cfg, m, 2000), null);
  assert.equal(g.decide(build, cfg, m, 2000 + g.DOCS_READ_WINDOW_MS), null);
  assert.equal(g.decide(build, cfg, m, 2001 + g.DOCS_READ_WINDOW_MS).guard, "DOCS_CONSULT_GATE");
  assert.equal(g.decide({ ...build, sessionKey: "s2" }, cfg, m, 2500).guard, "DOCS_CONSULT_GATE");
  assert.equal(g.decide({ ...build, sessionKey: undefined }, cfg, m, 2500).guard, "DOCS_CONSULT_GATE");
  assert.equal(g.decide(build, { ...cfg, docsGate: false }, new Map(), 0), null);
});

test("docs gate: what counts as a build and what does not", () => {
  for (const c of ["openclaw cron add x", "openclaw cron edit x", "openclaw gateway restart", "systemctl --user restart foo", "crontab -e"]) {
    assert.equal(g.isBuildExecCommand(c), true, c);
  }
  for (const c of ["openclaw cron list", "openclaw gateway status", "systemctl status foo", "ls", ""]) {
    assert.equal(g.isBuildExecCommand(c), false, c);
  }
  assert.equal(g.isBuildVerb("cron", { action: "add" }), true);
  assert.equal(g.isBuildVerb("cron", { action: "list" }), false);
  assert.equal(g.isBuildVerb("read", { action: "add" }), false);
  assert.equal(g.isQualifyingDocsPath("/w/state/openclaw-docs-index.md"), true);
  assert.equal(g.isQualifyingDocsPath("/w/wiki/potter-manual/index.md"), true);
  assert.equal(g.isQualifyingDocsPath("/w/notes/cron.md"), false);
});

test("config write guard: direct writes blocked on any profile, reads and CLI changes allowed", () => {
  const blocked = [
    ["write", { path: "/root/.openclaw/openclaw.json" }],
    ["edit", { file: "~/.openclaw-potter/openclaw.json" }],
    ["apply_patch", { input: "*** Update File: /root/.openclaw/openclaw.json" }],
    ["exec", { command: "sed -i 's/a/b/' ~/.openclaw/openclaw.json" }],
    ["exec", { command: "echo '{}' > /root/.openclaw/openclaw.json" }],
    ["exec", { command: "jq . x | tee /root/.openclaw/openclaw.json" }],
    ["exec", { command: "cp /tmp/new.json /root/.openclaw/openclaw.json" }],
  ];
  for (const [tool, params] of blocked) assert.equal(g.isConfigDirectWrite(tool, params), true, JSON.stringify(params));
  const allowed = [
    ["read", { path: "/root/.openclaw/openclaw.json" }],
    ["exec", { command: "cat ~/.openclaw/openclaw.json | jq .tools" }],
    ["exec", { command: "openclaw config patch --file /tmp/p.json5" }],
    ["exec", { command: "cp /root/.openclaw/openclaw.json /tmp/backup.json" }],
    ["write", { path: "/root/workspace/openclaw.json" }],
    ["write", { path: "notes/.openclaw/openclaw.json" }],
  ];
  for (const [tool, params] of allowed) assert.equal(g.isConfigDirectWrite(tool, params), false, JSON.stringify(params));
  assert.equal(g.decide({ tool: "write", params: { path: "/root/.openclaw/openclaw.json" } }, cfg, new Map(), 0).guard, "CONFIG_WRITE_GUARD");
  assert.equal(g.decide({ tool: "write", params: { path: "/root/.openclaw/openclaw.json" } }, { configWriteGuard: false }, new Map(), 0), null);
});

test("ordinary calls pass, and the audit log never contains params", () => {
  assert.equal(g.decide({ tool: "read", params: { path: "/w/notes.md" }, sessionKey: "s" }, cfg, new Map(), 0), null);
  const log = join(mkdtempSync(join(tmpdir(), "pg-")), "audit.log");
  g.audit(log, { guard: "HARD_WALL", tool: "read", action: "block" });
  const line = JSON.parse(readFileSync(log, "utf8").trim());
  assert.deepEqual(Object.keys(line).sort(), ["action", "guard", "tool", "ts"]);
  g.audit("/nonexistent-dir/x/y.log", { guard: "x" }); // must not throw
});
