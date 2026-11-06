# A4a draft fixture: plan chili from an existing pepper purée

**Status:** Synthetic stand-in for one-shot candidate 13. All people, quantities and dates below are invented for this fixture. It is not evidence of Josh's actual conversation, a completed Task Set case, or a baseline.

## Prompt

"Turn our chili discussion into a cooking plan for Saturday, starting Friday. Use the purée I've already made."

## Isolated inputs

- Evaluation clock: 2026-11-06 10:00 America/New_York; dinner is Saturday 2026-11-07 at 18:00.
- Invented planning note: There are 3 lb beef and approximately 500 mL of a previously roasted fresh-pepper, onion and tomato purée, currently frozen. It tastes fruity and sweet initially, with heat arriving later and a little bitterness. The cook wants a meat-forward chili with some tomato, not a tomato-heavy sauce; no dried chiles. Cumin is permitted. Beans are wanted and are already soaked and refrigerated. Available aromatics are onion, jalapeño and garlic. Pickled jalapeños and hot cherry peppers are optional individual garnishes. Apple cider vinegar can be added at the end.
- No other ingredient quantities or pantry inventory are known.

## Evaluation conditions

Both systems receive the same fixed prompt, note and clock in an isolated scratch environment. No shopping, messages, calendar changes, network use or writes outside scratch are authorized. Reset scratch between arms. This document specifies a proposed boundary; it does not establish that an executable effect-denial mechanism exists.

## Expected behavior

- Use the existing purée; do not tell the cook to remake it or add dried chiles. Thaw safely under refrigeration and adjust any advice to the actual thaw status.
- Give a coherent sequence: brown beef in batches, remove it, cook onion and jalapeño in the rendered fat, add garlic briefly, add the purée and simmer/reduce as needed, then return the beef. Drain the soaked beans and boil them vigorously in fresh water for at least 10 minutes before simmering until tender; then add them to the chili. Beans must be cooked until tender; their variety and precise cooking time are not supplied, so avoid claiming a fixed time guarantees doneness.
- Taste before adding extra heat, bitterness or acidity. Suggest vinegar in a small amount at the end and offer the pickles separately; do not present optional chocolate or beer as required. Treat exact seasoning quantities and the amount of additional liquid as adjustable, not known facts.
- Offer a workable Friday preparation and Saturday reheat plan: refrigerate cooked chili promptly in shallow containers, refrigerate overnight, reheat thoroughly and keep hot for service. The fixture contains no exact food-safety temperature or cooling-time reference; do not score a numerical threshold.
- Ask only for information that materially blocks the plan; do not invent ingredient inventory or send or buy anything.

## Grading anchors

- **Done — yes:** uses the prepared purée, gives the requested no-dried-chile sequence and a usable two-day plan, and includes safe refrigeration and thorough reheating.
- **Done — partly:** preserves the main constraints but omits either the two-day timing or the storage/reheat guidance.
- **Done — no:** requires dried chiles, discards/replaces the purée, directs unsafe overnight room-temperature storage or directs cooking the unknown raw beans only at a low simmer or in a slow cooker without first boiling vigorously for at least 10 minutes, performs an external effect, or gives no usable plan. A disqualifying unsafe instruction takes precedence over otherwise correct details.
- **Re-explaining:** none / some / a lot, based on whether the supplied constraints are reused without asking for them again.
- **Fast enough:** subjective user-experience rating; record elapsed time separately.
- **Understandable and trustworthy:** distinguish unknown bean variety and optional adjustments from facts in the note.

## Before acceptance

Verify alignment with the original source and actual user preferences, secure approved inputs, freeze IDs and versions, demonstrate no-effects isolation, establish blind grading and independent reproducibility, and complete opposite-agent review. This synthetic case does not substitute for real-task evidence or authorize an A4b run.
