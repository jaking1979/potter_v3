> **Superseded for the install by docs/SCOPE.md.** The table below is the long-range menu, not the Nov 27-29 build.

# Potter v3 build plan (Josh, 2026-11-16: "deliver the full thing")

Goal: one installer puts on the OptiPlex (fresh Proxmox container, never v2's infrastructure) the latest
OpenClaw, Josh's overlay, every v3 capability, and our own PWA. The same installer walks anyone else through
their own install. Everything is built by reusing existing open-source projects and configuring or modifying
them. Nothing already in this repo is deleted; the earlier Python runtime lives on as `legacy core`
(policy, trace, broker, governor) and earns its way back only where a reused project cannot do the job.

## Architecture (one coherent assistant, hidden specialists)
```
 PWA (chat + voice, native feel)  <->  Potter gateway (thin)  <->  OpenClaw harness
        Telegram channel  ---------------------------------------^      |
                                                                          +-- MCP connectors (read/act under policy)
                                                                          +-- browser sandbox, vault, voice agent
                                                                          +-- scheduler (briefs, monitors)
 Policy + approvals (deterministic) wraps every tool call;  trace + cost log;  memory
```
Potter logic stays behind stable interfaces (AgentRuntime, MemoryStore, ToolRegistry, PolicyEngine,
Scheduler, TraceSink) so OpenClaw stays swappable, as the charter requires.

> Update 2026-11-16: installing OpenClaw showed it already ships most of rows 3, 4, 6, 7, 12, 13, 14, 16 (see
> docs/OPENCLAW_FINDINGS.md). Our work on those rows is configuration, branding and skills, not new code.

## Capability -> reuse source -> what we write
Candidates below get the T24 one-day reuse check (license, maintenance, fit) before adoption.
| # | Capability | Reuse (candidate) | We write |
|---|---|---|---|
| 1 | Harness, Telegram, cron, model routing, sandbox | OpenClaw (MIT) | locked config template, update policy |
| 2 | Installer | Proxmox community-scripts LXC pattern + whiptail wizard | guided installer, forms, verify_host, uninstall |
| 3 | PWA (chat, voice, today, approvals, trace, settings) | OpenClaw Control UI as reference, assistant-ui or similar (MIT) | the PWA itself |
| 4 | Voice (in-app) | browser audio + provider STT/TTS, or LiveKit Agents (Apache-2.0) | voice session glue, push-to-talk, barge-in |
| 5 | Phone number + calls | Twilio/Telnyx + LiveKit SIP | number provisioning step, call policy |
| 6 | Browser/computer use | Playwright MCP / browser-use | sandbox profile, live view, approval hooks |
| 7 | Credential vault | Vaultwarden + Bitwarden CLI | vault bridge that never exposes secrets to prompts |
| 8 | Email + calendar + docs | Google Workspace MCP (MIT) | scope presets, triage and brief skills |
| 9 | Notion | official Notion MCP | read/write scoping |
| 10 | Home Assistant | HA built-in MCP server | entity allowlist, per-room actions |
| 11 | Payments (OFF by default) | Stripe Link / agent toolkit | spend caps, approval flow |
| 12 | Memory + person model | OpenClaw memory, mem0/Letta/Lore (reuse check) | provenance + editable memory UI |
| 13 | Proactivity, monitoring, responsibilities | OpenClaw cron + our policy | interruption policy, proactivity dial |
| 14 | Policy, approvals, earned autonomy | OpenClaw exec approvals + legacy core C1 | approval inbox, rules |
| 15 | Trace, cost, evaluation | Langfuse or OpenTelemetry; legacy cost log | trace/cost views, Josh Task Set evals |
| 16 | Multi-person | OpenClaw multi-agent profiles | per-person isolation, installer flow |

## Phases (each ends in something Josh can run)
1. Base install: container + OpenClaw + locked config + Telegram + models + 8:30 ET Morning Brief. (installer v0)
2. PWA v1: chat, today, approvals, trace/cost; Google Workspace, Notion, Home Assistant connectors.
3. Voice in the PWA; browser sandbox with live view; vault.
4. Phone number and calls; payments (switch-gated); proactive monitors; multi-person.
5. Evaluation against the Josh Task Set and the parity table; tuning.
Target: phases 1-2 installable over Nov 27-29; the rest as fast behind as they verify.

## Safety defaults (deterministic, not model judgment)
Outward actions (send, buy, trade, write, home control) are OFF per install until switched on; each switch
is logged. Clinical/EHR content and the CMC mailbox are excluded by rule. No secrets in the repo, Notion or chat.
