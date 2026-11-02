# Decision index (package B2)

Notion's Design Council is the canonical source of ratified wording. This folder
holds verbatim snapshots so code can cite a decision by ID and CI can detect drift.

- `manifest.json`: decision ID, status, source section, wording file, SHA-256 of canonical text.
- `wording/<ID>.md`: the verbatim `Decision & Reconciliation Record — <ID>` section, produced only by
  `python3 tools/decision_index.py extract <ID> <notion-export>`. Snapshots are never hand-copied.
- `expected.json`: every ratified decision B2 must index. `coverage` lists the gaps.
- Release-test IDs: `RT-<ID>-<n>` for numbered or inline `(n)` acceptance lists, and `RT-T37-S<n>` for the T37 appendix.

**Gates:**

1. CI (`.github/workflows/decision-index.yml`) runs the unit tests and `check` on every PR and on pushes to `main`. Any snapshot edit without its hash fails.
2. A coordinated edit of wording *and* hash passes `check`, so local hashes alone can't prove fidelity. `compare <ID> <fresh Notion export>` catches it. That check runs in an agent session with Notion read access, before every attestation on a PR that touches `governance/decisions/`. The manifest hash is bound into each T39 attestation, so a reviewer signs exactly what they compared.
3. CI also runs `coverage`, so the job is **red until every ratified decision is indexed**. That is intentional: B2 cannot be reported green or complete while T1–T36 are missing.
4. **Live-source verification is not enforced by B2 alone.** CI has no Notion access, and its tests use synthetic exports built from the snapshots, so a green CI here proves only internal consistency. The enforceable, fail-closed check that the snapshots equal the live ratified Notion text belongs at the B3/B5/T39 integration. Each reviewer attestation over a PR touching `governance/` carries the hash of a fresh `compare` run against the canonical export, and the receipt gate refuses without it. That integration is tracked on the Build Plan, and B2 must not be called complete before it exists.
5. B3 makes CI a required check, via the T39 receipt gate.

**Status:** T37–T39 are indexed. T1–T36 are not yet in the structured record format on the board; `coverage` exits 3 until they are indexed. B2 is not complete until it exits 0.
