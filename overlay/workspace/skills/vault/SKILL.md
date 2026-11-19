---
name: vault
description: Use a login or payment card the owner put in Potter's vault, without ever seeing it. Use when a task needs a password, a card, or the owner asks for something stored there ("what's in your vault", "log in to Con Ed", "pay with the virtual card", "send me my passport number").
---
# Vault

The owner chooses what goes in: items they put in the "Potter" collection of their Vaultwarden. Nothing else.
You never see a value, and you never ask the owner to type a password or card number into chat.

1. `vault_list` shows item names, kinds and fields. If the item the task needs is not there, tell the owner:
   "Add it to the Potter collection in your vault app, then tell me." Never ask for the value in chat.
2. `vault_authorize` with the item and a plain purpose. Cards always go to the owner for approval; logins too,
   unless the owner marked them `potter:auto` in the item's notes. Approval lasts 15 minutes.
3. In the browser, type `{{vault:<item name>#<field>}}` where the value goes (fields: username, password, totp,
   number, expiry, exp_month, exp_year, code, cardholder; for identities full_name, first_name, last_name, address1,
   address2, city, state, postal_code, country, phone, email, company; or a custom field name).
   Sensitive fields (ssn, passport_number, license_number, dob and the like) are approved one use at a time: call
   `vault_authorize` with `fields: ["ssn"]` for exactly the use at hand, never "just in case".
   Addresses come from the place items ("Bay Ridge — Home", "Catskills — House"); the default is the one the owner
   named. People's identity items hold their documents, not addresses. The real value is filled in
   at the last moment and removed from anything you see back.
4. The owner asks for a value itself (a passport number, a membership number): `vault_send_to_owner`. It goes
   straight to their chat; you learn only that it was sent.

## Never
- Put a reference into a shell command, a message, a file, memory, Notion or email (it is blocked anyway).
- Pay anything without the owner's approval of that card for that purpose.
- Store, repeat or guess a value. If you somehow see one, say so to the owner at once.
- Use the vault for anything clinical.

## Not set up yet
If a tool says "not set up yet", tell the owner: "The vault isn't connected. Say 'set up your vault' when it's
ready." On "set up your vault": run `potter-vault-setup` and send the owner the link it prints (it is a one-time
form on the home network or Tailscale; it closes itself).
