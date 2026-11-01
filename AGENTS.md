# Potter v3 agent implementation rules

1. Canonical product/architecture intent lives in the Potter v3 Notion Design Council and canonical Decision Log. Do not infer or revise product authority from code.
2. The Notion Build Execution Plan governs package order, dependencies, risk tier, ownership, review, and evidence.
3. Every change identifies its package ID and governing decision/test IDs.
4. Shared contracts land before dependent implementations.
5. Do not edit another agent's active branch.
6. T2 work requires opposite-agent independent adversarial review of the exact candidate commit before merge.
7. Until the T22 readiness token, v3 runtime is staging-only: no durable production writes and no real external effects.
8. Under L12, browser/computer are host-bound and default-disabled; no workstation executor or Messages-specific authority may land on main until T38 is ratified.
9. A package is not done because code exists. Required evidence must pass against the exact component versions.
10. If implementation conflicts with a ratified contract, stop and return the conflict to the Design Council rather than silently changing behavior.
