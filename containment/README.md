# containment — A1 / T32 step 2 / T5 offline package

This is the offline half of the A1 containment proposal, posted on the Build Execution Plan on 2026-11-04. It is ordinary code under review. **It changes nothing on the OptiPlex.** It contains no mandate values and no credentials, and it makes no network calls.

| Module | Ratified rule | What it does |
|---|---|---|
| `mandate.py` | T32 step 2 | Defines the mandate's shape (ceiling, instruments, max position, loss stop). It refuses anything malformed, including JSON floats, NaN, duplicates, unknown fields, and a max position above the ceiling. **The values themselves are Josh's decision.** |
| `order_guard.py` | T32 step 2: "a code check before every order" | A fail-closed pre-order check. It refuses unknown tools, options and exercises, out-of-mandate symbols, orders over the position or ceiling limits, buys past the loss stop, shorts, orders without a limit price, missing mandate or account state, and every new order once the kill switch is engaged. Cancels are always allowed. |
| `guarded_broker.py` | T32 step 2 kill switch; A1 box 2 | Puts the guard in the call path. It writes one audit line per decision and forwards only allowed calls. The kill switch is a file flag: engaging it refuses new orders and cancels open ones. `StubBroker` records calls and has no network access. |
| `watchdog.py` | T5(a), T5(b) | Flags jobs stale beyond 2× their cadence. A job that has never succeeded gets one grace window. Alerts after 2 missed HA checks. Produces a brief health line that is **always** emitted, so A1 box 4 can count it. |

## Negative tests in this package
- **Level 1** (`tests/test_containment_guard.py::GuardLevel1`): 20 refusal cases, an in-mandate positive control, and an exact ceiling boundary.
- **Level 3** (`GuardInPathLevel3`): refused calls never reach the stub, and every attempt is audited. The positive control does reach the stub. A corrupt or deleted mandate refuses at once, as does an exception in the account lookup. The kill switch cancels open orders and then blocks.
- **Mutation check, done locally:** disabling the instrument check makes 2 tests fail, and disabling the guard's refusal makes 4 fail.

## Not done here (needs host access and Josh's go — see the PLAN)
- **Confirm the real tool names.** The exact `cancel_*` names and the 5 named Alpha Bot tools have to be confirmed on the host.
- **Wire it into OpenClaw.** `GuardedBroker` has to become a `potter-guardrails` hook on the Robinhood MCP tools. The account-state provider and the cron-disable half of the kill switch are host-side.
- **Run negative-test level 3 on the host.** It runs against a disabled clone of the job, with the stub in place of the real MCP server. **It never runs against the live account.**
- **Set the mandate values.** These are Josh's decision.
