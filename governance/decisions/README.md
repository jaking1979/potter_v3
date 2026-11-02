# Decision index (package B2)

Notion's Design Council is the canonical source of ratified wording. This folder
holds exact snapshots so code can cite a decision by ID and CI can detect drift.

- `manifest.json`: decision ID, status, source, wording file, SHA-256 of canonical text.
- `wording/<ID>.md`: wording plus a numbered `## Acceptance tests` list.
- Release-test IDs are `RT-<ID>-<n>`, derived from that list (`tools/decision_index.py rt-ids`).

**Gates:** CI (`.github/workflows/decision-index.yml`) runs the unit tests and `python3 tools/decision_index.py check` on every PR and on pushes to `main`. B3 makes it a required check, via the T39 receipt gate. Any snapshot edit without a
matching manifest update fails. A snapshot changes only when a sync pass, run by
an agent with Notion read access, finds that `compare <ID> <export>` reports drift
after a ratified revision. That change is itself a reviewed PR.

Seeded with T38 and T39. T1–T37 are added by the first sync pass.
