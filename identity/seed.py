"""Seed directory (package C5). Placeholder identities only; no secrets.

Grants are configuration and are not seeded here: they depend on the C1
capability registry, which C8 wires. ``SEED_SCOPES`` records the intended
data-domain ceiling per principal for that wiring.
"""
from __future__ import annotations

from .principals import Principal, PrincipalDirectory, PrincipalKind as K, SecretRef, ServiceAccount

JOSH = "human:josh"
CHRISTIE = "human:christie"

SEED_PRINCIPALS = (
    Principal(JOSH, K.HUMAN, description="Josh (primary principal)"),
    Principal(CHRISTIE, K.HUMAN, enabled=False,
              description="Christie (placeholder; disabled until her own grants are decided)"),
    Principal("agent:potter", K.AGENT, description="Potter orchestrator"),
    Principal("agent:research", K.AGENT, description="research sub-agent"),
    Principal("job:nightly-digest", K.SYSTEM_JOB, description="nightly digest cron"),
    Principal("job:retry-worker", K.SYSTEM_JOB, description="effect retry worker"),
    Principal("test:fixture-runner", K.TEST, description="synthetic test-domain principal"),
    Principal("test:fixture-subagent", K.TEST, description="synthetic test-domain sub-agent"),
    Principal("svc:gmail", K.SERVICE_ACCOUNT, description="Gmail connector identity"),
    Principal("svc:gcal", K.SERVICE_ACCOUNT, description="Google Calendar connector identity"),
    Principal("svc:notion", K.SERVICE_ACCOUNT, description="Notion connector identity"),
)

SEED_SERVICE_ACCOUNTS = (
    ServiceAccount("svc:gmail", "gmail", JOSH, SecretRef("secretref://keychain/potter-gmail-oauth")),
    ServiceAccount("svc:gcal", "gcal", JOSH, SecretRef("secretref://keychain/potter-gcal-oauth")),
    ServiceAccount("svc:notion", "notion", JOSH, SecretRef("secretref://keychain/potter-notion-token")),
)

SEED_SCOPES = {
    JOSH: ("josh/*", "household/*"),
    "agent:potter": ("josh/*", "household/*"),
    "agent:research": ("josh/notes/*", "household/*"),
    "job:nightly-digest": ("josh/mail/*", "josh/calendar/*"),
    "job:retry-worker": ("josh/*", "household/*"),
    "test:fixture-runner": ("test/*",),
    "test:fixture-subagent": ("test/*",),
}


def seed_directory() -> PrincipalDirectory:
    return PrincipalDirectory(SEED_PRINCIPALS, SEED_SERVICE_ACCOUNTS)
