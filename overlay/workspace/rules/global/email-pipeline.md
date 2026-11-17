# Email pipeline (how v3 runs email)

This file describes the mechanism. The owner's email preferences and boundaries live in the personal rules; where
an older personal rule describes a listener service, a queue script or a cron mechanism, this file wins.

1. **Potter's own inbox** is the only address Potter sends from. Sending as the owner from the owner's personal
   mailbox is locked: only on the owner's explicit, per-message "send this as me".
2. **Conversations by email:** mail from an address on the allowed list (the owner and any co-owner) reaches Potter
   through the AgentMail channel and is answered in the same thread. A co-owner's requests are followed like the
   owner's, except anything that changes Potter's own configuration, trades, or takes a consequential external
   action: route those to the owner.
3. **Everything else** (newsletters, forwards, notices) is handled by the scheduled `mail-intake` skill in a
   quarantined turn with no delivery channel.
4. **Urgent items** reach the owner only through `state/urgent-mail-queue.jsonl` and the model-free delivery
   program. No turn that has read untrusted email ever messages the owner directly.
5. **Silence is the default.** No "processed N emails". Nothing found or a failed check produces no message.
6. Inbox checks use the AgentMail tools, never hand-written shell one-liners.
7. Verification codes and account notices for Potter-run accounts go to Potter's inbox and stay there.
