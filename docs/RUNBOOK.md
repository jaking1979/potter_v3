# Install Potter v3 (one page)

**First, in Telegram (no terminal):** paste the message in `docs/V2_EXPORT_PROMPT.md` to your v2 Potter and save the
zip it sends back.

**Before you start (by Nov 24):** a Telegram bot token (@BotFather, /newbot) and your numeric Telegram user ID;
an OpenRouter key with a credit limit; optional: Composio MCP URL (read-only connections), AgentMail key, OpenAI
key for voice (with a limit), Home Assistant address + token for a dedicated Potter user, Tailscale auth key.

**1. Start the installer** (the only terminal step; the host-connected Claude session can do it for you).
On the Proxmox host: `git clone <repo> && cd potter_v3 && git checkout v3-core && ./install/install.sh --proxmox`
It creates a new container (never an existing one), then prints a web address.

**2. Fill in the form** at that address. Each box explains what to paste; upload the v2 export zip in its box.
Submit once. Watch the progress log.

**3. Finish page** shows the Potter web address and a one-time password. Save it in your password manager.

**4. Say hello** to your bot in Telegram. Ask: "what can you do right now?" and "why and what did that cost?".

**5. Connect more by chatting:** "connect my calendar", "connect AgentMail". Potter proposes; you approve.
Secrets go in the Settings form, never in chat.

**Check it:** in the container, `/opt/potter/src/install/verify_host.sh` prints PASS/FAIL per item.
**Remove it:** `/opt/potter/src/install/uninstall.sh`. Your other containers are not touched.

**After install:** ask Potter "what is in standby?". Items that act on shared accounts (trading loop, email
listener, monitors) wait until you say "take over <name>" and v2 has stopped doing that job. Secrets for imported
apps go in the settings page (port 8789, same password), never in chat.
