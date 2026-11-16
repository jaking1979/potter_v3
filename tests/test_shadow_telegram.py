"""E3 ingress + E1 auth wired in front of the shadow turn loop (synthetic transport)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingress import IngressGateway, IngressStore, InMemoryAttachmentStore, TelegramUpdate  # noqa: E402
from notifier import ChannelAuth  # noqa: E402
from notifier.auth import TransportSigner  # noqa: E402
from identity.seed import JOSH  # noqa: E402
from shadow.telegram import TelegramFrontend  # noqa: E402
from test_shadow import World  # noqa: E402

T = 1_800_000_000
KEY = b"t" * 32
JOSH_TG, STRANGER = 1001, 2002


class Front(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.ok = set()
        self.w = World(self.tmp)
        self.gw = self.make_gw()
        self.front = TelegramFrontend(gateway=self.gw, runtime=self.w.rt)

    def make_gw(self):
        auth = ChannelAuth(db=os.path.join(self.tmp, "e1.db"), transport_keys={"telegram": KEY},
                           inbound_bindings={"telegram": {str(JOSH_TG): JOSH}},
                           outbound_addresses={JOSH: {"telegram": str(JOSH_TG)}},
                           dkim_verifier=lambda m: False, clock=lambda: float(T))
        return IngressGateway(store=IngressStore(os.path.join(self.tmp, "in.db")),
                              attachments=InMemoryAttachmentStore(), channel_auth=auth,
                              clock=lambda: T,
                              transport_signers={"telegram": TransportSigner("telegram", KEY)},
                              telegram_transport=lambda u: u.delivery_ref in self.ok)

    def upd(self, *, user=JOSH_TG, chat=None, mid=1, text="hello", ref=None, verified=True, **kw):
        ref = ref or f"d{mid}"
        if verified:
            self.ok.add(ref)
        return TelegramUpdate(update_id=mid, message_id=mid, chat_id=user if chat is None else chat,
                              chat_type=kw.pop("chat_type", "private"), from_user_id=user,
                              text=text, delivery_ref=ref, **kw)


class TestFront(Front):
    def test_owner_private_message_gets_a_reply(self):
        out = self.front.handle_update(self.upd())
        self.assertEqual(out.status, "replied")
        self.assertEqual(len(self.w.sent), 1)

    def test_stranger_gets_nothing(self):
        out = self.front.handle_update(self.upd(user=STRANGER))
        self.assertEqual((out.status, out.detail), ("ignored", "sender_not_allowlisted"))
        self.assertEqual(self.w.sent, [])

    def test_unverified_transport_gets_nothing(self):
        out = self.front.handle_update(self.upd(verified=False))
        self.assertEqual(out.status, "ignored")
        self.assertEqual(self.w.sent, [])

    def test_bot_sender_gets_nothing(self):
        out = self.front.handle_update(self.upd(from_is_bot=True))
        self.assertEqual(out.status, "ignored")
        self.assertEqual(self.w.sent, [])

    def test_forwarded_group_and_via_bot_never_drive_a_turn(self):
        for i, kw in enumerate((dict(forwarded=True), dict(chat_type="group", chat=-5),
                                dict(via_bot=True)), start=10):
            out = self.front.handle_update(self.upd(mid=i, **kw))
            self.assertEqual((out.status, out.detail), ("ignored", "untrusted_provenance"), kw)
        self.assertEqual(self.w.sent, [])
        self.assertEqual(self.gw.pending(), [])

    def test_redelivery_is_a_duplicate_and_costs_nothing(self):
        self.assertEqual(self.front.handle_update(self.upd(mid=7)).status, "replied")
        again = self.front.handle_update(self.upd(mid=7))
        self.assertEqual((again.status, again.detail), ("ignored", "duplicate"))
        self.assertEqual(len(self.w.sent), 1)

    def test_task_id_is_the_event_id_and_stable(self):
        seen = []
        orig = self.w.rt.handle
        self.w.rt.handle = lambda m: (seen.append(m), orig(m))[1]
        self.front.handle_update(self.upd(mid=3))
        [m] = seen
        self.assertEqual(m.task_id, m.event_id)
        self.assertEqual(m.provenance, ("principal_direct",))
        self.assertEqual(m.chat_id, str(JOSH_TG))

    def test_a_handled_event_is_acked_and_not_pending(self):
        self.front.handle_update(self.upd(mid=4))
        self.assertEqual(self.gw.pending(), [])

    def test_a_crash_before_ack_leaves_the_event_for_recovery(self):
        self.w.rt.handle = lambda m: (_ for _ in ()).throw(RuntimeError("crash"))
        with self.assertRaises(RuntimeError):
            self.front.handle_update(self.upd(mid=5))
        self.assertEqual(len(self.gw.pending()), 1)
        del self.w.rt.handle
        [out] = self.front.recover()
        self.assertEqual(out.status, "replied")
        self.assertEqual(self.gw.pending(), [])

    def test_gateway_exception_is_ignored_not_raised(self):
        class Boom:
            def ingest_telegram(self, u):
                raise RuntimeError("x")
        out = TelegramFrontend(gateway=Boom(), runtime=self.w.rt).handle_update(self.upd())
        self.assertEqual((out.status, out.detail), ("ignored", "ingress_error"))

    def test_a_forged_event_is_not_vouched(self):
        res = self.gw.ingest_telegram(self.upd(mid=9))
        class Fake:
            def ingest_telegram(self, u): return res
            def vouches(self, e): return False
            def ack(self, e): raise AssertionError
        out = TelegramFrontend(gateway=Fake(), runtime=self.w.rt).handle_update(self.upd(mid=9))
        self.assertEqual((out.status, out.detail), ("ignored", "not_vouched"))
        self.assertEqual(self.w.sent, [])


if __name__ == "__main__":
    unittest.main()
