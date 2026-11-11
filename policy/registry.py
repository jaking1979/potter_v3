"""Capability registry for the C1 PolicyEngine (T11/T12).

Each capability declares its risk class, whether it needs an approval, and
whether it is stateful. Registration itself fails closed: a declaration that
would weaken policy (a consequential capability without approval, a forbidden
capability marked anything but forbidden) is rejected at registration time,
not discovered at decision time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class RiskClass(str, Enum):
    READ = "read"
    REVERSIBLE_WRITE = "reversible_write"
    CONSEQUENTIAL = "consequential"
    FORBIDDEN = "forbidden"


class UnknownCapability(LookupError):
    pass


class RegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class Capability:
    name: str
    risk: RiskClass
    approval_required: bool
    stateful: bool
    # Precondition keys a stateful capability must carry (e.g. "etag"). Empty
    # means "at least one precondition" is still required when stateful.
    required_preconditions: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise RegistrationError("capability name must be dotted lowercase")
        if not isinstance(self.risk, RiskClass):
            raise RegistrationError("risk must be a RiskClass")
        if not isinstance(self.approval_required, bool) or not isinstance(self.stateful, bool):
            raise RegistrationError("approval_required and stateful must be bool")
        if self.risk is RiskClass.CONSEQUENTIAL and not self.approval_required:
            raise RegistrationError("consequential capabilities always require approval")
        if self.risk is RiskClass.READ and self.approval_required:
            # Not unsafe, but almost certainly a mis-declaration; be explicit.
            raise RegistrationError("read capabilities do not take approval_required")
        if not isinstance(self.required_preconditions, tuple) or not all(
                isinstance(k, str) and k for k in self.required_preconditions):
            raise RegistrationError("required_preconditions must be a tuple of names")
        if self.required_preconditions and not self.stateful:
            raise RegistrationError("required_preconditions only apply to stateful capabilities")


class CapabilityRegistry:
    """Immutable after construction. Lookups of unregistered names raise."""

    def __init__(self, capabilities: tuple[Capability, ...] | list[Capability]):
        table: dict[str, Capability] = {}
        for cap in capabilities:
            if not isinstance(cap, Capability):
                raise RegistrationError("registry entries must be Capability")
            if cap.name in table:
                raise RegistrationError("duplicate capability")
            table[cap.name] = cap
        self._table: Mapping[str, Capability] = MappingProxyType(table)

    def get(self, name: str) -> Capability:
        cap = self._table.get(name) if isinstance(name, str) else None
        if cap is None:
            raise UnknownCapability("unregistered capability")
        return cap

    def names(self) -> frozenset[str]:
        return frozenset(self._table)
