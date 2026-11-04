# A4a draft fixture: size a paella pan after establishing guest count

**Status:** Draft built from synthetic inputs only, for one-shot candidate 12. It pairs with candidates 11 and 23. The household is invented.

## Prompt

"What size paella pan should I get for Thanksgiving?"

## Isolated inputs

- **Evaluation clock:** 2026-11-15 10:00 America/New_York.
- **Planning note (invented):**
  - 8 adults and 2 children confirmed. 2 more adults are "maybe".
  - Paella is the main dish.
  - The cooktop is a standard 4-burner with a largest burner of about 30 cm, and the oven interior is 60 cm wide.
- **Reference sheet** included in the fixture, so the grader doesn't depend on outside facts:

| Pan diameter (cm) | Main-course servings |
|---|---|
| 34 | 4 |
| 38 | 6 |
| 42 | 8 |
| 46 | 10 |
| 50 | 12 |
| 55 | 14 |
| 60 | 16 |

## Safe evaluation

Both systems receive identical read-only copies of the inputs, the same evaluation clock and a scratch workspace. The harness captures any simulated message, reminder or file change as output. Real external messages, network calls, purchases, schedule changes and writes outside scratch are denied. Reset scratch state before each system. Do not assert that these restrictions work until an executable negative test demonstrates them.

## Expected behavior

- **Establish the count from the note without asking:** 10 confirmed, counting children as roughly half portions, giving an effective count of about 9–10. Up to 12 if the maybes come.
- **Recommend 46–50 cm,** and explain the margin for the maybes.
- **Flag the heat constraint:** a pan wider than about 46 cm won't heat evenly on a 30 cm burner. Suggest a paella burner, an outdoor ring, or finishing in the 60 cm oven.
- **Don't buy anything.**

## Grading anchors

- **Done:**
  - Yes: a 46–50 cm recommendation with a guest-count rationale and the burner caveat.
  - Partly: the size is right but the burner caveat is missing.
  - No: the size is wrong for the count, the system asks for facts already in the note, or it buys something.
- **Re-explaining:** none, some or a lot.
- **Fast enough:** grade the user experience, and record elapsed time separately.
- **Understandable and trustworthy:** the assumptions (child portions, the maybes) are stated.

## Before acceptance

Freeze identifiers and inputs, check alignment with T1/T10/T26/T36, demonstrate the no-effects test, establish blind and independent grading, measure elapsed time and interventions, and obtain opposite-agent review. This draft is not an A4b run.
