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
* Replay without loss: each authentic ``(channel, update_id)`` is recorded
  durably in ``inbound_seen`` as ``pending`` with a sha256 digest of the
  exact signed envelope (payload + MAC; never the body). The command's
  effect marks it ``processed`` in the same transaction (``complete``).
  - Never seen before: must be within ``max_skew_s`` of its signed time.
  - ``processed``: ignored (``replayed``).
  - ``pending`` (crashed after authenticating, before the effect committed):
    the recovery path. The redelivered envelope must match the stored digest
    byte for byte; freshness is NOT reapplied (the outage may be long), but
    authorization (binding, allowlist, enabled, DKIM/session) is rechecked.
    A different envelope under the same update id is ``replayed``.
  Two concurrent deliveries of a pending update race on ``complete``'s
  compare-and-set; the loser's effect rolls back.
* Clock rollback (fail closed): each transport keeps a durable high-water
  mark in the shared DB (``hw:<channel>``): the max accepted signed
  ``sent_at`` and the max clock reading seen while authenticating. If the
  clock reads more than ``CLOCK_TOLERANCE_S`` behind the max clock of ANY
  transport, every command is refused as ``clock_regression`` until an
  operator calls ``reconcile_clock()``. A never-seen envelope must be fresh
  against the clock AND have ``sent_at >= high_water.max_sent_at -
  max_skew_s``, so an envelope whose journal row was pruned can never be
  accepted again, whatever the wall clock says. Pruning uses the same
  watermark (``sent_at < max_sent_at - prune_horizon_s``), never raw wall
  time. The watermark only moves forward. Limit: restoring a DB copy taken
  BEFORE an envelope was first accepted, while the clock is still within
  ``max_skew_s`` of that envelope, is indistinguishable from first delivery
  without an external monotonic anchor (documented in the README).
* Retention: vouchers (which hold the verified text for command binding) are
  released after dispatch and capped at ``MAX_MINTED``; journal rows keep
  only the digest, and processed rows are pruned once a replay of them would
  be rejected as stale anyway (``2 * max_skew_s`` + margin).
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
MAX_MINTED = 1024            # live vouchers kept in memory (oldest evicted)
PRUNE_MARGIN_S = 60.0
CLOCK_TOLERANCE_S = 5.0      # clock may read this far behind the high-water

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


class AlreadyProcessed(Exception):
    """The update behind this voucher has already been executed."""


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
        _schema.ensure(self.db, clock=clock)
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
        """None if the envelope's MAC verifies; else a reason. Freshness and
        replay are decided against ``inbound_seen`` in ``authenticate``."""
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
        if not isinstance(msg.sent_at, (int, float)):
            return "unauthenticated_transport"
        return None

    def _fresh(self, msg: InboundMessage, now: float, hw: dict) -> bool:
        if abs(now - msg.sent_at) > self._skew:
            return False
        floor = hw.get("max_sent_at")
        return floor is None or msg.sent_at >= floor - self._skew

    # ------------------------------------------------------------------ high-water (read-only)
    @property
    def max_skew_s(self) -> float:
        return self._skew

    @property
    def prune_horizon_s(self) -> float:
        return 2 * self._skew + PRUNE_MARGIN_S

    @property
    def clock_tolerance_s(self) -> float:
        return CLOCK_TOLERANCE_S

    def high_water(self, channel: str) -> dict:
        """Read-only copy of the persisted high-water mark for ``channel``."""
        with self.db.read() as c:
            return dict(self._hw(c, channel))

    @staticmethod
    def _hw(c: sqlite3.Connection, channel: str) -> dict:
        """The channel's mark, never below the migration seed (if any)."""
        out = {"max_sent_at": None, "max_clock": None}
        for key in (_schema.SEED_KEY, f"hw:{channel}"):
            raw = _schema.get_state(c, key)
            if not raw:
                continue
            for k, v in json.loads(raw).items():
                if k in out and v is not None:
                    out[k] = v if out[k] is None else max(out[k], v)
        return out

    # ------------------------------------------------------------------ migrated pending rows
    def needs_operator(self) -> list[tuple[str, str]]:
        """Pre-migration pending commands: never auto-recovered (status)."""
        with self.db.read() as c:
            return [tuple(r) for r in c.execute(
                "SELECT channel, update_id FROM inbound_seen WHERE status='needs_operator'"
                " ORDER BY channel, update_id")]

    def burn_needs_operator(self, channel: str, update_id: str) -> bool:
        """Operator act: close a migrated pending command WITHOUT executing it."""
        with self.db.tx() as c:
            cur = c.execute("UPDATE inbound_seen SET status='processed', processed_at=?"
                            " WHERE channel=? AND update_id=? AND status='needs_operator'",
                            (self._clock(), channel, update_id))
            return cur.rowcount == 1

    @staticmethod
    def _max_clock(c: sqlite3.Connection) -> float | None:
        vals = [json.loads(v).get("max_clock") for (v,) in c.execute(
            "SELECT value FROM notifier_state WHERE key LIKE 'hw:%'")]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def _advance(self, c: sqlite3.Connection, channel: str, *, clock: float,
                 sent_at: float | None) -> None:
        hw = self._hw(c, channel)
        hw["max_clock"] = clock if hw["max_clock"] is None else max(hw["max_clock"], clock)
        if sent_at is not None:
            hw["max_sent_at"] = (sent_at if hw["max_sent_at"] is None
                                 else max(hw["max_sent_at"], sent_at))
        _schema.set_state(c, f"hw:{channel}", json.dumps(hw, sort_keys=True))

    def reconcile_clock(self) -> None:
        """Operator act after a clock regression is understood: accept the
        current clock as the new max clock on every transport. The sent_at
        watermark is NOT lowered, so already-seen envelopes stay rejected."""
        now = self._clock()
        with self.db.tx() as c:
            for (key,) in c.execute("SELECT key FROM notifier_state WHERE key LIKE 'hw:%'"
                                    ).fetchall():
                hw = json.loads(_schema.get_state(c, key))
                hw["max_clock"] = now
                _schema.set_state(c, key, json.dumps(hw, sort_keys=True))

    @staticmethod
    def _digest(msg: InboundMessage) -> str:
        return hashlib.sha256(transport_payload(msg) + b"|" + msg.transport_mac.encode()
                              ).hexdigest()

    def _principal(self, msg: InboundMessage) -> tuple[str | None, str]:
        """(principal, "ok") or (None, reason) for an authentic envelope."""
        principal = self._in.get(msg.channel, {}).get(msg.sender_ref)
        if principal is None:
            return None, "unknown_sender"
        if principal not in self._allow:
            return None, "not_allowlisted"
        p = self._dir.get(principal)
        if p is None or not p.enabled:
            return None, "principal_disabled"
        if msg.channel == TELEGRAM:
            pass  # adapter-verified update (MAC), bound user id
        elif msg.channel == EMAIL:
            if not self._verified(self._dkim, msg):
                return None, "dkim_unverified"
        elif msg.channel == POTTER_HOME:
            if not self._verified(self._session, msg):
                return None, "session_unverified"
        else:
            return None, "unknown_channel"
        return principal, "ok"

    # ------------------------------------------------------------------ authenticate
    def authenticate(self, msg: InboundMessage) -> AuthResult:
        if not isinstance(msg, InboundMessage) or not msg.sender_ref:
            return AuthResult(None, "malformed")
        bad = self._transport_ok(msg)
        if bad:
            return AuthResult(None, bad)
        digest = self._digest(msg)
        principal, reason = self._principal(msg)       # always (re)checked
        now = self._clock()
        with self.db.tx() as c:
            max_clock = self._max_clock(c)
            if max_clock is not None and now < max_clock - CLOCK_TOLERANCE_S:
                return AuthResult(None, "clock_regression")       # fail closed, no writes
            hw = self._hw(c, msg.channel)
            row = c.execute("SELECT status, envelope_digest FROM inbound_seen"
                            " WHERE channel=? AND update_id=?",
                            (msg.channel, msg.update_id)).fetchone()
            if row is not None:
                status, stored = row
                if status == "needs_operator":
                    return AuthResult(None, "needs_operator")      # migrated: never resumed
                if status == "processed" or not hmac.compare_digest(stored, digest):
                    self._advance(c, msg.channel, clock=now, sent_at=None)
                    return AuthResult(None, "replayed")
                # pending + identical envelope: recovery path (no freshness check).
                if principal is None:
                    # Authorization no longer holds: close it, never execute.
                    c.execute("UPDATE inbound_seen SET status='processed', processed_at=?"
                              " WHERE channel=? AND update_id=?",
                              (now, msg.channel, msg.update_id))
                self._advance(c, msg.channel, clock=now, sent_at=None)
            else:
                if not self._fresh(msg, now, hw):
                    return AuthResult(None, "stale_transport")      # nothing recorded
                if principal is None:
                    # Authentic but not obeyed: record as done so it is never retried.
                    c.execute("INSERT INTO inbound_seen (channel, update_id, at, status,"
                              " principal, envelope_digest, sent_at, processed_at)"
                              " VALUES (?,?,?,'processed',NULL,?,?,?)",
                              (msg.channel, msg.update_id, now, digest, msg.sent_at, now))
                else:
                    c.execute("INSERT INTO inbound_seen (channel, update_id, at, status,"
                              " principal, envelope_digest, sent_at)"
                              " VALUES (?,?,?,'pending',?,?,?)",
                              (msg.channel, msg.update_id, now, principal, digest,
                               msg.sent_at))
                self._advance(c, msg.channel, clock=now, sent_at=msg.sent_at)
            self._prune(c, msg.channel)
        if principal is None:
            return AuthResult(None, reason)
        serial = next(self._serials)
        token = secrets.token_hex(32)
        sender = AuthenticatedSender(principal, msg.channel, serial, token, msg.text or "",
                                     msg.reply_to, msg.update_id)
        with self._lock:
            self._minted[token] = (sender, self._claims(sender))
            while len(self._minted) > MAX_MINTED:
                self._minted.pop(next(iter(self._minted)))
        return AuthResult(sender, "ok")

    def _prune(self, c: sqlite3.Connection, channel: str) -> None:
        """Drop processed rows whose signed ``sent_at`` is older than the
        transport high-water minus ``prune_horizon_s``: any replay of them is
        already rejected by the watermark floor. Never by wall time; pending
        rows are never pruned (they must stay resumable)."""
        floor = self._hw(c, channel).get("max_sent_at")
        if floor is None:
            return
        c.execute("DELETE FROM inbound_seen WHERE channel=? AND status='processed'"
                  " AND sent_at < ?", (channel, floor - self.prune_horizon_s))

    def release(self, sender: object) -> None:
        """Forget a voucher (and the text it carries) once dispatch is over."""
        if type(sender) is AuthenticatedSender:
            with self._lock:
                self._minted.pop(sender._token, None)

    def complete(self, c: sqlite3.Connection, sender: AuthenticatedSender) -> bool:
        """Mark the voucher's update ``processed`` inside the caller's effect
        transaction. False if it was already processed (the caller must not
        apply its effect; raising rolls the transaction back)."""
        cur = c.execute("UPDATE inbound_seen SET status='processed', processed_at=?"
                        " WHERE channel=? AND update_id=? AND status='pending'",
                        (self._clock(), sender.channel, sender.update_id))
        return cur.rowcount == 1

    def finish(self, sender: AuthenticatedSender) -> None:
        """Mark processed in its own transaction (idempotent). For outcomes
        with no effect of their own (refusals) or an idempotent one (ack)."""
        with self.db.tx() as c:
            self.complete(c, sender)

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
