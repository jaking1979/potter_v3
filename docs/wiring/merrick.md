# Wiring: Merrick (home specialist)

What the main session must add to the installer for build step 5. Nothing here has been run against a live
OpenClaw or Home Assistant; see "Not verified" at the end. Code: `overlay/merrick/`. Agent files:
`overlay/workspace-merrick/`. Tests: `tests/test_merrick_*.py`.

## 1. Files to install
| From the repo | To | Notes |
|---|---|---|
| `overlay/merrick/` (whole directory) | `$POTTER_HOME/merrick/` | Runs as `python3 -B $POTTER_HOME/merrick <command>`; no PYTHONPATH needed. `testing.py` and `house.example.json` may be left out. |
| `overlay/workspace-merrick/{AGENTS,IDENTITY,SOUL}.md` | the Merrick agent's workspace root | Plain copy; no placeholders to render. |
| (generated) house file | `$MERRICK_STATE_DIR/house.json` | See section 4. Not in the repo: it holds the owner's entity ids. |
| (generated) wrapper | `/usr/local/bin/merrick` | See below. The agent files and the jobs call `merrick`, never a path. |

Wrapper (one place for settings, so jobs and the agent cannot disagree):

```sh
#!/bin/sh
export MERRICK_STATE_DIR="<merrick workspace>/state"
export MERRICK_HOUSE="$MERRICK_STATE_DIR/house.json"
export MERRICK_HA_CMD="<the Home Assistant bridge command, section 4>"
exec python3 -B "$POTTER_HOME/merrick" "$@"
```

State files Merrick writes under `$MERRICK_STATE_DIR`: `path-log.jsonl`, `cold-watch.json`, `intentions.json`,
`model.json`, `room-snapshots.jsonl`, `thermal-episodes.jsonl`, `.lock`.

## 2. The OpenClaw agent
| Setting | Value |
|---|---|
| Agent id / name | `merrick` / Merrick |
| Create with | `openclaw agents add merrick --workspace <dir> --model <id> --non-interactive` (Gateway running), then copy the three workspace files, then `openclaw agents set-identity --agent merrick --from-identity` |
| Workspace | a directory next to Potter's workspace (v2 used `workspace/merrick`); read it back with `openclaw config get agents.entries.merrick.workspace` |
| Model | A capable model, not the small one v2's fallback ran on. This is the "stronger model" item in the design doc. The exact id belongs to the model-policy step. |
| Channel bindings | None. The owner talks to Potter; Potter delegates. |
| Delegation | Potter must be allowed to spawn it: `agents.entries.potter.subagents.allowAgents: ["merrick"]` (validated config patch). |
| Hard time limit | `agents.entries.merrick.subagents.runTimeoutSeconds` or the spawn call's own timeout: **120 seconds**. This is the outer stop. The inner budget (`merrick ask --budget`, default 90 s) is what makes Merrick report before the outer stop cuts it off. |
| Tools | exec (to run `merrick`), the Home Assistant MCP server for reads. Home Assistant **write** tools should be denied to this agent if the tool policy allows it per agent: all writes go through `merrick`, which clamps, refuses and reads back. |
| Built-in jobs | v2's `heartbeat-merrick` and `skill-collection-review-merrick` are not carried (design doc, "What is NOT carried"). |

Potter's side (existing file, not edited by this step): `overlay/workspace/skills/home-status/SKILL.md` should say:
for any home request, first run `merrick ask "<the owner's words>"`. If `path` is `fast`, relay `reply`. If
`fallback`, tell the owner in one line that Merrick is looking into it, then spawn the `merrick` agent with the
request and the JSON (`id`, `started_epoch`, `deadline_epoch`). This keeps a recognised request at one command
and no model call on Merrick's side.

## 3. Scheduled jobs
All are command jobs (no model). All use `--declaration-key` equal to the name, like the existing jobs.

| Name | Schedule | Time zone | Command | Timeout | Delivery | Starts in standby? |
|---|---|---|---|---|---|---|
| `potter-merrick-cold-watch` | `--cron "0 */2 * * *" --exact` | `--tz "$TIMEZONE"` | `merrick --openclaw 'openclaw <profile flags>' cold-watch --to $OWNER_TELEGRAM_ID` | 90 s | `--no-deliver` (the command sends its own message, only when there is a finding) | **No.** Reads only. Until v2's watcher is stopped the owner gets each alert twice; see decision 1. |
| `potter-merrick-collect` | `--cron "7 * * * *" --exact` | `--tz "$TIMEZONE"` | `merrick collect` | 180 s | `--no-deliver` | **No.** Reads Home Assistant, writes only its own files. |
| `potter-merrick-tick` | `--cron "*/5 * * * *" --exact` | `--tz "$TIMEZONE"` | `merrick --openclaw 'openclaw <profile flags>' tick --to $OWNER_TELEGRAM_ID` | 120 s | `--no-deliver` (sends its own one-line result when an intention runs, fails or expires) | **Yes, `--disabled`.** It changes thermostat setpoints on devices v2 also controls. Enable at handover ("take over the house"). |

Behaviour the schedule watchdog relies on:
- `cold-watch` exits 1 when the basement sensor cannot be read or its message could not be delivered. It prints
  nothing to chat in that case; the watchdog reports the failed job.
- `collect` exits 1 if any zone's history could not be read.
- `tick` exits 1 if an intention failed or its message could not be delivered. With nothing pending it does not
  contact Home Assistant at all.

After creating each job: list it, confirm the next run time, and run `cold-watch --dry-run` once (safe: reads
only, prints instead of sending, saves nothing).

Optional, not created by default: a weekly `merrick report --days 7` for the owner or for QC (decision 4).

The Merrick agent itself is not a job. It changes devices only when asked, through `merrick ask`.

## 4. Configuration and secrets (names only)
| Name | Kind | Where it comes from |
|---|---|---|
| `HA_URL` | setting | setup form (already collected). Used by the bridge command only. |
| `HA_TOKEN` | **secret** | setup form (already collected). Used by the bridge command only; Merrick's code never sees it. Today it is only placed in the MCP server entry, so the bridge needs it stored where a command job can read it (for example a mode-600 file under `$SECRETS_DIR`). |
| `MERRICK_HA_CMD` | setting | the bridge command line, set in the wrapper |
| `OWNER_TELEGRAM_ID`, `TIMEZONE` | settings | already collected |
| `MERRICK_STATE_DIR`, `MERRICK_HOUSE` | environment, set by the wrapper | derived |
| House file `house.json` | config | generated; shape in `overlay/merrick/house.example.json` |
| Decision command (`merrick ask --decide-cmd`) | optional | model-policy step; without it routing is table-only |

**The Home Assistant bridge command (must be supplied; decision 0).** Scheduled command jobs have no MCP tools,
and the repo's lint (`notifier/lint.py`) refuses any network client import in Python, so `overlay/merrick/` holds
no transport. `habridge.py` runs the command in `MERRICK_HA_CMD` once per operation, writes one JSON object to
its stdin and expects one on stdout:

| Request on stdin | Home Assistant REST call it stands for (as v2 used) | Reply on stdout |
|---|---|---|
| `{"op":"get_state","entity_id":E}` | `GET /api/states/E` (404 means missing) | `{"ok":true,"state":{...}}` or `{"ok":true,"state":null}` |
| `{"op":"call_service","domain":D,"service":S,"data":{...}}` | `POST /api/services/D/S` with `data` as the body | `{"ok":true}` |
| `{"op":"history","entity_id":E,"start":T}` | `GET /api/history/period/T?filter_entity_id=E` (first list of the reply) | `{"ok":true,"rows":[...]}` |

Any failure: non-zero exit, or `{"ok":false}`. The bridge must time out by itself in under 20 seconds and must
not print the token. The safety rules do not depend on the bridge: clamping, refusals and read-back all happen
before and after it, in `ha.py`.

House file contents the installer must fill from the owner's Home Assistant and smart-home rules:
`zones` (id, name, `climate_entity`, `aliases`, and `room_sensor` = the sensor the owner's rules name as
authoritative for that room), `default_zone`, `sensors` (every room temperature sensor, with aliases; the
basement one is required by the watcher), `perimeter` (door and window sensors by name), `security_read` (lock
and alarm entities, read-only), `controllable` (extra switches or fans Merrick may operate; may be empty),
`protected_entities` (anything else that must never be written, such as a water valve exposed as a switch),
`weather_entity`, `vacancy_entity` + `vacancy_on_state`, `away_hold_f`, `cold_watch` thresholds, and
`unsupported_location_words` (how the owner refers to the second property). v2's
`merrick/data/house_entities.json` and `merrick/state/temperature_sensors_canonical.json` in the export are the
source for most of this.

## 5. Decisions for the main session / the owner
0. **How command jobs reach Home Assistant.** Either (a) write the bridge command outside Python's linted tree
   (a few lines of shell around an HTTP tool, reading the token from a root-only file), (b) grant the lint an
   explicit exception for one audited transport file, or (c) route through the Gateway's own tool-invoke
   endpoint to the Home Assistant MCP server. Until one exists the three jobs and `merrick ask` cannot run.
   (`overlay/tools/monitor_lib.py`, from another step, currently trips the same lint; it needs the same decision.)
1. **Cold-watch during the overlap with v2.** Enabled from day one (recommended: two alerts are better than
   none) or standby until v2's watcher is stopped.
2. **Away hold value.** v2's watcher treats 62 F as the vacancy hold; v2's fast path set 65 F for "away". The
   house file needs one number.
3. **Departure ("close up the house").** v3 checks the perimeter and refuses to lock or arm, per the step's
   fail-closed rule. v2's fast path did lock and arm. If the owner wants that back it needs an explicit
   confirmation step, not a silent fast-path action.
4. **Who reads the path report, and when.** Weekly to the owner, or folded into QC.
5. **Where Merrick reads the owner's smart-home rules.** Merrick's workspace is separate from Potter's rule tree.
   Either the relevant facts live in the house file (recommended: one source), or the rule file is also made
   readable to Merrick.
6. **Second property.** Not modelled. Requests naming it go to the slow path.
7. **Fallback budget.** 90 s inner / 120 s outer are starting values; tune from `merrick report`.

## 6. Not verified
- No OpenClaw command in this document was run: the installed CLI does not start on this machine's Node version.
  `agents add` flags, `subagents.allowAgents`, `subagents.runTimeoutSeconds` and `--command` / `--timeout-seconds`
  / `--tz` / `--exact` were read in the bundled 2026.11 docs. `--disabled` and `--declaration-key` are used by
  the existing installer but were not found in those docs.
- Whether per-agent tool policy can deny the Home Assistant MCP write tools to `merrick` only.
- Whether a spawned agent run is actually stopped at `runTimeoutSeconds`, and what the owner sees when it is.
- The Home Assistant REST shapes in section 4 are the ones v2's scripts used; they were not called. The history
  parsing in particular (attributes present on every row) is untested against a live server.
- No bridge command exists yet, so nothing has run end to end; every test uses an in-memory Home Assistant.
- That the house's climate entities report Fahrenheit (the clamp assumes it; a non-F house file is refused).
- The router's pattern table was written from v2's examples, not from real request history. The first week of
  `path-log.jsonl` is what tunes it.
- A request made from the slow path through `merrick ask` adds its own line to the path log, so a single owner
  request that falls back and then sets a temperature counts as two requests.
