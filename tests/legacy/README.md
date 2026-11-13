# Legacy E2 code (test fixtures only)

Verbatim copies of `responsibilities/*.py` from earlier E2 heads. They are used
only to produce real databases from the code that actually ran then, so that
the upgrade tests exercise an actual prior layout rather than a hand-written
imitation. They are never imported by runtime code. Each file's Git blob hash
equals the original's (`git rev-parse <commit>:responsibilities/<file>`).

| Directory | Commit | Schema |
| --- | --- | --- |
| `e2_467b2b9/` | `467b2b9` (last head before approval claims) | `e2.v0` |
| `e2_e6a7af6/` | `e6a7af6` (claims, empty v0 migration) | `e2.v1` |
