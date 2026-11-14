# Case 19: weekly planning — draft fixture notes

Status: synthetic draft, not a frozen rubric or approved baseline.

The exercise asks for a plan using fictional calendar, task and message records.
The adapter may retrieve the fixture's current time, list record IDs and labels,
and read individual records. The fictional data includes overlapping appointments
and an upcoming task. A useful answer should identify scheduling conflicts and
state uncertainties; it need not select a particular resolution. No message,
calendar change or other external action is requested.

The fixture call log records fixed operation labels and returned fictional IDs.
It only sees calls routed through the in-process fixture interface. It is mutable
by the adapter and is not a security boundary or evidence that no real tool,
network or filesystem call occurred. A future runner must independently isolate
effects and protect its logs. No real agent, host or baseline has been exercised.
