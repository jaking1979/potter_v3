"""E3: ingress adapters - the untrusted-source boundary (T11/T12; T19 PASS 2 items 2+4).
Staging-only (T22): every transport and verifier is injected; only offline fakes exist."""
from .content import IngressLimits
from .events import (ALL_KINDS, ATTACHMENT, EXTERNAL_SENDER, FORWARDED_CONTENT,
                     PRINCIPAL_DIRECT, QUOTED_CONTENT, UNTRUSTED_KINDS, WEB_CONTENT,
                     WEBHOOK_PAYLOAD, AttachmentRef, InboundEvent, Segment)
from .gateway import (REASONS, EmailAuthVerdict, IngressGateway, IngressResult, RawAttachment,
                      TelegramUpdate, WebhookDelivery)
from .store import AttachmentStore, InMemoryAttachmentStore, IngressStore

__all__ = [n for n in dir() if not n.startswith("_")]
