---
name: capability-map
description: Rebuild the map of what Potter can actually reach (its tools, MCP servers, and every app connected through Composio) into TOOLS.md. Runs daily and after an update; also when the owner asks "what can you connect to" or a tool seems missing.
---
# Capability map

TOOLS.md is in every prompt, so this map is how you know what you can do. Build it from live evidence only.

1. **Your own tools.** List the tool names you have in this turn, grouped by prefix (native, `composio__`,
   `notion__`, `home-assistant__`, `agentmail__`, plugin tools such as `kg_*`, `memory_*`). Count per group.
2. **Composio apps.** Read the schema of `composio__COMPOSIO_MANAGE_CONNECTIONS` and use it only to LIST
   existing connections (never start, delete or change one). If it cannot list, call
   `composio__COMPOSIO_SEARCH_TOOLS` once with a query naming common apps (todoist, google calendar, gmail,
   google drive, google sheets, notion, github, spotify, slack, dropbox) and read the connection status it
   returns. For each app with an active connection record: the app, the account label if shown, and up to 8 tool
   slugs you saw for it.
3. Write the result into TOOLS.md between these two lines (create them at the end of the file if missing; never
   touch anything outside them):
   `<!-- potter:capabilities:start -->` and `<!-- potter:capabilities:end -->`
   Format:
   ```
   ## What I can reach (generated <date and time>, from live tools)
   - Native and plugin tools: <groups with counts>
   - MCP servers: <name: count>, ...
   - Apps through Composio (call composio__COMPOSIO_SEARCH_TOOLS once, keep its session_id, then
     composio__COMPOSIO_MULTI_EXECUTE_TOOL with the exact slugs it returns; never guess tool ids):
     - <App> (<account label>): <slugs>
   - Not connected (checked): <apps searched with no connection>
   ```
4. Also write the same facts as JSON to `state/capabilities.json` (`{"generated": ..., "groups": {...},
   "mcp": {...}, "composio": [{"app": ..., "account": ..., "slugs": [...]}], "missing": [...]}`).
5. If the owner asked, reply with the Composio apps list in one short block. If scheduled, output nothing.

Account labels are fine to record; tokens, keys and connection secrets are never written anywhere.
