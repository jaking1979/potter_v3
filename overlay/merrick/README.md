# Merrick (home specialist), v3

Plain Python, standard library only, Python 3.12. Nothing here talks to a model, and nothing here opens a network
connection: Home Assistant is reached through an injected client (in tests, a fake; in command jobs, a bridge
command supplied at install, see `habridge.py`).

## Why it is built this way
The owner's complaint about v2: sometimes fast and perfect, sometimes slow and lost. v2 had a fast path (one
decision-model call, then fixed code) and a slow path (a full conversational agent on a small model) for anything
the fast path did not recognise, and nothing recorded which path a request took. v3:

| Problem in v2 | v3 answer | File |
|---|---|---|
| Every request, even "set heat to 68", needed a model call to be routed | Pattern table first; the model is asked only when the table does not recognise the request | `router.py` |
| Misses were invisible | One JSON line per request: intent or "fallback", path, duration, outcome, reason; a report with counts and the commonest misses | `pathlog.py` |
| The slow path wandered silently | A deadline, and at the deadline a report of what is observed / estimated / unknown | `budget.py`, `cli.py ask` / `budget-check` / `fallback-end` |
| "Done" was reported without checking | No result is ok unless the state read back from Home Assistant matches | `ha.py` |
| Safety rules were prose | Locks, alarms, water valves refused in code; setpoints clamped to 50-74 F in code | `ha.py` |
| Watcher mixed reading, judging and printing, with fixed entity ids | A pure function over readings that returns findings | `coldwatch.py` |

## Files
- `router.py` request -> intent (table, then optional injected decision function), or fallback with a reason.
- `fastpath.py` runs a recognised intent with verified calls and writes the path log line.
- `pathlog.py` the log and its report.
- `budget.py` deadline and partial report for the slow path.
- `ha.py` the safety layer over an injected Home Assistant client. `habridge.py` is the client for command jobs: it speaks JSON to a bridge command and holds no address or token.
- `coldwatch.py` cold-weather findings. `thermal.py` thermal arithmetic. `intentions.py` one-shot and period-bound actions.
- `cli.py`, `__main__.py` the `merrick` command. `testing.py` an in-memory Home Assistant for tests.
- `house.example.json` shape of the house file. No entity id is hard-coded in the code; the real file is written at install.

## Adding a recognised request (how the fast path gets wider)
1. Run `merrick report`. Take the most common miss.
2. If plain code can do it: add a pattern in `router.py`, a handler in `fastpath.py`, and tests for both.
3. If it truly needs judgment, leave it on the slow path. Its reason will keep being counted.

## Carried over from v2 (understood and tested)
- Clamp 50-74 F; protected devices; perimeter check before a departure.
- Cold-watch thresholds: basement below 40 F alert, below 36 F urgent; vacancy hold drift; main zone reading under 58 F.
- Heating-time estimate (rate, 15% outdoor adjustment under 30 F / over 45 F, x1.25 / x0.80 range, thermal lag).
- Episode split (active heating / thermal carry-over / passive cooling) and the running rate calibration.
- Dew point, slope between samples (45 minute minimum), hours to freezing.
- One-shot intentions with a 15 minute late window; hourly room snapshot of every listed sensor.

## Deliberately changed from v2
- **Out-of-range setpoints.** v2's fast path only recognised numbers 50-74 and otherwise silently used 68 ("set heat to 80" set 68). v3 clamps the owner's number and says so.
- **Read-back.** v2's fast path returned the raw service reply; v2's engine wrote "Successfully set" whatever came back. v3 marks an action ok only on a matching read-back, retries the read briefly, and otherwise says NOT confirmed.
- **Departure.** v2's fast path locked the door and armed the alarm on a model's say-so, against its own rule that these need explicit confirmation. v3 runs the perimeter check and refuses the lock and alarm.
- **Calibration bug.** v2's learner looked for episode types `heating` / `cooling`; its collector wrote `active_heating` / `passive_cooling`, so the hourly collector never taught the model anything (the export's episode file shows about 6,000 uncounted episodes against 50 counted). v3 learns from the types the collector produces.
- **Double counting.** v2 re-read the last 24 hours every hour and appended the same episodes each time. v3 keeps a per-zone "learned until" mark, and does not count the stretch still in progress.
- **Short stretches.** v3 learns only from stretches of 30 minutes or more (v2: 12). v2's learned cooling rate for the main zone was five times its starting assumption, which looks like sensor rounding over short stretches.
- **Labels.** v2 labelled a prediction "observed" once it had 10 samples. A prediction is always "estimated"; v3 says separately what the rate rests on.
- **Unknown outdoor temperature.** v2 assumed 40-45 F. v3 makes no adjustment and says the value is unknown.
- **Room truth.** A zone's room temperature comes from the house file's `room_sensor`. If that sensor has no reading the answer is "unknown"; the thermostat's own thermistor is never substituted. (v2's house file still listed one as a fallback.)
- **Hold drift.** Not raised when the house is known to be occupied (v2's own protocol said "during vacancy"; its script checked always). Checked when occupancy is unknown, as v2 did.
- **Unreadable basement sensor.** v2 posted a warning in chat every two hours. v3 says nothing in chat, exits non-zero, and the schedule watchdog reports the failed job.
- **Repeat alerts.** v2 repeated the same alert every two hours. v3 alerts when a problem appears or worsens, then every 12 hours while it lasts.
- **Freeze trajectory.** Added to the watcher from v2's protocol notes (threshold 3: under 24 hours to freezing), using the previous run's basement sample.
- **Late intentions.** An ordinary one-shot more than 15 minutes late is not run (as v2). The return leg of a period-bound instruction always runs, however late, so the house is not left warm.
- **Failed intentions.** v2 marked an intention completed without confirming. v3 retries up to 3 ticks, then marks it failed and tells the owner.
- **No model numbers carried.** v2's learned rates are not imported (see above); zones start from the house file's starting assumptions.

## TODO: not ported, and why
1. **Arrival plans with closed-loop replanning** (`create_arrival_plan`, `reassess_and_replan`). The start-time arithmetic is here (`thermal.heating_estimate`); the replanning loop is not. In v2 it changed two zones' setpoints without read-back, assumed 55 F / 40 F / 60 F when readings were missing, and the export holds no completed plan to test against. "Have the house warm by Friday 7 pm" is logged as `scheduled_or_conditional` and goes to the slow path, which can register a one-shot with `merrick intend`.
2. **Scheduled and conditional requests on the fast path** ("at 5 pm", "for the weekend", "once it hits 68"). Parsing times and conditions from free text needs the owner's real phrasings; the path log will supply them. The machinery to run them exists (`intentions.py`); only the parsing is missing. Condition-triggered one-shots ("once it hits 68") have no engine yet.
3. **Whole-house state file** (`merrick_environmental.py`: occupancy inference, stratification, moisture decay, sensor-drift scoring). Only dew point, slope and hours-to-freeze are ported. The rest depends on a sensor registry with per-sensor bias figures that must be re-measured, not copied.
4. **Cold baseline sampler and protocol** (`merrick_cold_baseline.py`, thresholds 1, 4 (two consecutive samples), 5, 7, 8 of the protocol table). They were "recommended for arming", not live in v2's watcher.
5. **Automation outcome tracking and research backlog** (`merrick_outcomes.py`, `research_backlog.json`, fan and fireplace experiments). Judgment work for the agent; no deterministic part worth porting yet.
6. **Outdoor-bracket rate tables** (`by_outdoor_temp_bracket`). v2 stored them but its estimate never read them.
7. **The decision-model call itself.** `router.route(decide=...)` and `merrick ask --decide-cmd` accept one; which model and endpoint is the model-policy step's decision. Until then the table alone routes.
8. **A second property.** The house file describes one house. Words listed under `unsupported_location_words` send a request to the slow path with reason `other_location`.
9. **Fans, lights, plugs on the fast path.** `SafeHome.switch` is ready and tested (allowlisted devices only); no intent uses it yet. Wait for the path log to show the demand.
10. **Analysis scripts** (`analyze_*.py`, `eval_suite.py`). One-off investigations, not part of the running system.
11. **The Home Assistant bridge command.** The repo's lint refuses any network client in Python, so the transport is not in this package. `habridge.py` defines the JSON contract; the installer must supply the command (see `docs/wiring/merrick.md`). Until it does, `ask`, `cold-watch`, `collect` and `tick` stop with "Home Assistant is not configured".
12. **Celsius.** The clamp assumes Fahrenheit; a house file with another unit has setpoints refused.

## Tests
`python -B -m unittest tests.test_merrick_router` (and `_ha`, `_pathlog`, `_budget`, `_coldwatch`, `_thermal`,
`_intentions`, `_fastpath`, `_cli`). No network, no skips.
