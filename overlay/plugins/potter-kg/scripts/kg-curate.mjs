#!/usr/bin/env node
// kg-curate: the knowledge graph curator as a plain program, for an OpenClaw command job.
//
//   node kg-curate.mjs --db <file> [--apply] [--decider <module> --policy <file>] [--report] [--max-calls N]
//
// Without a decider, or when the decider has no key, only the deterministic part runs (scan + run
// record) and the program exits 0 without printing anything. Silence is a result.
//
// --db        graph file. Else POTTER_KG_DB, else <OPENCLAW_STATE_DIR>/potter-kg/potter-kg.sqlite.
// --decider   a module exporting createDecider({ env, policyPath, taskClass, telemetryPath }) that returns
//             an async decide() function, or null when it cannot run (for example no key). Potter's is
//             overlay/jev/index.js. The curator itself knows no endpoint, model or key.
// --policy    model policy file handed to the decider (which model, timeout, cost ceiling).
// --apply     act on confident decisions (archive, merge, upgrade). Default: propose only.
// --report    print the run record as JSON on stdout.
// --run-file  where the run record is written. Default: kg-curator-last-run.json beside the graph file.
//
// Exit codes: 0 done; 2 bad arguments or no graph path; 3 a decider was configured and every call failed.
import { writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { curate } from "../lib/curate.js";
import { resolveDbPath } from "../lib/paths.js";

// Node 22 announces node:sqlite as experimental on stderr (Node 24 does not). A scheduled job that
// prints on every run is noise, so that one notice is dropped; every other warning still shows.
function quietSqliteNotice() {
  const emit = process.emitWarning;
  process.emitWarning = function (warning, ...rest) {
    if (String(warning?.message ?? warning).startsWith("SQLite is an experimental feature")) return;
    return emit.call(this, warning, ...rest);
  };
}

export async function main(argv, env = process.env, io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) }) {
  let args;
  try {
    args = parseArgs({
      args: argv,
      options: {
        db: { type: "string" }, apply: { type: "boolean", default: false }, report: { type: "boolean", default: false },
        decider: { type: "string" }, policy: { type: "string" }, "task-class": { type: "string", default: "kg_curation" },
        telemetry: { type: "string" }, "run-file": { type: "string" }, "max-calls": { type: "string" },
      },
    }).values;
  } catch (e) {
    io.err(`kg-curate: ${e.message}\n`);
    return 2;
  }
  let dbPath;
  try {
    dbPath = resolveDbPath({ explicit: args.db, env });
  } catch (e) {
    io.err(`kg-curate: ${e.message}\n`);
    return 2;
  }
  if (!dbPath) {
    io.err("kg-curate: no graph file. Pass --db, or set POTTER_KG_DB or OPENCLAW_STATE_DIR.\n");
    return 2;
  }
  const maxCalls = args["max-calls"] === undefined ? undefined : Number(args["max-calls"]);
  if (maxCalls !== undefined && !(Number.isInteger(maxCalls) && maxCalls >= 0)) {
    io.err("kg-curate: --max-calls must be a whole number\n");
    return 2;
  }

  let decide = null;
  if (args.decider) {
    try {
      const mod = await import(pathToFileURL(resolve(args.decider)).href);
      decide = await mod.createDecider({ env, policyPath: args.policy, taskClass: args["task-class"], telemetryPath: args.telemetry });
    } catch (e) {
      // A decider that is named but broken is a configuration error, not a reason to go quiet.
      io.err(`kg-curate: cannot load decider: ${e.message}\n`);
      return 2;
    }
  }

  quietSqliteNotice();
  const { openStore } = await import("../lib/store.js"); // loaded late so the notice filter is in place
  const store = openStore(dbPath);
  let run;
  try {
    run = await curate(store, { decide: decide ?? undefined, apply: args.apply, maxCalls });
  } finally {
    store.close();
  }
  if (dbPath !== ":memory:" || args["run-file"]) {
    writeFileSync(args["run-file"] ?? join(dirname(dbPath), "kg-curator-last-run.json"), JSON.stringify(run, null, 2) + "\n");
  }
  if (args.report) io.out(JSON.stringify(run, null, 2) + "\n");
  if (!run.ok) {
    io.err(`kg-curate: decision model gave no usable answer (${run.errors} of ${run.calls} calls failed)\n`);
    return 3;
  }
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).then((code) => { process.exitCode = code; }, (e) => {
    process.stderr.write(`kg-curate: ${e?.message ?? e}\n`);
    process.exitCode = 1;
  });
}
