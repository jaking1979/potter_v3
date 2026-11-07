"""Which inbound messages count as Josh, per channel (T19 PASS 2 items 2 and 4).

Transport authentication (message-bound, integrity- and replay-protected):

* Only a trusted channel adapter can produce an acceptable ``InboundMessage``.
  The adapter verifies the platform's own proof (Telegram webhook secret,
  inbound-mail pipeline, Potter Home session) and then signs the *whole*
  update - channel, sender, text, reply_to, update id and time - with its
  per-channel transport key (``TransportSigner``). ``ChannelAuth`` holds the
  same keys and refuses any message whose MAC does not verify, so a
  hand-built ``InboundMessage('telegram', josh_id, '/stop')`` is ignored, and
  so is a genuine update whose text or reply target was changed.
* Replay: each ``(channel, update_id)`` is accepted once, durably (the
  ``inbound_seen`` table in the shared SQLite file), and only within
  ``max_skew_s`` of its signed time.
* No key for a channel -> nothing on that channel is ever authenticated.

Principal checks (after the transport check):

* Commands are accepted only from allowlisted principal IDs. The allowlist is
  ``human:josh``; ``human:christie`` is a disabled placeholder (C5 seed) and is
  ignored even if bound. Anyone else is ignored and logged.
* Per channel: ``email`` additionally needs the injected DKIM verifier to pass
  for this exact message; ``potter_home`` needs the injected session verifier.
  No verifier configured -> never authenticated.

Vouchers:

* ``AuthenticatedSender`` vouchers are minted only here and are bound to the
  verified message: they carry its text, reply target and update id. Each has
  an unguessable token; this instance keeps the exact minted object and its
  claims, and ``vouches`` accepts only that same object, unmodified. A
  constructed lookalike, a ``dataclasses.replace`` copy or a mutated voucher
  is refused. ``consume`` additionally retires it (one approval decision).
  Consumers (stop, ack, approve/deny) check that the voucher's verified
  command is the action being taken, so a voucher minted for ``/ack`` cannot
  drive ``/stop`` or ``/approve``.

Test keys only: real adapter keys are T11/T12 key custody and out of scope.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

from identity import PrincipalDirectory, seed_directory
from identity.seed import CHRISTIE, JOSH
from runtime import Database
from runtime.db import as_database

from . import _schema
from .channels import EMAIL, POTTER_HOME, TELEGRAM, InboundMessage

DEFAULT_ALLOWLIST = frozenset({JOSH, CHRISTIE})   # Christie present but disabled (C5)
DEFAULT_MAX_SKEW_S = 300.0

Verifier = Callable[[InboundMessage], bool]


def transport_payload(msg: InboundMessage) -> bytes:
    """The exact bytes a transport MAC covers: every field but the MAC."""
    return json.dumps([msg.channel, msg.sender_ref, msg.text, msg.reply_to, msg.raw_ref,
                       msg.update_id, msg.sent_at], separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


class TransportSigner:
    """What a trusted channel adapter uses to hand a verified update to the
    router. TEST KEYS ONLY in this package."""

    def __init__(self, channel: str, key: bytes):
        if not channel or not isinstance(key, bytes) or len(key) < 16:
            raise ValueError("transport signer needs a channel and a >=16-byte key")
        self.channel = channel
        self._key = key

    def envelope(self, sender_ref: str, text: str, *, update_id: str, sent_at: float,
                 reply_to: str | None = None, raw_ref: str | None = None) -> InboundMessage:
        msg = InboundMessage(self.channel, sender_ref, text, reply_to=reply_to,
                             raw_ref=raw_ref, update_id=update_id, sent_at=float(sent_at))
        mac = hmac.new(self._key, transport_payload(msg), hashlib.sha256).hexdigest()
        return replace(msg, transport_mac=mac)


@dataclass(frozen=True)
class AuthenticatedSender:
    principal: str
    channel: str
    _serial: int = field(repr=False, compare=False, default=0)
    _token: str = field(repr=False, compare=False, default="")
    # The verified message this voucher was minted for.
    text: str = field(repr=False, compare=False, default="")
    reply_to: str | None = field(repr=False, compare=False, default=None)
    update_id: str = field(repr=False, compare=False, default="")

    def command(self) -> tuple[str, str | None]:
        """(command word, argument or reply target) from the verified text."""
        words = (self.text or "").strip().split()
        cmd = words[0].lower() if words else ""
        return cmd, (words[1] if len(words) > 1 else self.reply_to)


@dataclass(frozen=True)
class AuthResult:
    sender: AuthenticatedSender | None
    reason: str           # "ok" or a fixed code

    @property
    def ok(self) -> bool:
        return self.sender is not None


class ChannelAuth:
    def __init__(self, *, db: Database | str, transport_keys: Mapping[str, bytes],
                 inbound_bindings: Mapping[str, Mapping[str, str]],
                 outbound_addresses: Mapping[str, Mapping[str, str]],
                 dkim_verifier: Verifier | None = None,
                 session_verifier: Verifier | None = None,
                 directory: PrincipalDirectory | None = None,
                 allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
                 clock: Callable[[], float] = time.time,
                 max_skew_s: float = DEFAULT_MAX_SKEW_S):
        """``transport_keys[channel]`` verifies adapter envelopes;
        ``inbound_bindings[channel][sender_ref] -> principal``;
        ``outbound_addresses[principal][channel] -> address``."""
        self.db = as_database(db)
        _schema.ensure(self.db)
        self._keys = {ch: bytes(k) for ch, k in transport_keys.items()}
        self._in = {ch: dict(m) for ch, m in inbound_bindings.items()}
        self._out = {p: dict(m) for p, m in outbound_addresses.items()}
        self._dkim = dkim_verifier
        self._session = session_verifier
        self._dir = directory or seed_directory()
        self._allow = frozenset(allowlist)
        self._clock = clock
        self._skew = max_skew_s
        self._serials = itertools.count(1)
        # token -> (the exact minted object, its claims at mint time)
        self._minted: dict[str, tuple[AuthenticatedSender, tuple[str, ...]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ transport
    def _transport_ok(self, msg: InboundMessage) -> str | None:
        """None if the envelope is authentic, fresh and new; else a reason."""
        key = self._keys.get(msg.channel)
        if key is None:
            return "unknown_channel"
        mac = msg.transport_mac
        if not isinstance(mac, str) or not mac:
            return "unauthenticated_transport"
        try:
            want = hmac.new(key, transport_payload(msg), hashlib.sha256).hexdigest()
        except (TypeError, ValueError):
            return "unauthenticated_transport"
        if not hmac.compare_digest(want.encode(), mac.encode()):
            return "unauthenticated_transport"
        if not isinstance(msg.update_id, str) or not msg.update_id:
            return "unauthenticated_transport"
        if not isinstance(msg.sent_at, (int, float)) or abs(self._clock() - msg.sent_at) > self._skew:
            return "stale_transport"
        try:
            with self.db.tx() as c:
                c.execute("INSERT INTO inbound_seen (channel, update_id, at) VALUES (?,?,?)",
                          (msg.channel, msg.update_id, self._clock()))
        except sqlite3.IntegrityError:
            return "replayed"
        return None

    # ------------------------------------------------------------------ authenticate
    def authenticate(self, msg: InboundMessage) -> AuthResult:
        if not isinstance(msg, InboundMessage) or not msg.sender_ref:
            return AuthResult(None, "malformed")
        bad = self._transport_ok(msg)
        if bad:
            return AuthResult(None, bad)
        principal = self._in.get(msg.channel, {}).get(msg.sender_ref)
        if principal is None:
            return AuthResult(None, "unknown_sender")
        if principal not in self._allow:
            return AuthResult(None, "not_allowlisted")
        p = self._dir.get(principal)
        if p is None or not p.enabled:
            return AuthResult(None, "principal_disabled")
        if msg.channel == TELEGRAM:
            pass  # adapter-verified update (MAC above), bound user id
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
        sender = AuthenticatedSender(principal, msg.channel, serial, token, msg.text or "",
                                     msg.reply_to, msg.update_id)
        with self._lock:
            self._minted[token] = (sender, self._claims(sender))
        return AuthResult(sender, "ok")

    @staticmethod
    def _claims(s: AuthenticatedSender) -> tuple[str, ...]:
        return (s.principal, s.channel, str(s._serial), s.text, s.reply_to or "\x00",
                s.update_id)

    @staticmethod
    def _verified(verifier: Verifier | None, msg: InboundMessage) -> bool:
        if verifier is None:
            return False
        try:
            return verifier(msg) is True
        except Exception:
            return False

    # ------------------------------------------------------------------ vouchers
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
        minted, want = entry
        if minted is not sender:
            return None
        try:
            got = self._claims(sender)
        except Exception:
            return None
        ok = len(got) == len(want)
        for g, w in zip(got, want):
            if not isinstance(g, str) or not hmac.compare_digest(g.encode(), w.encode()):
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
