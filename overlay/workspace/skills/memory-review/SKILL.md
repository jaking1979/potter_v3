---
name: memory-review
description: The owner's weekly review of what Potter proposes to remember (approve, edit, downgrade, reject), and applying the owner's answer. Also on request ("what do you want to remember about me", "memory review").
---
# Memory review

1. Call `memory_review`. If it says nothing is waiting, output nothing (a scheduled run stays silent).
2. Send the list exactly as returned. Add one line at the end only if a responsibility started being tracked this
   week (`responsibility_due` with a long window, items whose `told_owner` is false): "I also started tracking: ...".
3. When the owner answers ("approve 1 3, reject 2, edit 4: <text>, downgrade 5"), turn it into decisions and call
   `memory_review_apply` with exactly those. Never decide for the owner, never approve what was not named.
4. Report in one line what was written to MEMORY.md and what was dropped.

## Graph facts waiting (knowledge graph)
5. Also run `potter-kg-capture --list`. Each line is a fact Potter heard but Jev was not sure of, with its id and
   the owner's own words. Send them after the memory list as "Facts for the graph:" numbered from where the
   memory list stopped, each with the quote. "Nothing waiting." means skip this section.
6. On the owner's answer: `potter-kg-capture --approve <id,id>` for approved ones (written at high confidence),
   `potter-kg-capture --reject <id,id>` for rejected ones. Report the one-line result.
