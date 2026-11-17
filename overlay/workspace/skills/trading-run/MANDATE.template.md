---
# Trading mandate. The scheduled run reads this block first, every run, and refuses to act unless every line
# is filled in and it is in force today. Only the owner changes this file. Copy it to trading/MANDATE.md.
status: draft
# ^ draft | approved | suspended | ended. Only "approved" allows a run.
approved_by:
approved_on:
# ^ YYYY-MM-DD, the day the owner approved this exact text.
window_start:
window_end:
# ^ YYYY-MM-DD. After window_end the run refuses; renewing means the owner edits and re-approves.
account:
# ^ a label for the one account this covers (for example its nickname). Never an account number.
capital_limit:
# ^ plain number: the most this mandate may put at risk in total.
max_order_value:
# ^ plain number: the most one buy order may be.
max_orders_per_run:
instruments:
# ^ comma list from: equity, etf, option, crypto
universe:
# ^ comma list of the only ticker symbols that may ever be traded
order_approval: per_order
# ^ per_order = the owner approves each exact order first. autonomous = orders inside these limits need no
#   further approval.
strategy:
# ^ path (inside the workspace) of the instructions that decide what to trade. They work inside this mandate.
---
# Mandate

Written by the owner, in the owner's words: objective, what is never allowed (margin, borrowing, short sales,
transfers, withdrawals, after-hours orders, anything not listed above), when to stop for operational trouble
(authentication failure, cash / position / order mismatch, stale quotes, unknown order state), who is told
and how fast, and when the status update is due.

Where the prose and the block above disagree, the stricter one wins.
