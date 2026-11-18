---
name: week-review
description: The Sunday week in review (chief of staff): how the week went against the forecasts, what kept slipping, the patterns learned, and the one change for next week. Scheduled Sunday evening; also on request ("week review", "how did my week go").
---
# Week review

1. Run `potter-cos-score --week`. It prints the week in numbers as JSON: days with a forecast, days with an evening
   record, forecasts that came true (`hit`), did not (`miss`), unknown, `accuracy`, and the items that `slipped`.
2. Read your patterns: they arrive in your working context as "has a pattern: ..." (ask the graph with `kg_search`
   for `pattern` entities if they did not).
3. Reply, at most 12 lines, no preamble:
   - **The week:** one sentence judgment (from the numbers and the evening records, not invented).
   - **Kept slipping:** up to 3 items with how many times, and for each one concrete fix (do it first, shrink it,
     delegate it, or drop it). Ask which; never decide for the owner.
   - **Patterns:** new or changed patterns with their evidence ("Thursdays ran packed 4 of 4").
   - **How good my forecasts were:** accuracy and what to change in them, in one line.
   - **Next week, one change:** the single highest-leverage adjustment.
4. If fewer than 3 days had an evening record, say so in one line and keep the rest short: there is not enough
   evidence yet, and invented patterns are worse than none.
