# iMessage for Potter

Potter sends and receives iMessage through the always-on MacBook. Messages runs there under a **separate macOS
user signed in with Potter's own Apple ID**, so texts come from Potter, never from you, and your own Messages
history is never read. The Gateway on CT 211 reaches it over SSH on the home network.

Who can do what:
- Only your handles (your phone number and Apple ID email) can direct Potter, in a DM or a group.
- In a group (you, Edie and Potter), Potter answers only when named, and only to you. Edie sees what Potter
  sends but cannot direct it.
- Every message Potter sends to anyone but you asks you on Telegram first.

## 1. On the Mac (about 20 minutes; or paste the prompt below to Claude Code on the Mac)
1. Create a new Apple ID for Potter (an email you control, for example Potter's AgentMail address).
2. System Settings > Users & Groups: add a Standard user `potter`. Log in as it once, sign in to Messages with
   Potter's Apple ID, then switch back to your user (leave `potter` logged in: Fast User Switching keeps its
   Messages running).
3. As your admin user: `brew install steipete/tap/imsg`.
4. System Settings > General > Sharing > Remote Login: on, allowed for user `potter` only.
5. System Settings > Privacy & Security: Full Disk Access for `sshd-keygen-wrapper` (so imsg can read the
   Messages database over SSH), and allow Automation of Messages when macOS asks on the first send.
6. Give the Mac a fixed address on your router (a DHCP reservation), so Potter always finds it.

Prompt for Claude Code on the Mac:
> Set up iMessage for my assistant Potter on this Mac following docs/IMESSAGE_SETUP.md steps 2-5 in the
> potter_v3 repo: a Standard user `potter` (I will sign it in to Messages with Potter's Apple ID myself when you
> tell me), imsg via Homebrew, Remote Login for `potter` only, Full Disk Access for sshd-keygen-wrapper. Then add
> the SSH public key I paste to /Users/potter/.ssh/authorized_keys (permissions 700/600, owned by potter). Do not
> change anything else, and do not touch my own user's Messages.

## 2. On Potter (two messages)
1. Tell Potter **"set up iMessage"**. It runs `potter-imessage-setup key` and sends you a public key line: add it
   on the Mac (step in the prompt above).
2. Tell Potter **"connect iMessage to potter at <Mac address>; my handles are <+1 phone>, <Apple ID email>"**. It
   runs `potter-imessage-setup connect <address> potter <handles>`, checks imsg over SSH, configures the channel
   and restarts the gateway.

Then: "text me a test", and "text Edie and me: dinner at 7" (asks you first, then sends to the group).
SMS through a carrier number is possible too (OpenClaw's Twilio plugin), but US carrier registration (A2P 10DLC)
makes it a poor fit for family texts; iMessage covers it.
