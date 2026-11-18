---
name: kg-import
description: Bring another knowledge graph file (such as the copy of v2's graph) into the live graph. Use when the owner asks to import, seed or merge a graph.
---
# KG import

1. Preview first: `potter-kg-import --from <graph file> --dry-run`. The copy of v2's graph is
   `/root/v2-kg/potter-kg.sqlite`. Reply with the one-line preview and ask the owner to confirm.
2. On "yes": `potter-kg-import --from <graph file>`. Reply with its one line.
3. Then say: "The graph is read on every request from now on (within 30 seconds). Ask me about someone or
   something in it, then ask for the last line of governor.jsonl to see what was used."

It only adds. The live graph's own records are never overwritten; imported records are tagged with the source
file's name. Clinical or patient material is never imported.
