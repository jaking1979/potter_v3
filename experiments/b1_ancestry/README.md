# B1 / T39 offline ancestry inventory

Package B1; T39 Rule 12 reconciliation evidence. No receipt, merge exception,
signing key, consumer permission or incident closure is created here.

`observed-main.json` is a sanitized GitHub REST observation of tip
`911cb998672fe171fa268ec1d657d86558367df6`: **71 reachable commit objects**, not
71 arbitrary Git objects; five commits lie on its first-parent line. Every
parent is present. The observation contains full commit/tree/parent SHAs and
excludes author identities and commit messages. It is neither a signed
bootstrap manifest nor a substitute for reviewing any of the exact trees.

`ancestry.py` checks internal parent closure, no unreachable extras, exact
full SHA shape, cycles and deterministic ordering. It reports added/missing
commits and changed tree/parent bindings between observations. It does not
authenticate the source, rehash Git object bytes, read a repository, reject
object replacement, establish freshness, verify T39 signatures, or enforce
real consumers. A compromised source can provide a self-consistent fake graph.

`compare()` accepts both the shipped `ancestry-observation/v0` document
(`observed_tip`) and normalized `ancestry-inventory/v0` documents (`tip`). It
rejects unknown schemas, missing fields and ambiguous documents carrying both
tip fields, and recomputes graph information rather than trusting summary data.

Run from this directory:

```sh
python3 -m unittest discover -s tests -v
python3 ancestry.py observed-main.json
```

All execution is synthetic/local and has no network or host access. Existing
T22/T39, exact-head Claude review, merge hold and acceptance gates remain.
This is evidence preparation on a branch; nothing is approved for consumption.
