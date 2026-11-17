# Wiring: nightly quality control and the finance specialist

What the main session must add to the installer for build steps 8 (finance, standby) and 9 (QC). Nothing here has
been installed, and no install script was touched. Paths use the installer's own variables: `$POTTER_HOME`
(tools), `$WS` (the potter agent's workspace), `$OWNER_TELEGRAM_ID`, `oc` (the profile-aware `openclaw` wrapper).

Source read: v2 export, `inventory/cron.json`, `inventory/mcp.json`, `workspace/scripts/quality-control/`,
`workspace/rules/global/quality-control.md`, `workspace/main/trading/crypto-alpha-bot/`, `workspace/trading/`,
`workspace/rules/domains/finance.md`, `workspace/rules/projects/crypto-tracking.md`.

## 1. Files this step added

| File | What it is |
|---|---|
| `overlay/tools/qc/{__init__,common,recorder,verifier}.py` | nightly QC, stdlib only |
| `overlay/workspace/rules/global/quality-control.md` | the v3 rule |
| `tests/test_qc.py` | 39 tests |
| `overlay/tools/trading_precheck.py` | the trading gate and run log, stdlib only, no network |
| `tests/test_trading_precheck.py` | 29 tests |
| `overlay/workspace/skills/trading-run/SKILL.md` | how the scheduled trading run behaves |
| `overlay/workspace/skills/trading-run/MANDATE.template.md` | blank mandate form (cannot authorise a run as shipped) |

Installer changes these need:
- **Copy the `qc` directory.** The installer copies `overlay/tools/*.py` only. Add
  `cp -r "$REPO/overlay/tools/qc" "$POTTER_HOME/tools/"`. `verifier.py` imports `schedule_watch` from the parent
  directory, so `qc/` must sit next to `schedule_watch.py`.
- **Rules index.** Add a row to `overlay/workspace/rules/INDEX.md` (I did not edit it):
  `| A quality-control report, "did you really do that", the nightly check | global/quality-control.md |`
  and under stores: `| Nightly QC claims, verdicts, follow-ups | state/quality-control/ |` and
  `| Trading mandate, stop file, run log | trading/MANDATE.md, trading/STOP, trading/logs/trading-run.jsonl |`.
- `mkdir -p "$WS/state/quality-control" "$WS/trading/logs"`.

## 2. Jobs

Schedules and time zone are v2's, unchanged. Every job: `--exact`, explicit `--tz America/New_York`, explicit
timeout. STANDBY = created with `--disabled` and listed in `/etc/potter/handover.json`.

| v3 name | v2 name | Schedule (America/New_York) | Kind | Timeout | Delivery | STANDBY |
|---|---|---|---|---|---|---|
| `potter-qc-recorder` | QC Recorder | `30 0 * * *` | command | 1800 s | none | no |
| `potter-qc-verifier` | QC Verifier | `0 1 * * *` | command | 600 s | none by the scheduler; the program sends one Telegram message itself when something fails (v2: announce to Telegram) | no |
| `potter-qc-recheck` | QC Recheck | `5 * * * *` | command | 300 s | none | no |
| `potter-daily-trading` | daily-alpha-trading | `0 17 * * 1-5` | agentTurn, isolated | **v2 had none**; proposed 900 s | none by the scheduler (v2: `delivery_mode: none`); the skill sends one line only when orders were placed or the run was refused | **yes** |
| `potter-llm-monthly-rebalance` | llm-monthly-rebalance (id `329d9b9a…`) | `35 9 1 * *` | agentTurn, isolated | v2 had none; proposed 900 s | announce to the owner's Telegram | **yes** (also disabled in v2) |
| `potter-llm-monthly-rebalance-daily` | llm-monthly-rebalance (id `ddea39b1…`, same name in v2) | `45 13 * * 1-5` | agentTurn, isolated | v2 had none; proposed 900 s | announce to the owner's Telegram | **yes** (also disabled in v2) |
| `potter-llm-monthly-status` | llm-monthly-status | `0 18 * * 5` | agentTurn, isolated | v2 had none; proposed 300 s | announce to the owner's Telegram | **yes** (also disabled in v2) |

v2 has two disabled jobs both called `llm-monthly-rebalance`. The weekday one pairs with v2's
`rebalance-trigger.py` (fires only on the last trading weekday of a month); the day-1 one is older. v2's own
README says the monthly portfolio was liquidated and both jobs were switched off on purpose. v3 needs at most
one of them. Recommended: install neither until the owner asks for monthly scoring again (see section 7).

### Commands

QC (not standby; they read only v3's own transcripts and workspace):

```
oc cron add --name potter-qc-recorder --declaration-key potter-qc-recorder --cron "30 0 * * *" \
   --tz America/New_York --exact --no-deliver --timeout-seconds 1800 \
   --command "python3 $POTTER_HOME/tools/qc/recorder.py --sessions <potter agent sessions dir> --state $WS/state/quality-control --hours 26 --tz America/New_York"

oc cron add --name potter-qc-verifier --declaration-key potter-qc-verifier --cron "0 1 * * *" \
   --tz America/New_York --exact --no-deliver --timeout-seconds 600 \
   --command "python3 $POTTER_HOME/tools/qc/verifier.py --mode verify --state $WS/state/quality-control --workspace $WS --tz America/New_York --to $OWNER_TELEGRAM_ID --openclaw 'openclaw ${OC_PROFILE_FLAG[*]:-}'"

oc cron add --name potter-qc-recheck --declaration-key potter-qc-recheck --cron "5 * * * *" \
   --tz America/New_York --exact --no-deliver --timeout-seconds 300 \
   --command "python3 $POTTER_HOME/tools/qc/verifier.py --mode recheck --state $WS/state/quality-control --workspace $WS --tz America/New_York --openclaw 'openclaw ${OC_PROFILE_FLAG[*]:-}'"
```

- `--sessions` may be given more than once (one per agent whose claims should be audited: potter, and the home
  agent if wanted). v2 read the main agent and the local-tier agent. **I do not know the session directory of the
  v3 potter agent**; take it from `oc config` or the 2026.11 docs.
- Optional recorder flag `--model-cmd "<command>"`: the command gets the prompt as its last argument and prints
  the reply (plain text or OpenClaw's `--json` payload form). v2 used
  `openclaw agent --local --model <flash model> --session-key agent:main:qc-recorder --json -m`. Without it the
  recorder uses its built-in rule extractor and still exits 0. The model command sees transcript text, so choose
  the model with that in mind. No key is stored anywhere by these tools.
- Optional verifier flags: `--library-cmd "<read-only command that prints the reference library>"` (v2 ran the
  Notion CLI against one hard-coded data source id; v3 takes any command, or none) and `--wake-cmd "<command>"`
  (gets a fix-it prompt as its last argument; v2 woke the main session with an agent turn aimed at a hard-coded
  chat id).
- Exit codes: 0 when the checks ran, whatever they found; 1 only when the report could not be delivered (so the
  schedule watchdog reports the QC job itself).

Trading (standby):

```
oc cron add --name potter-daily-trading --declaration-key potter-daily-trading --cron "0 17 * * 1-5" \
   --tz America/New_York --exact --agent potter --session isolated --no-deliver --timeout-seconds 900 --disabled \
   --message "Run the trading-run skill. Gate command: python3 $POTTER_HOME/tools/trading_precheck.py with --mandate $WS/trading/MANDATE.md --kill-switch $WS/trading/STOP --log $WS/trading/logs/trading-run.jsonl --tz America/New_York. If the gate refuses, send the owner the one line the skill specifies and stop."
```

- The job must name its model (v2: `deepseek/deepseek-v4-flash`; v3: whatever the model policy file assigns to
  money-moving tool use) and its tools. v2 allowed `*`. v3 should allow only: read, write, exec, the message
  tool, and from the brokerage connection `get_accounts`, `get_portfolio`, `get_equity_positions`,
  `get_equity_orders`, `get_equity_quotes`, `review_equity_order`, `place_equity_order`, `cancel_equity_order`.
  No option, watchlist or scan tools. I did not find the 2026.11 flag names for per-job model and tool lists in
  this repo; read the cron docs before adding them.
- Handover registry: add `potter-daily-trading` under `crons` with `enabled: false` and purpose "daily trading
  run under the owner's mandate". The phrase the owner uses is "take over trading"; the `trading-run` skill has
  the checks that come before `potter-cron on potter-daily-trading`.

## 3. Where v2's trading code would live in v3, and its dependencies

| v2 (under `workspace/`) | v3 |
|---|---|
| `main/trading/crypto-alpha-bot/` (`live_check.py`, `alpha_framework.py`, `data_fetcher.py`, `CRON_INSTRUCTIONS.md`, `state/`) | `$WS/trading/loop/` |
| `trading/llm-monthly-scoring/` | `$WS/trading/monthly-scoring/` |
| mandate documents in `trading/` | `$WS/trading/MANDATE.md` (new format, section 5); old documents kept beside it as history |

- **Not copied by this step.** The code is the owner's working strategy; it should be copied byte for byte by the
  importer when the owner takes over, not rewritten.
- **Dependencies** (v2 `requirements.txt`, generated at export, unpinned): `numpy`, `pandas`, `pykalman`,
  `yfinance`. v2 ran them from a `venv/` inside the code directory, which the export leaves out. v3: the installer
  creates `$POTTER_HOME/venvs/trading` (`python3 -m venv`, `pip install -r requirements.txt`) at handover time,
  not at base install, and the strategy instructions call that interpreter. Pin the versions v2 actually has
  (`pip freeze` on the old host) before installing; the export does not record them.
- `live_check.py` fetches prices from a public market-data site (through `yfinance`), so the job needs outbound
  network to it. The monthly-scoring folder has its own small HTTP client for the brokerage connection that
  reads a token from disk; do not carry that client: v3 uses the gateway's connection.
- Things to change when the code is copied (owner's decision, not done here): `live_check.py` and both
  instruction files contain the literal account number and a chat id; replace with the mandate's account label
  and delivery settings. `data/*.pkl` are pickles: do not load them from the export, let the code refetch.
  `CRON_INSTRUCTIONS.md` must be edited to start with "run the trading-run skill's gate" and to send orders only
  through the gate.

## 4. Brokerage connection

- One MCP server, name `robinhood-trading` in v2 (`inventory/mcp.json`): streamable-http, `auth: oauth`. No key in
  any file; the gateway holds the OAuth grant.
- v3: add it to the gateway's `mcp.servers` through a validated config patch like the Notion entry, then the
  owner completes the OAuth sign-in once in a browser. The grant cannot be exported from v2.
- Bundle the connection into the trading job only. No other job, and not the main chat session, should have the
  order tools.

## 5. Config and secrets (names only)

| Name | Used by | Secret? |
|---|---|---|
| `OWNER_TELEGRAM_ID` | verifier `--to`, trading job delivery | no (already in the installer) |
| `OC_PROFILE_FLAG` | `--openclaw` argument | no |
| potter agent session directory | recorder `--sessions` | no |
| QC model command (optional) | recorder `--model-cmd`; the model's key stays in OpenClaw's own secret store | the key is; the command is not |
| reference-library read command (optional) | verifier `--library-cmd`; Notion access through the existing connection | no new secret |
| brokerage OAuth grant | gateway MCP server | yes, held by the gateway, created by the owner's sign-in |
| `$WS/trading/MANDATE.md` | trading gate | not a secret, but owner-only to edit |
| `$WS/trading/STOP` | trading gate (kill switch; exists = stop) | no |
| market holiday file (optional) | trading gate `--holidays` | no |

Mandate fields (designed from v2's two mandate documents: status line, window, capital, instruments, order and
campaign limits, records, approval gate): `status`, `approved_by`, `approved_on`, `window_start`, `window_end`,
`account` (label only), `capital_limit`, `max_order_value`, `max_orders_per_run`, `instruments`, `universe`,
`order_approval` (`per_order` | `autonomous`), `strategy`. No amount from the real documents is in any file.

## 6. Ported, changed, not ported

**QC, kept from v2:** two separate steps (recorder lists, verifier checks) plus an hourly recheck; transcript
parsing; claim records and categories; the extraction prompt (personal examples removed); keyword evidence
checks against the scheduler, workspace files and recent commits; follow-up files; the 24-recheck cap; the owner
hears only about failures.

**QC, changed on purpose:**
- Paths, chat id, model and library id are arguments; nothing is hard-coded and nothing is read from a home
  directory.
- Silence is no output at all. v2 printed a `NO_REPLY` marker for the scheduler to swallow.
- The model is optional. A built-in rule extractor (first-person done-sentences that name a path or a quoted
  name) always works; a failing model falls back to it per file.
- New "Schedules" section: `schedule_watch.evaluate()` is imported, not copied, and run over every enabled job;
  the watchdog job itself, which `evaluate()` skips, is also checked here.
- `sent` and `verified` claims have no deterministic check and stay silent. v2 checked `sent` against the cron
  list and `fixed`/`verified` against the Notion library, which reported unrelated work as not done.
- The recheck no longer enables and disables its own cron job. It runs hourly and returns at once when nothing
  is open. v2's version depended on a config file of cron ids.
- Follow-ups are closed when a recheck finds the evidence. v2 never closed one.
- The verifier does not wake an agent by default (`--wake-cmd` is optional).
- A named path must be inside the workspace; links are not evidence.

**Two v2 defects found while reading (from the code; the export has no run history to confirm):**
1. Recorder and verifier computed the audited day differently. With `--hours 26` at 00:30 the recorder filed
   claims under the date 26 hours earlier (two days back), and the 01:00 verifier read yesterday's file. On that
   reading the nightly pass normally found no claims file and stayed silent. v3 uses one shared function.
2. The hourly recheck only looked at items the nightly pass had already marked open, so with defect 1 it had
   nothing to do.
If this reading is right, v2's QC has not been reporting, which fits the design document's "no known problems".

**Finance, not ported (deliberately):** every line of trading logic: signal pipeline, factor framework, data
fetcher, backtests, grid search, monthly scoring prompt, universe, portfolio state, the two cron instruction
files, and v2's run logs and pending-trade state. The finance and crypto-tracking rule files were not rewritten
either: they say "no trading" while the daily job trades, and that conflict is the owner's to settle (section 7).

**Finance, new:** the gate (`trading_precheck.py`), the run-log format, the mandate format and blank form, the
skill.

## 7. What the main session or the owner must decide

1. **There is no mandate for the loop that is actually trading.** v2's only mandate documents are a one-month
   options experiment whose window ended in August and a monthly-scoring mandate; both say every order needs the
   owner's explicit approval. The live daily job trades factor funds without per-order approval on the strength
   of its cron message and instruction file. In v3 the gate refuses until the owner writes and approves
   `trading/MANDATE.md` for that loop (universe, limits, end date, `order_approval`). That is intended: v3 will
   not trade on an instruction file alone. Someone has to tell the owner before "take over trading".
2. **v2's rules say "no trading"** (finance and crypto-tracking) apart from the expired exception. v3 needs one
   finance rule that points at the mandate. Not written here because the owner's intent is not on record.
3. **Gate enforcement is in the skill, not in a hook.** The agent is told to call the gate; nothing blocks an
   order tool call that skips it. Design rule 2 says guardrails are hooks: the guardrails plugin should refuse
   the brokerage order tools unless the run log shows a `run_start` and a matching `order_intent` from today.
   Outside this step's files.
4. **Timeout for the trading job.** v2 set none. 900 s is a guess.
5. **Delivery for the trading job.** v2 used `delivery_mode: none`. If the "run refused" line must reach the
   owner, sending it should be a must-deliver path, not the agent's choice.
6. **Monthly scoring jobs:** install as standby, or leave out until asked (recommended).
7. **QC model:** rules only (free, misses paraphrased claims) or a model command (v2 behaviour).
8. **Keyword matching strength.** One distinctive word is enough for "found" (v2's rule; quiet, but it can pass a
   claim on a coincidence). `--min-keyword-hits 2` is stricter for scheduler evidence.

## 8. Not verified

- Nothing was run against a live gateway. `cron list --json` field names are taken from `schedule_watch.py`.
- The v3 session transcript location and line format. The parser accepts v2's format (`type: message`,
  `message.role`, `message.content` text blocks, numeric or ISO timestamps) and keeps messages whose timestamp
  it cannot read.
- That `openclaw agent ... -m <prompt>` still takes the prompt as a final argument in 2026.11.
- How a 2026.11 isolated job turn sends a Telegram message to the owner, and the flag names for a job's model
  and tool list.
- The brokerage connection's tool names in v3 (taken from v2's job tool lists) and whether its OAuth sign-in
  works from the v3 host.
- Market holidays: the gate checks weekdays, plus a holiday file if one is supplied. None is shipped. v2 checked
  neither.
- The gate cannot see the brokerage. Capital and per-order limits are checked against the orders the run
  proposes, not against the account's real holdings; the skill's read-only account check covers that part and is
  prose.
- `install/import_v2.py` reads a schedule's `cron` key and no timeout, while this export's `inventory/cron.json`
  uses `expr` and `timeoutSeconds`. I did not run it; if it is still used on this export it would skip these jobs.
