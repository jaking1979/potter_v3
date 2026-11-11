"""Offline counterexample: independently built D3/C3 have no forget wiring.

This is not a test of an integrated system or an authorization to send.
All content, databases, channel and verifier are synthetic.
"""
import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore
from runtime import Database, Outbox, OutboxMessage


class UnwiredForgetBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.memory = MemoryStore.bootstrap_offline(
            root / 'memory.db', lambda op, token, payload: token == ('test', op),
            environment='synthetic')
        self.db = Database(root / 'outbox.db')
        self.outbox = Outbox(self.db)
        staged = self.memory.stage(request_id='request-1', key='fixture',
                                   value='synthetic-forget-value',
                                   origin='external', source_ref='synthetic')
        self.item = self.memory.promote(staged.id, staged.version, ('test', 'promote'))
        with self.db.tx() as tx:
            self.outbox.enqueue(tx, OutboxMessage('message-1', 'fake', 'fake-josh',
                                {'text': self.item.value}), now=1.0)

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def forget(self):
        self.memory.forget(self.item.id, self.item.version, ('test', 'forget'))
        self.assertEqual(self.memory.retrieve('fixture', consequential=True).active, ())

    def test_pending_message_is_still_sent_after_forget(self):
        # C3 and D3 have no integrated forget contract. This asserts the
        # counterexample, not a desired production behavior.
        self.forget()
        self.assertEqual(self.outbox.get('message-1').status, 'pending')
        sent = []
        self.outbox.relay(lambda m: sent.append(m.body), lambda m: 'still_unknown',
                          owner='synthetic', now=2.0)
        self.assertEqual(sent, [{'text': 'synthetic-forget-value'}])

    def test_claimed_copy_can_send_after_forget(self):
        # The callback is entered after C3 commits UNKNOWN, but before our
        # fake send records visibility. A forget here cannot recall the copy.
        visible = []
        states = []
        def fake_send(message):
            states.append(self.outbox.get('message-1').status)
            self.forget()
            visible.append(message.body)
        self.outbox.relay(fake_send, lambda m: 'still_unknown',
                          owner='synthetic', now=2.0)
        self.assertEqual(states, ['unknown'])
        self.assertEqual(visible, [{'text': 'synthetic-forget-value'}])

    def test_expired_unknown_urgent_message_can_be_retried_after_forget(self):
        # A crash after claim leaves an ambiguous outcome.  Independently built
        # C3 has no tombstone authority and can issue its urgent resend.
        with self.db.tx() as tx:
            self.outbox.enqueue(tx, OutboxMessage('urgent-1', 'fake', 'fake-josh',
                                {'text': self.item.value}, urgency='urgent'), now=1.0)
        kind, claimed = self.outbox._claim('crashed-worker', 2.0)
        self.assertEqual((kind, claimed.message_id, claimed.status),
                         ('send', 'urgent-1', 'unknown'))
        self.forget()
        visible = []
        report = self.outbox.relay(lambda m: visible.append((m.message_id, m.body)),
                                   lambda m: 'still_unknown', owner='recovery',
                                   now=100000.0, limit=1)
        self.assertEqual(report.resent_unknown, ['urgent-1'])
        self.assertEqual(visible, [('urgent-1', {'text': 'synthetic-forget-value'})])


if __name__ == '__main__':
    unittest.main()
