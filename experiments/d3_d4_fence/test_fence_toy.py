"""Synthetic in-process generation fence experiment, not a production guard."""
import threading
import unittest
import uuid

class ToyAuthority:
    def __init__(self):
        self._lock = threading.Lock()
        self._epoch = uuid.uuid4().hex
        self._gen = 0
        self._active = set()
        self.published = []
    def add(self, item):
        with self._lock:
            self._active.add(item)
    def stamp(self):
        with self._lock:
            return (self._epoch, self._gen)
    def forget(self, item):
        with self._lock:
            self._active.discard(item)
            self._gen += 1
    def deliver(self, stamp, ids, value):
        # Toy append models the visibility boundary; real delivery cannot be a
        # network callback under a process-local mutex.
        with self._lock:
            if stamp != (self._epoch, self._gen) or not set(ids) <= self._active:
                return False
            self.published.append(value)
            return True

class FenceTests(unittest.TestCase):
    def setUp(self):
        self.a = ToyAuthority(); self.a.add('x')
    def test_forget_during_work(self):
        stamp = self.a.stamp(); self.a.forget('x')
        self.assertFalse(self.a.deliver(stamp, ['x'], 'sensitive'))
        self.assertEqual(self.a.published, [])
    def test_completed_result_before_delivery(self):
        stamp = self.a.stamp(); result = ('x', 'sensitive')
        self.a.forget('x')
        self.assertFalse(self.a.deliver(stamp, [result[0]], result[1]))
    def test_restart_invalidates_old_stamp(self):
        stamp = self.a.stamp(); restarted = ToyAuthority(); restarted.add('x')
        self.assertFalse(restarted.deliver(stamp, ['x'], 'sensitive'))
    def test_other_forget_invalidates_even_if_item_active(self):
        stamp = self.a.stamp(); self.a.forget('another')
        self.assertFalse(self.a.deliver(stamp, ['x'], 'sensitive'))
    def test_inactive_id_rejected(self):
        self.assertFalse(self.a.deliver(self.a.stamp(), ['missing'], 'sensitive'))
    def test_delivery_before_forget_cannot_be_recalled(self):
        self.assertTrue(self.a.deliver(self.a.stamp(), ['x'], 'already visible'))
        self.a.forget('x'); self.assertEqual(self.a.published, ['already visible'])
    def test_forget_wins_lock_before_delivery(self):
        stamp = self.a.stamp(); entered = threading.Event(); resume = threading.Event()
        def worker():
            entered.set(); resume.wait(2); self.a.deliver(stamp, ['x'], 'sensitive')
        t = threading.Thread(target=worker); t.start(); self.assertTrue(entered.wait(2))
        self.a.forget('x'); resume.set(); t.join(2)
        self.assertFalse(t.is_alive()); self.assertEqual(self.a.published, [])

if __name__ == '__main__': unittest.main()
