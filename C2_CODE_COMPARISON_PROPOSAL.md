# C2 code-comparison correction proposal

Dolores proposal stacked on Claude C2 exact d8ea587; Claude's active branch is
unchanged. T10/T39; independent Claude review is required, no merge permission.

Reproduction on Python 3.12: `External.perform` loaded from the exact candidate
has an equal freshly compiled code object and identical exposed `co_*` fields,
but `marshal.dumps` produces different bytes. The candidate rejects a legitimate
adapter, matching current CI's source-verifier errors. Marshal serialization
contains reference/interning bookkeeping; its byte representation is not the
right equality check for separately loaded code objects.

This proposal uses Python code-object structural equality while retaining the
qualified-name and first-line checks. Existing source-file SHA binding and
registry checks stay in place. Regression checks source/cached imports and
in-memory constant mutation; existing same-name source replacement and restart
tests must continue passing. It does not authenticate dependencies, closure or
object configuration, provider namespace, mutable state or production custody.
Those prior findings remain unresolved. No live connectors or hosts are used.
