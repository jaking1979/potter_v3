---
name: board-meeting
description: Independent advice from three models of different makers on a decision, with a chair's synthesis ("/boardmeeting", "take this to the board", "ask the board"). Only when the owner asks; advisory only.
---
# Board meeting

Run only when the owner asks: `/boardmeeting`, `/boardmeeting <question>`, `/boardmeeting full`, "take this to the
board", "run a board meeting on this", "ask the board what it thinks". Never for clinical or EHR matters.

1. Gather only the part of this conversation the decision needs (the session history tool), as plain text.
2. Run, passing it on stdin (add `--full` for `/boardmeeting full`):
   `echo '<json>' | node skills/board-meeting/board.mjs run` with JSON `{"conversation": "<text>", "question": "<the
   owner's question, if given>"}`. It redacts secrets, refuses EHR or client material, prepares the packet with the
   chair, asks the three directors independently, and has the chair evaluate their anonymised answers.
3. By `status`:
   - `ok`: send `report` as it is.
   - `approval_required`: show the owner the redacted `packet` and ask "send this to the board?". On yes, run again
     with `{"approved_packet": <packet>}`.
   - `clarify`: ask the owner the one question that settles which decision is meant.
   - `blocked`: say only that it touched excluded material. `failed`: say which part failed (`reason`, `participation`).
4. Advisory only: never send, schedule, buy, trade, publish or change anything because of the report. A later,
   explicit request is needed for action. Which model was which director is in `state/board-runs/<run_id>.jsonl`;
   reveal it only if the owner asks.
