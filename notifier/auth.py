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
* ``AuthenticatedSender`` vouchers are minted only here. Each carries an
  unguessable token; this instance keeps the exact minted object and its
  claims. ``vouches`` accepts only that same object with unchanged claims, so
  a constructed lookalike, a ``dataclasses.replace`` copy (e.g. an email
  voucher relabelled as Telegram) or a mutated voucher is refused.
  ``consume`` additionally retires the voucher: an approval decision uses it
  exactly once.
"""
from __future__ import annotations

import hmac
import itertools
import secrets
import threading
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
    _token: str = field(repr=False, compare=False, default="")


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
        # token -> (the exact minted object, its claims at mint time)
        self._minted: dict[str, tuple[AuthenticatedSender, tuple[str, str, str]]] = {}
        self._lock = threading.Lock()

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
        token = secrets.token_hex(32)
        sender = AuthenticatedSender(principal, msg.channel, serial, token)
        with self._lock:
            self._minted[token] = (sender, (principal, msg.channel, str(serial)))
        return AuthResult(sender, "ok")

    @staticmethod
    def _verified(verifier: Verifier | None, msg: InboundMessage) -> bool:
        if verifier is None:
            return False
        try:
            return verifier(msg) is True
        except Exception:
            return False

    def _check(self, sender: object) -> str | None:
        """Return the voucher's token if ``sender`` is exactly what was minted."""
        if type(sender) is not AuthenticatedSender:
            return None
        token = sender._token
        if not isinstance(token, str) or not token:
            return None
        entry = self._minted.get(token)
        if entry is None:
            return None
        minted, (principal, channel, serial) = entry
        if minted is not sender:
            return None
        claims = (sender.principal, sender.channel, str(sender._serial))
        ok = True
        for got, want in zip(claims, (principal, channel, serial)):
            if not isinstance(got, str) or not hmac.compare_digest(got.encode(), want.encode()):
                ok = False
        if not hmac.compare_digest(token.encode(), sender._token.encode()):
            ok = False
        return token if ok else None

    def vouches(self, sender: object) -> bool:
        """True only for the exact voucher this instance minted, unmodified."""
        with self._lock:
            return self._check(sender) is not None

    def consume(self, sender: object) -> bool:
        """Like ``vouches``, then retire the voucher (single use for approvals)."""
        with self._lock:
            token = self._check(sender)
            if token is None:
                return False
            del self._minted[token]
            return True

    def address(self, principal: str, channel: str) -> str | None:
        return self._out.get(principal, {}).get(channel)
