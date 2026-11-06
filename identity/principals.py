"""Principals, service accounts and SecretRef handles (package C5, T11).

Every actor Potter recognises has a stable, typed ID whose prefix names its
kind (``human:josh``, ``agent:planner``, ``job:nightly-digest``,
``test:fixture-runner``, ``svc:gmail``). Service accounts exist one per
connector and carry a ``SecretRef`` handle only: this module never holds,
accepts or returns a secret value.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


class IdentityError(ValueError):
    """Raised at construction time for a malformed identity declaration."""


class PrincipalKind(str, enum.Enum):
    HUMAN = "human"
    SYSTEM_JOB = "job"
    AGENT = "agent"
    TEST = "test"
    SERVICE_ACCOUNT = "svc"


# Kinds that may originate a request (be ``Caller.principal`` in C1).
CALLER_KINDS = frozenset({PrincipalKind.HUMAN, PrincipalKind.SYSTEM_JOB,
                          PrincipalKind.AGENT, PrincipalKind.TEST})
# Kinds that may appear as a delegation hop, per hop kind.
HOP_PRINCIPAL_KINDS = {
    "subagent": frozenset({PrincipalKind.AGENT, PrincipalKind.TEST}),
    "cron": frozenset({PrincipalKind.SYSTEM_JOB, PrincipalKind.TEST}),
    "retry": frozenset({PrincipalKind.AGENT, PrincipalKind.SYSTEM_JOB, PrincipalKind.TEST}),
}

_ID = re.compile(r"^(human|job|agent|test|svc):[a-z0-9][a-z0-9_.-]{0,63}$")
_CONNECTOR = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A handle into a secret store: scheme, store, name. Deliberately narrow so a
# raw token, password or key material cannot be smuggled in as a "reference".
_SECRET_REF = re.compile(r"^secretref://[a-z][a-z0-9-]{0,31}/[a-z0-9][a-z0-9._-]{0,63}$")


def valid_principal_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


@dataclass(frozen=True)
class SecretRef:
    """Opaque handle to a secret held elsewhere (keychain / vault).

    There is intentionally no method that resolves it; resolution is a host
    concern behind a later package. The handle itself is not secret.
    """
    ref: str

    def __post_init__(self):
        if not isinstance(self.ref, str) or not _SECRET_REF.match(self.ref):
            raise IdentityError("SecretRef must be secretref://<store>/<name>")


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: PrincipalKind
    enabled: bool = True
    description: str = ""

    def __post_init__(self):
        if not valid_principal_id(self.principal_id):
            raise IdentityError("principal id must be <kind>:<name>")
        if not isinstance(self.kind, PrincipalKind):
            raise IdentityError("kind must be a PrincipalKind")
        if self.principal_id.split(":", 1)[0] != self.kind.value:
            raise IdentityError("principal id prefix must match its kind")
        if not isinstance(self.enabled, bool) or not isinstance(self.description, str):
            raise IdentityError("enabled must be bool, description str")

    @property
    def can_originate(self) -> bool:
        return self.enabled and self.kind in CALLER_KINDS


@dataclass(frozen=True)
class ServiceAccount:
    """The identity a connector acts as. One per connector."""
    principal_id: str
    connector: str
    owner: str                 # principal id of the human/system that owns it
    secret_ref: SecretRef

    def __post_init__(self):
        if not valid_principal_id(self.principal_id) or not self.principal_id.startswith("svc:"):
            raise IdentityError("service account id must be svc:<name>")
        if not isinstance(self.connector, str) or not _CONNECTOR.match(self.connector):
            raise IdentityError("connector must be a lowercase name")
        if not valid_principal_id(self.owner) or self.owner.startswith("svc:"):
            raise IdentityError("owner must be a non-service principal id")
        if not isinstance(self.secret_ref, SecretRef):
            raise IdentityError("service accounts hold a SecretRef, never a secret")


class PrincipalDirectory:
    """Immutable after construction. Unknown IDs resolve to ``None``."""

    def __init__(self, principals: Iterable[Principal],
                 service_accounts: Iterable[ServiceAccount] = ()):
        table: dict[str, Principal] = {}
        for p in principals:
            if not isinstance(p, Principal):
                raise IdentityError("directory entries must be Principal")
            if p.principal_id in table:
                raise IdentityError("duplicate principal id")
            table[p.principal_id] = p
        accounts: dict[str, ServiceAccount] = {}
        for sa in service_accounts:
            if not isinstance(sa, ServiceAccount):
                raise IdentityError("service account entries must be ServiceAccount")
            if sa.connector in accounts:
                raise IdentityError("one service account per connector")
            p = table.get(sa.principal_id)
            if p is None or p.kind is not PrincipalKind.SERVICE_ACCOUNT:
                raise IdentityError("service account needs a svc principal")
            if sa.owner not in table:
                raise IdentityError("service account owner must be a known principal")
            accounts[sa.connector] = sa
        self._principals: Mapping[str, Principal] = MappingProxyType(table)
        self._accounts: Mapping[str, ServiceAccount] = MappingProxyType(accounts)

    def get(self, principal_id: object) -> Principal | None:
        return self._principals.get(principal_id) if isinstance(principal_id, str) else None

    def service_account(self, connector: str) -> ServiceAccount | None:
        return self._accounts.get(connector) if isinstance(connector, str) else None

    def ids(self) -> frozenset[str]:
        return frozenset(self._principals)

    def __iter__(self):
        return iter(self._principals.values())
