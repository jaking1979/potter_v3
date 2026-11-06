"""Which inbound senders count as Josh, per channel (T19 PASS 2 items 2 and 4).

* Commands are accepted only from allowlisted principal IDs. The allowlist is
  ``human:josh``; ``human:christie`` is a disabled placeholder (C5 seed) and is
  ignored even if bound. Anyone else is ignored and logged.
* A channel authenticates a sender only through its own trusted signal:
  - ``telegram``: the platform-authenticated user id must be bound to the
    principal (Telegram signs the update; the adapter supplies the user id).
  - ``email`` (fallback): the From address must be bound AND the injected
    DKIM verifier must report a pass for this exact message. No verifier
    configured -> never authenticated.
  - ``potter_home``: the injected session verifier must confirm the session
    (passkey-backed). No verifier -> never authenticated.
* ``AuthenticatedSender`` tokens are minted only here, and ``vouches`` lets
  the issuer check a token came from this instance (a caller cannot construct
  one to bypass authentication).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Mapping

from identity import PrincipalDirectory, seed_directory
from identity.seed import CHRISTIE, JOSH

from .channels import EMAIL, POTTER_HOME, TELEGRAM, InboundMessage

DEFAULT_ALLOWLIST = frozenset({JOSH, CHRISTIE})   # Christie present but disabled (C5)

Verifier = Callable[[InboundMessage], bool]


@dataclass(frozen=True)
class AuthenticatedSender:
    principal: str
    channel: str
    _serial: int = field(repr=False, compare=False, default=0)


@dataclass(frozen=True)
class AuthResult:
    sender: AuthenticatedSender | None
    reason: str           # "ok" or a fixed code

    @property
    def ok(self) -> bool:
        return self.sender is not None


class ChannelAuth:
    def __init__(self, *, inbound_bindings: Mapping[str, Mapping[str, str]],
                 outbound_addresses: Mapping[str, Mapping[str, str]],
                 dkim_verifier: Verifier | None = None,
                 session_verifier: Verifier | None = None,
                 directory: PrincipalDirectory | None = None,
                 allowlist: frozenset[str] = DEFAULT_ALLOWLIST):
        """``inbound_bindings[channel][sender_ref] -> principal``;
        ``outbound_addresses[principal][channel] -> address``."""
        self._in = {ch: dict(m) for ch, m in inbound_bindings.items()}
        self._out = {p: dict(m) for p, m in outbound_addresses.items()}
        self._dkim = dkim_verifier
        self._session = session_verifier
        self._dir = directory or seed_directory()
        self._allow = frozenset(allowlist)
        self._serials = itertools.count(1)
        self._minted: set[int] = set()

    def authenticate(self, msg: InboundMessage) -> AuthResult:
        if not isinstance(msg, InboundMessage) or not msg.sender_ref:
            return AuthResult(None, "malformed")
        principal = self._in.get(msg.channel, {}).get(msg.sender_ref)
        if principal is None:
            return AuthResult(None, "unknown_sender")
        if principal not in self._allow:
            return AuthResult(None, "not_allowlisted")
        p = self._dir.get(principal)
        if p is None or not p.enabled:
            return AuthResult(None, "principal_disabled")
        if msg.channel == TELEGRAM:
            pass  # platform-authenticated user id, bound above
        elif msg.channel == EMAIL:
            if not self._verified(self._dkim, msg):
                return AuthResult(None, "dkim_unverified")
        elif msg.channel == POTTER_HOME:
            if not self._verified(self._session, msg):
                return AuthResult(None, "session_unverified")
        else:
            return AuthResult(None, "unknown_channel")
        serial = next(self._serials)
        self._minted.add(serial)
        return AuthResult(AuthenticatedSender(principal, msg.channel, serial), "ok")

    @staticmethod
    def _verified(verifier: Verifier | None, msg: InboundMessage) -> bool:
        if verifier is None:
            return False
        try:
            return verifier(msg) is True
        except Exception:
            return False

    def vouches(self, sender: object) -> bool:
        return isinstance(sender, AuthenticatedSender) and sender._serial in self._minted

    def address(self, principal: str, channel: str) -> str | None:
        return self._out.get(principal, {}).get(channel)
