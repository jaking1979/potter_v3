# Shadow-MVP runtime pieces

Offline, network-free building blocks for the Nov 27–29 shadow install. All
transports are injected callables; nothing here imports a network client.

- `gate.py`: `ToolGate`. Tools register with one of three effect classes
  (`read`, `reply_owner`, `model`); anything else cannot register. Calls are
  refused for unknown tools, before `seal()`, for a reply to any chat but the
  owner's, and when the audit sink is down. Audit records carry no arguments.
- `models.py`: `BrokeredModel`. Reserve, dispatch, call, settle through D1's
  `Broker` with the ratified ceilings (`RATIFIED_CAPS`). Unknown cost stays
  reserved at the worst case. An overrun halts all further calls.
- `costlog.py`: append-only JSONL cost record per call, no prompt or reply text.
- `turn.py`: `ShadowRuntime.handle`. Owner check, policy seam (C1 plugs in),
  Context Governor seam, model call, reply. `/why` shows the last turn's
  context labels and cost without a model call.

`model_broker/` is Dolores's D1 at `dolores/d1-file-portability-2228`
(63fca7b), with two `vars()` calls replaced so it passes `notifier.lint`.

Limits: the gate enforces declarations, not behaviour; a callable registered
as `read` must be checked at wiring and by Audit 2. A refused or failed model
tool call after dispatch leaves its reservation counted (the broker has no
cancel). Dates and task IDs are caller-supplied. PolicyEngine, the real
Context Governor, Telegram ingress auth (E1/E3) and all real transports are
not wired here.

Tests: `python3 -m unittest tests.test_shadow` (27).
