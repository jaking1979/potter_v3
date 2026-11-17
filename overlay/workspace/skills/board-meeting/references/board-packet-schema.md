# Board packet

The chair returns JSON only:

```json
{"board_question":"","decision_type":"","background":"","known_facts":[],"assumptions_or_unverified_claims":[],"owner_goals":[],"constraints":[],"options_already_discussed":[],"tentative_position":"","important_uncertainties":[],"requested_output":""}
```

Preserve substance; separate facts from assumptions; label tentative preferences; remove repetition; add no facts. Never state an inferred goal, constraint, or preference as confirmed. If no explicit question exists, infer one central unresolved decision. For multiple material decisions, return `clarification_required` and neutrally name them.