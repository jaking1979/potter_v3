# Potter's vault: setup

Potter uses logins and payment cards you choose, without its model ever seeing a value (docs/V3_CORE.md, v3.11).
The vault is a self-hosted Vaultwarden (free, Bitwarden-compatible) in its own small container. Your Dashlane
and your work passwords are not involved.

## 1. The container (paste to the Proxmox Claude Code session)

> Create a new Proxmox LXC for Vaultwarden: first free ID from 212, hostname `vault`, Debian 13 template, 1 GB RAM,
> 4 GB disk, unprivileged, nesting on (for Docker). Install Docker from Docker's own apt repository and run
> `vaultwarden/server` (latest stable tag, pinned by digest) with a data volume at /srv/vaultwarden, restart always,
> environment `SIGNUPS_ALLOWED=true`, `INVITATIONS_ALLOWED=true`, `SHOW_PASSWORD_HINT=false`, listening on
> 127.0.0.1:8080 only. Install Tailscale in the container (state on disk), join my tailnet as `vault`, and run
> `tailscale serve --bg 8080` so it is reachable at https://vault.<my tailnet>.ts.net only on the tailnet. Do NOT use
> Funnel. Add the container to the nightly Proxmox backup (vzdump, keep 7). Do not touch CT 210 or CT 211. Report
> the https address and the container's memory use.

## 2. Accounts (you, about 10 minutes, in the browser)

1. Open the https address. Create **your** account (your email, a new master password; store it in Dashlane).
2. New organization "Potter" (free). Rename its default collection to "Potter".
3. Create **Potter's** account: a second email you control (for example Potter's AgentMail address) and its own
   master password. In your organization, invite it with access to the "Potter" collection only (role User,
   "Can view"). Accept the invite as Potter, then confirm it as you.
4. Tell the Proxmox session: "set `SIGNUPS_ALLOWED=false` on the vault and restart it."
5. Signed in as **Potter**: Account settings > Security > Keys > View API key. Keep that page open.

## 3. Connect Potter (one message)

Tell Potter **"set up your vault"**. It sends a one-time link. Open it, enter the vault address, Potter's
client_id, client_secret and master password. The form checks them and tells you which one to fix if any is wrong.

## 4. Use

- Add only what Potter should have: in the Bitwarden app (pointed at your vault address), save the item into the
  "Potter" collection. Cards: virtual card numbers only, locked to a merchant, with a limit.
- Optional: put `potter:auto` in a login's notes to let Potter use it without asking each time. Cards always ask.
- Ask "what's in your vault" to check. Every use is in Potter's vault audit (names and purposes, never values).
