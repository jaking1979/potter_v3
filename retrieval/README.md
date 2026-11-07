# D4 offline retrieval process proposal

Package D4, governing T3 retrieval deadlines and T22 staging containment.
Build-ahead only; A5/D1/D2 dependencies, canonical T15/T27 filtering, measured
backend selection and independent Claude review remain outstanding. This proposal
does not complete D4, claim a production SLA or authorize consequential work.

`SyntheticProcessBackend` is an explicit closed synthetic option to `retrieve_turn`
and `benchmark`. A fresh spawned process sleeps or emits a synthetic bounded JSON
result through a private pipe for each request. There are no real adapters,
credentials, network calls, durable writes or configurable worker callables.
Production turn default/max is two seconds for this option; benchmarks default to
30 seconds and may score slower answers, returning IDs only. Existing async
backends retain advisory cancellation semantics and are not made process-safe.

At a deadline or caller cancellation, the child is terminated, allowed 50ms,
then killed if necessary and reaped (250ms maximum additional join). Cleanup runs
before return/cancellation propagation. Failure to reap raises a runtime error;
it never yields an accepted result. Process startup, decode and cleanup count in
reported elapsed time. Results finishing cleanup after the deadline are dropped.
Scheduling/startup/cleanup overhead means this is not proof of a hard wall-clock
two-second SLA. A process is lifecycle isolation, not an OS security sandbox;
do not put effectful or untrusted adapters here. Process start and bounded joins
can briefly occupy the parent thread. Real sandbox/process-tree containment,
cross-platform qualification and host resource quotas remain future work.

IPC is capped at 16KiB, three items, 256 characters per ID and 2048 per text;
only validated JSON evidence is accepted. No received pickle is deserialized.
Each request owns its process/pipe, preventing a timed-out worker/result from
being reused by a subsequent turn. Consequential results always remain blocked,
including successful retrieval, pending canonical/policy integration.

Validation: `python3 -m unittest discover -s tests -v` covers real child blocking,
SIGTERM resistance/kill, parent heartbeat, caller cancellation, worker exceptions,
malformed/oversized messages, result/request bounds, fresh-request independence,
separate benchmark deadlines and unconditional consequential blocking. Synthetic
short deadline tests do not establish production latency or benchmark quality.
