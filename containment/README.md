# containment — A1 / T32 / T31 PASS 2 row 1 / T5, offline package

This package implements the **ratified** Alpha Bot build spec: T31 PASS 2 row 1, agreed in T34, which carries out T32 without changing it. It also implements the T5(a)/(b) watchdog. It is ordinary code under review and is **not wired to the live account or the host**. It holds no mandate values and no credentials, and it makes no network calls. Josh has directed that Alpha Bot keep trading; nothing here pauses or reconfigures it.

| Module | Row 1 item | What it does |
|---|---|---|
| `alpha_guard.py` | 1, 2, 3, 5 (plus Dolores 0b98bbb review) | The only caller of `place_equity_order`. It refuses unless all of the conditions below hold. Only the Alpha Bot cron may call it. Market orders are valued at the fresh quote plus a 2% developer safety buffer. An unreadable kill flag or broker-schema drift at startup pauses the guard and raises an alert. |
| `decision_journal.py` | 3 + `ref_id` addendum | The decision ID is the New York date, ticker, side and signal version. It maps to a UUIDv5 `ref_id` that is reused on every retry. Each order is journaled as `pending` with fsync **before** the call; an error marks it `unknown`, and it is never resubmitted blind. Entries the broker hasn't reported yet are counted as exposure, which also survives a restart. |
| `kill_switch.py` | 1, 7 | Josh's command path. It is the only holder of `cancel_equity_order`. It takes the guard's lock, so any in-flight order finishes first. It writes the flag `on` atomically, then cancels **only the bot's own** open orders (ref_ids in the journal), attempting each one independently. |
| `mandate.py` | 2 | A strict parser for the approved mandate file. The guard checks the file's hash before parsing it. |
| `watchdog.py` | T5(a)/(b) | Flags jobs stale beyond 2× their cadence. HA status is `reachable`, `degraded`, `unreachable` or `unknown`; no observations, a check that couldn't run, or stopped coverage all count as `unknown`, never healthy. The health line is always emitted and reads OK only when every source is confirmed. |

## Conditions the guard requires before any order (row 1 item 2)
- The kill flag is readable and reads `off`.
- The T35 fencing epoch is current.
- The mandate file's hash matches the approved version.
- The ticker is on the mandate's list, and the order is an equity market order.
- Positions and buying power were read within the last 60 seconds.
- After the trade, exposure is within both the ceiling and the position limit, including orders not yet filled.
- Account value is above the loss stop.
- Buying power covers the worst-case fill.

## Release tests (row 1 item 7): `tests/test_alpha_guard.py::test_rt_*`
- An order from the main agent is refused.
- An off-list ticker is refused.
- A stale epoch is refused.
- A timeout after the order filled remotely is not duplicated.
- A retried order carries the same `ref_id`.
- A kill mid-run waits for the in-flight order and then cancels it, deterministically, using events.
- An unreadable flag starts the guard paused.
- Schema drift pauses the guard.

Further tests cover the unapproved mandate, state older than 60 seconds or from the future, the ceiling including the price buffer, buying power, a loss stop that blocks buys and sells, sells net of open sells, reservations surviving a restart, audit failure meaning no order, malformed inputs, a kill switch that cancels only the bot's orders, and a kill switch that tries every cancel.

**Mutation checks (done locally):** each of these makes at least one test fail when removed:
- the lock on the kill switch
- counting orders the broker hasn't reported yet
- the block on blind retries
- the mandate-hash check
- the kill switch's filter to the bot's own orders

## Review fixes (Dolores, on 0b98bbb)
- **Own quotes:** every held position and open buy must have its **own** fresh, positive and finite quote. There is no fallback to another ticker's price, and invalid amounts refuse.
- **The 2% buffer is a margin, not a hard bound:** an overrun after a fill (holdings above the ceiling or the position limit) refuses all buys, raises an alert, and still allows sells. Whether the ratified market-order contract needs a hard dollar bound before each trade is a Rule 12 question for the Council.
- **Cross-process lock:** check, reservation, broker call and journal update run under a cross-process `flock` on `<journal>.lock`, shared with the kill switch. Tests cover two guard instances in one process and two guard processes; exactly one $204 buy goes through against the $250 ceiling.

## Not done here (host steps; none change Alpha Bot's behavior or credentials)
- Replace the cron's direct order tool with this guard.
- Wire the kill switch to Josh's Telegram command.
- Wire the T35 epoch source and the schema-hash capture.
- Run the same release tests on a disabled clone against a stub MCP server.
- Row 1 items 4 (sell first, then buy) and 6 (Telegram summary) are bot-side and not in the guard.
