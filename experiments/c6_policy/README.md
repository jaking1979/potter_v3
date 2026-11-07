# C6/C1 synthetic local integration proposal

Package C6 (Dolores), T10/T11/T12/T22 and C6 task-linked merge evidence.
Stacked on the separately unreviewed local sink candidate `abdde433c1163d311645ed25110c6b2852bcbc69`.
No Claude-owned branch is changed. C0/C1/C2/T39 dependencies remain unmet for merge.

`tracing.policy_binding.SyntheticPolicyAudit` is a synthetic/replay-only binding
between C1's hook, the bounded C6 adapter and a `(event_id, JSONL)` sink.
The caller binds task ID, trace ID and timestamp independently of the emitted
event. ContextVar separates concurrent callers and restores nested contexts.
Missing or mismatched context refuses before writing. Stable per-decision event
UUIDs make an identical hook retry idempotent in the local SQLite experiment;
changed bytes for an existing event remain a conflict. The caller must preserve
the exact context and record across an uncertain retry.

This is not context authentication: a caller can bind false UUIDs. Each request
must create its own binding; async children inherit their parent's context.
There is no worker-loop integration, tracing of C2/C3, runtime activation,
credential access, host access, platform signing or external service call.

## Repeatable source-bound experiment

Stage the nine source files listed in `sources.json` from the recorded exact
commits, plus `tracing/policy_binding.py` from this candidate, into a disposable
scratch directory. Do not place credentials or user data there. Then run:

```sh
python3 -I experiments/c6_policy/run.py --source-root /absolute/scratch/components
```

The runner reads at most 128 KiB per expected file, requires each SHA-256 match,
copies the verified bytes into a new temporary import tree, and executes tests.
It performs no network lookup, dependency installation or artifact upload.
These content pins record what was observed through GitHub; they are not signed
source provenance, trusted manifest custody, a receipt or OS isolation. Trusted
paths, a fresh interpreter, and a trusted Python installation are assumptions.

Eleven local integration tests passed: 32 concurrent real C1 decisions reach
the correct local task/decision rows and survive reopen; linked denials;
uncorrelated malformed denial; mismatched/missing context; lost store and sink
failure; stable retry identity and nested restoration; synthetic-only binding.
Only synthetic temporary files are written. No production SLA is measured.

## Observed audit completion limit

One regression deliberately commits an `allow` trace then raises from the hook.
Current C1 correctly returns `deny/trace_error`, but the committed trace still
contains the preliminary `allow`. Therefore the record is a candidate policy
evaluation, not final execution authority or complete returned-decision evidence.
A failed sink can also leave no trace. C1/C6/C8 need an explicit independently
reviewed audit-completion/failure-reporting contract before claiming every final
decision is recorded. No effect is invoked by these tests and no decision record
is used to authorize an effect. Effect/audit atomicity, canonical task authority,
restore/retention, privacy access controls and full C6 acceptance remain open.

Migration is additive offline code only; rollback removes this proposal and
leaves the unmodified C1/C6 base branches. Claude's opposite-family review of
the exact candidate is required; no same-family test counts as acceptance.
