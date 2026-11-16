# Potter v3 vs. the current personal-agent products

Rule from the charter: if v3 is not meaningfully better for Josh's life than these, it is not good enough.
Sources are launch-week press coverage and vendor pages (vendor pages for Dots and Muse could not be
fetched from the build environment; treat press details as unverified until Josh or Dolores confirm).

| Capability | Dots (OpenAI) | Muse (Meta) | Duet (Anthropic) | Instinct | Grok Bot (xAI) | Potter v3 plan |
|---|---|---|---|---|---|---|
| Always-on, works after you close the app | yes, own cloud computer | yes, own VM | yes, own cloud env | yes | yes, VM per agent | yes: runs on the OptiPlex, scheduled + event-driven jobs |
| Channels | app, chat | app, web, iOS/Android, WhatsApp | own app | text and phone call | chat/messenger | PWA (chat + voice), Telegram; phone later in sequence |
| Voice | voice instruction | not confirmed | not confirmed | phone call | not confirmed | PWA voice (push-to-talk + hands-free) |
| Own phone number / calls | reported | not confirmed | no | yes | no | Twilio/Telnyx number via a SIP voice agent (phase 3) |
| Browser / computer use | virtual browser | VM | cannot yet show its browser | uses phone + computer | cloud VM | Playwright MCP in a sandboxed browser with live view in the PWA |
| Email + calendar | Gmail, Docs, Calendar | Gmail, apps | morning scan, drafts, every-few-hours checks | yes | not confirmed | Google Workspace MCP; read first, drafts, send behind approval |
| Purchases / payments | gated | Stripe Link one-time cards | asks before paying | orders groceries, tickets | confirmations | Stripe Link style one-time cards; OFF by default, per-install switch |
| Approvals / permission modes | read-only background mode, built-in rules | approval for sensitive actions | asks before send/pay/sign/delete | not confirmed | asks confirmation | deterministic policy + approval inbox in the PWA; earned autonomy |
| Memory / personalization | learns preferences | memory | not confirmed | understands what you work on | not confirmed | memory + person model, provenance-aware, inspectable and editable |
| Proactive updates, adjustable | proactive research | returns when something changes | messages you first | not confirmed | not confirmed | proactivity dial, interruption policy, quiet by default |
| Activity log / "why did it do that" | partial | not confirmed | partial | no | no | full trace + cost view per action (our edge) |
| Credential handling | connected accounts | confidential VM planned | connectors | not confirmed | not confirmed | vault; secrets never enter prompts or logs |
| Home / IoT | no | no | no | no | no | Home Assistant (our edge) |
| Multiple people | per user | per user | per user | per user | per user | one install per person, separate identity/memory/credentials (Christie, Molly, Edie) |
| Privacy / data location | vendor cloud | vendor cloud | vendor cloud | vendor cloud, flagged | vendor cloud | self-hosted on the OptiPlex; model calls only leave the box |
| Cost control | subscription | free + $20/$100 | usage heavy | subscription | n/a | hard spend ceilings, per-task cost shown |

Where v3 can win: continuity across Josh's actual world (two homes, Home Assistant, finances, clinical and
product work), self-hosting and privacy, per-action traceability, spend control. Where v3 loses by default:
polish, voice naturalness, breadth of reliable actions. The PWA, voice and browser work close those gaps first.
