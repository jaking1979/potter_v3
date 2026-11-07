# responsibilities (package E2, decision T16)

A responsibility is a standing duty, such as a morning brief, bill watch or follow-up. This package runs them on top of C1 (`policy/`), C2 (`runtime/` tasks and effect journal) and E1 (`notifier/`). It is staging-only (T22): connectors are injected, and the only ones in this repository are offline test fakes.

```
trigger ─(C2 trigger key)─▶ task ─claim─▶ reconcile gate ─▶ step: C1 decide ─▶ C2 record_intent/execute ─▶ connector
                                                               │ deny(needs approval)
                                                               └▶ E1 ApprovalIssuer ─▶ waiting_approval ─/approve─▶ queued
results · long-work state · escalations · watchdog ─▶ E1 JobHandle (enqueue-only) ─▶ C3 outbox
```

| Module | What it does |
| --- | --- |
| `definitions.py` | `Responsibility(id, trigger, delivery, steps, cadence_s, …)`. The trigger is `Schedule(every_s)` or `Event(topic, key fields)`. Delivery is `"josh"` or `"silent"`. Each `Step` names a C1 capability, its bound action (static, or a deterministic builder over the payload and earlier results) and a connector identity. |
| `runner.py` | `ResponsibilityRunner`: `tick`, `on_event`, `run_once` / `run_pending`, `poll_approvals`, `live_action` (for E1's `CommandRouter`), `watchdog` and `recover`. |
| `_schema.py` | `responsibility_state` holds the schedule watermarks. `responsibility_progress` holds the write-ahead long-work ticks. The layout is pinned by `meta.responsibilities_schema=e2.v0`. |

## How each requirement is met

1. **Dedupe.** Trigger keys are `resp:<id>:slot:<n>` for schedules and `resp:<id>:event:<subject fields>` for events. They go into C2's unique trigger key, so a duplicate trigger, even from another process, returns the existing task. Event keys come from subject fields, never from arrival order, so a reordered or late copy also maps to the same task (the first payload is kept). A schedule tick for a slot older than the watermark is dropped.
2. **Steps go through C1, then C2.**
   - `allow`: `record_intent(task, step, action)`, then `execute(..., connector=)`.
   - A deny whose only reason is a missing approval (`approval_required` / `untrusted_context_requires_approval`): one request through E1's `ApprovalIssuer`. If a crash happens before the checkpoint, the same pending request is reused rather than prompting Josh twice. The task then moves to `waiting_approval`.
   - Any other deny fails the run.
   - Event triggers default to untrusted provenance (`external_event`), so any write they cause needs an approval. Reads do not.
3. **Never sends.** The runner holds only `JobHandle`s. Each responsibility gets one bound to its declared delivery. A `potter.ops` handle (always `josh`) carries escalations and watchdog alerts, so a silent job's failures still reach Josh. `notifier.lint` passes over this package.
4. **Kill flag (E1).** It is checked before a claim, before every step and right before every dispatch, and `tick` does nothing while it is set. A call already in flight completes and C2 records it. Nothing after it runs. The checkpoint `{"next": i, "interrupted": true}` records where to resume. Once the operator clears the flag, the run resumes at step `i`, and completed steps are skipped from the journal.
5. **Recovery.** Leases are C2's: a dead worker's task is reclaimed when its lease expires. Before any step of a claimed task runs, each of its `unknown` effects is reconciled through `EffectJournal.reconcile`, which never dispatches. If an effect stays unknown or in flight, the task is **blocked**: a retryable failure, which C2 escalates after 3. Re-dispatch happens only where C2 allows it (a fence, or a trusted contract digest). An approved step whose nonce was consumed before a crash resumes through C1's `retry_intent` path (`reconcile` outcome) and then C2.
6. **Long work (T16 states → E1 `LongWork`).** States are `queued`, `running`, `waiting_approval`, `done`, `failed` and `escalated`. Each tick is persisted *before* E1 sees it, and the history is replayed into a fresh `LongWork`. E1's deterministic ids make the replay idempotent across restarts. The result is an ack after 30 s, an update only on a state change, and a final message with `potter://receipts/<task_id>`. Short successful runs send just the `job_result` (with the receipt) when the definition declares a `report`.
7. **Watchdog and escalation.** `watchdog()` alerts on C2 `stale_jobs` (cadence × tolerance), with one alert per bucket. It also re-enqueues every escalation idempotently, because C2's escalate callback is at-most-once after commit.

## Known gaps / decisions for review

- **Interrupt counts as a failure.** C2 has no pause or release transition, so an interrupted run is released with `fail(retryable=True)`. Three interrupts of the same task escalate it. Candidate C2 follow-up: `TaskStore.release(task, worker)`.
- **Approval cancelled by `/stop` → run fails.** E1 cancels pending approvals on `/stop`, and `poll_approvals` maps `cancelled` to a C2 denial (`failed`). Re-requesting after the flag is cleared would need a definition-level policy.
- **Direct reads of other packages' tables.** The runner reads, never writes, three things directly: E1's `approval_requests` (E1 has no API that returns the issued record, or the pending request for a task and action hash), and C2's `tasks` for `waiting_approval` and `escalated` ids and `jobs` for `escalated_at` (C2 has no list-by-state API). Candidate follow-ups: `ApprovalIssuer.record(id)`, `TaskStore.list(state=)`.
- **Claim has no kind filter.** The runner assumes it owns the task store's queue. A task whose kind is not one of its definitions is failed closed, not skipped.
- **`LONG_WORK_ACK_S`** is mirrored (30 s) because lint forbids importing `notifier.service` outside E1.
- **No real scheduler daemon or connectors.** `tick`, `run_pending`, `poll_approvals` and `watchdog` are called by the future C8 loop.

Evidence: `tests/test_responsibilities.py`, with fixtures in `tests/resp_world.py`. The restart tests SIGKILL a spawned worker mid-dispatch.
