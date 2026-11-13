"""Offline world for the E2 responsibility tests, importable by a spawned child
process (restart tests). Every connector is a fake over a local SQLite file;
keys are test constants. Nothing here touches a network or a real account."""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing, contextmanager
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identity.seed import JOSH  # noqa: E402
from notifier import (ApprovalIssuer, ChannelAuth, CommandRouter, FakeChannel,  # noqa: E402
                      HmacIssuerKey, Notifier, TransportSigner)
from policy import Capability, CapabilityRegistry, PolicyEngine, RiskClass  # noqa: E402
from policy._contracts import approval as c0  # noqa: E402
from responsibilities import (Connector, Event, Responsibility, ResponsibilityRunner,  # noqa: E402
                              Schedule, Step)
try:                                     # absent in the vendored legacy E2 code
    from responsibilities import kill_switch_for  # noqa: E402
except ImportError:                      # pragma: no cover - legacy fixtures only
    kill_switch_for = None
from runtime import Database, EffectFailed, EffectJournal, JournalApprovalLedger  # noqa: E402

T0 = 1_800_000_000.0
TEST_KEY = b"test-only-e2-issuer-key"          # test constant, not a secret
TG_JOSH = "tg-1001"
# Test-only adapter keys (E1 transport auth); not secrets.
TRANSPORT_KEYS = {"telegram": b"test-only-e2-telegram-adapter",
                  "email": b"test-only-e2-email-adapter-key"}
R = RiskClass
CAPS = (Capability("notes.append", R.REVERSIBLE_WRITE, False, False),
        Capability("bills.lookup", R.READ, False, False),
        Capability("mail.read", R.READ, False, False),
        Capability("bills.pay", R.CONSEQUENTIAL, True, False))
CONNECTOR = "fake.notes"


@contextmanager
def _conn(path: str):
    with closing(sqlite3.connect(path)) as c, c:
        yield c


class FakeExternal:
    """A provider that applies each idempotency key at most once, stored in its
    own SQLite file so a killed process's writes are visible after restart.
    ``hooks[effect_id] = (when, fn)`` runs fn before/after the apply."""

    def __init__(self, path: Path):
        self.path = str(path)
        with _conn(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied (key TEXT PRIMARY KEY, step TEXT,"
                      " task TEXT, n INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS calls (seq INTEGER PRIMARY KEY"
                      " AUTOINCREMENT, key TEXT, step TEXT)")
        self.hooks: dict[str, tuple[str, object]] = {}
        self.results: dict[str, object] = {}       # canned provider results per step

    def _step(self, eff) -> str:
        return eff.action().parameters.get("step", eff.effect_id)

    def perform(self, eff):
        step = self._step(eff)
        action = eff.action()                          # the journaled snapshot only
        with _conn(self.path) as c:
            c.execute("INSERT INTO calls (key, step) VALUES (?,?)", (eff.idempotency_key, step))
        when, fn = self.hooks.get(step, (None, None))
        if when == "before":
            fn(eff)
        with _conn(self.path) as c:
            c.execute("INSERT INTO applied (key, step, task, n) VALUES (?,?,?,1)"
                      " ON CONFLICT(key) DO UPDATE SET n=n+1",
                      (eff.idempotency_key, step, action.task_id))
        if when == "after":
            fn(eff)
        return self.results.get(step, {"step": step, "ok": True})

    def reconcile(self, eff) -> str:
        with _conn(self.path) as c:
            row = c.execute("SELECT 1 FROM applied WHERE key=?", (eff.idempotency_key,)).fetchone()
        return "succeeded" if row else "failed"     # "not applied (yet)"

    def applied(self, step: str | None = None) -> list[tuple[str, int]]:
        with _conn(self.path) as c:
            q = "SELECT step, n FROM applied" + (" WHERE step=?" if step else "") + " ORDER BY step"
            return [tuple(r) for r in c.execute(q, (step,) if step else ())]

    def calls(self, step: str | None = None) -> int:
        with _conn(self.path) as c:
            q = "SELECT COUNT(*) FROM calls" + (" WHERE step=?" if step else "")
            return c.execute(q, (step,) if step else ()).fetchone()[0]


def note_step(sid: str) -> Step:
    return Step(sid, "notes.append", "append", "notes/daily", CONNECTOR,
                parameters={"step": sid, "line": f"line for {sid}"})


def lookup_step(sid: str = "log") -> Step:
    return Step(sid, "bills.lookup", "get", "bills/acct-1", CONNECTOR,
                parameters=lambda ctx: {"step": sid, "bill": ctx.payload["event"]["id"]})


def pay_step(sid: str = "pay") -> Step:
    return Step(sid, "bills.pay", "pay", "bills/acct-1", CONNECTOR,
                parameters=lambda ctx: {"step": sid, "bill": ctx.payload["event"]["id"],
                                        "amount": ctx.payload["event"]["amount"]},
                approval_summary="Pay the electric bill?")


def batch(rid="notes.batch", delivery="josh", n=3, every=3600.0) -> Responsibility:
    return Responsibility(rid, Schedule(every), delivery,
                          tuple(note_step(f"s{i}") for i in range(1, n + 1)),
                          report=lambda ctx: f"Appended {len(ctx.results)} notes.")


def mail_read_step(sid: str = "read", sources=None) -> Step:
    return Step(sid, "mail.read", "list", "mail/inbox", CONNECTOR,
                parameters={"step": sid}, result_provenance=sources)


def derived_note_step(sid: str, src: str = "read") -> Step:
    """A write whose text comes from an earlier step's result."""
    return Step(sid, "notes.append", "append", "notes/daily", CONNECTOR,
                parameters=lambda ctx: {"step": sid,
                                        "line": (ctx.results.get(src) or {}).get("text", "")})


def mail_digest(sources=None, extra=()) -> Responsibility:
    """Scheduled (trusted trigger) read of external mail, then derived writes."""
    return Responsibility("mail.digest", Schedule(3600.0), "josh",
                          (mail_read_step("read", sources),) + tuple(extra))


def bill_watch() -> Responsibility:
    return Responsibility("bill.watch", Event("bill.due", ("id",)), "josh",
                          (lookup_step("log"), pay_step("pay")), cadence_s=7 * 86400.0,
                          report=lambda ctx: "Bill handled.")


class World:
    def __init__(self, tmp: str | Path, *, clock=None, definitions=None, worker="w1",
                 lease_s=60.0, fence=None, kill_switch=None):
        tmp = Path(tmp)
        self.now = T0
        self.clock = clock or (lambda: self.now)
        self.db = Database(tmp / "potter.db")
        self.ext = FakeExternal(tmp / "external.db")
        self.tg, self.email = FakeChannel("telegram"), FakeChannel("email")
        self.signer = TransportSigner("telegram", TRANSPORT_KEYS["telegram"])
        self.auth = ChannelAuth(db=self.db, transport_keys=TRANSPORT_KEYS, clock=self.clock,
                                inbound_bindings={"telegram": {TG_JOSH: JOSH}},
                                outbound_addresses={JOSH: {"telegram": "chat-1001",
                                                           "email": "josh@example.test"}})
        self.notifier = Notifier(self.db, channels={"telegram": self.tg, "email": self.email},
                                 auth=self.auth, clock=self.clock)
        self.issuer = ApprovalIssuer(self.db, key=HmacIssuerKey("e2-test", TEST_KEY),
                                     notifier=self.notifier, auth=self.auth, clock=self.clock)
        self.ledger = JournalApprovalLedger(self.db, clock=self.clock)
        self.engine = PolicyEngine(registry=CapabilityRegistry(CAPS),
                                   grants={"agent:potter": frozenset(c.name for c in CAPS)},
                                   ledger=self.ledger,
                                   authenticator=c0.HmacAuthenticator({"e2-test": TEST_KEY}),
                                   clock=lambda: int(self.clock()))
        if kill_switch is None and kill_switch_for is not None:
            kill_switch = kill_switch_for(self.db)
        self.journal = EffectJournal(self.db, clock=self.clock, fence=fence,
                                     kill_switch=kill_switch)
        self.definitions = definitions if definitions is not None else [batch(), bill_watch()]
        self.runner = ResponsibilityRunner(
            self.db, definitions=self.definitions, journal=self.journal, policy=self.engine,
            ledger=self.ledger, issuer=self.issuer, handle_for=self.notifier.handle_for,
            connectors=[Connector(CONNECTOR, self.ext.perform, self.ext.reconcile)],
            worker_id=worker, lease_s=lease_s, clock=self.clock)
        self.router = CommandRouter(auth=self.auth, notifier=self.notifier, issuer=self.issuer,
                                    live_action=self.runner.live_action, clock=self.clock)

    def say(self, text: str):
        # A trusted Telegram adapter's signed envelope; update ids are unique
        # across World instances sharing one database (restart tests).
        msg = self.signer.envelope(TG_JOSH, text, update_id=f"u-{uuid.uuid4().hex}",
                                   sent_at=self.clock())
        return self.router.handle(msg, self.clock())

    def outbox_ids(self) -> list[str]:
        with self.db.read() as c:
            return [r[0] for r in c.execute("SELECT notification_id FROM outbox"
                                            " WHERE channel='telegram' ORDER BY created_at,"
                                            " message_id")]

    def body(self, nid: str) -> dict:
        import json
        with self.db.read() as c:
            row = c.execute("SELECT body FROM outbox WHERE notification_id=? AND"
                            " channel='telegram'", (nid,)).fetchone()
        return json.loads(row[0]) if row else None


def fail_definitely(eff):
    raise EffectFailed("provider rejected (4xx)")


def child_main(tmp: str, signal_path: str, phase: str) -> None:
    """Spawned by the restart tests. Real clock. Runs notes.batch; step s2
    signals the parent and then hangs, before or after the provider applied it,
    so the parent can SIGKILL this process mid-dispatch."""
    w = World(tmp, clock=time.time, definitions=[batch()])

    def hang(eff):
        Path(signal_path).write_text("in-flight")
        time.sleep(120)

    w.ext.hooks["s2"] = (phase, hang)
    w.runner.tick()
    w.runner.run_once()


def child_poll(tmp: str, go_path: str, worker: str) -> None:
    """Spawned by the approval-race test: wait for the start signal, then
    run one poll_approvals at the same moment as the other children."""
    w = World(tmp, worker=worker)
    deadline = time.time() + 30
    while not Path(go_path).exists() and time.time() < deadline:
        time.sleep(0.005)
    w.runner.poll_approvals()


if __name__ == "__main__":
    if sys.argv[1] == "poll":
        child_poll(*sys.argv[2:5])
    else:
        child_main(*sys.argv[1:4])
