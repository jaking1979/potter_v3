import tempfile
import sqlite3
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from model_broker.broker import Broker, Candidate, Caps, Refused, Request, LedgerUnavailable


def req(call="c1", task="12345678-1234-4234-8234-123456789abc", privacy="private", day="2026-11-06", month="2026-11"):
    return Request(call, task, "research", privacy, day, month)


def candidate(model="local", expected=4, worst=10, **kwargs):
    return Candidate(model, "synthetic", kwargs.get("approved", True), frozenset({"research"}),
                     frozenset(kwargs.get("privacy", {"private"})), expected, worst,
                     kwargs.get("available", True))


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "broker.sqlite"
        self.caps = Caps(50, 20, 30, 60)
        self.b = Broker.bootstrap(self.path, self.caps)

    def test_sqlite_amount_overflow_refuses_and_preserves_state(self):
        huge = 1 << 63
        with self.assertRaises(ValueError):
            Broker(self.path, Caps(huge, 20, 30, 60))
        with self.assertRaisesRegex(Refused, 'malformed_candidate'):
            self.b.reserve(req('overflow'), [candidate(expected=0, worst=huge)])
        self.assertIsNone(self.b.status('overflow'))
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        with self.assertRaisesRegex(Refused, 'unknown_or_invalid_cost'):
            self.b.settle('c1', huge)
        self.assertEqual(self.b.status('c1')[-1], 'dispatched')
        self.assertEqual(self.b.entries('c1'), (('reservation', 10, 'operation'),))
        reopened = Broker(self.path, self.caps)
        self.assertEqual(reopened.status('c1')[-1], 'dispatched')

    def test_rollover_refuses_stale_without_releasing_after_restart(self):
        b = Broker(self.path, Caps(100, 1000, 100, 1000))
        b.reserve(req('old', day='2026-11-30', month='2026-11'),
                  [candidate(expected=100, worst=100)])
        b.reserve(req('new', day='2026-12-01', month='2026-12'),
                  [candidate(expected=100, worst=100)])
        b.dispatch_once('new', dispatch_day='2026-12-01')
        for current in ('2026-12-01', '2026-12-01'):
            with self.assertRaisesRegex(Refused, 'reservation_period_changed'):
                Broker(self.path, Caps(100,1000,100,1000)).dispatch_once('old', dispatch_day=current)
        self.assertEqual(b.status('old')[-1], 'reserved')
        self.assertEqual(b.entries('old'), (('reservation',100,'operation'),))
        with self.assertRaisesRegex(Refused, 'duplicate_call_id'):
            b.reserve(req('old', day='2026-12-01', month='2026-12'),
                      [candidate(expected=100, worst=100)])
        self.assertEqual(b.entries('new'), (('reservation',100,'operation'),))

    def test_same_month_rollover_and_invalid_dates_fail_closed(self):
        self.b.reserve(req(), [candidate()])
        for day in ('2026-11-07', '2026-11-05'):
            with self.assertRaisesRegex(Refused, 'reservation_period_changed'):
                self.b.dispatch_once('c1', dispatch_day=day)
        for day in ('20261106', '2026-02-30', '', None):
            with self.assertRaisesRegex(Refused, 'invalid_dispatch_day'):
                self.b.dispatch_once('c1', dispatch_day=day)
        self.assertEqual(self.b.status('c1')[-1], 'reserved')
        self.assertEqual(len(self.b.entries('c1')), 1)

    def test_concurrent_rollover_attempts_never_dispatch_or_release(self):
        self.b.reserve(req(), [candidate()])
        barrier, results = threading.Barrier(4), []
        def attempt():
            b = Broker(self.path, self.caps)
            barrier.wait()
            try:
                b.dispatch_once('c1', dispatch_day='2026-11-07')
                results.append('dispatched')
            except Refused as exc:
                results.append(str(exc))
        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(results, ['reservation_period_changed'] * 4)
        self.assertEqual(self.b.status('c1')[-1], 'reserved')
        self.assertEqual(self.b.entries('c1'), (('reservation',10,'operation'),))

    def test_budget_bucket_variants_cannot_bypass_ceilings(self):
        task = "12345678-1234-4234-8234-123456789abc"
        strict = Broker(self.path, Caps(100, 100, 100, 100))
        strict.reserve(req(task=task), [candidate(expected=100, worst=100)])
        variants = [
            dict(day="2026-11-6"), dict(day="2026-11-06 "),
            dict(day="26-11-06"), dict(day="20261106"),
            dict(day="2026-02-30", month="2026-02"),
            dict(month="2026-11 "), dict(month="2026-1"),
            dict(month="2026-12"),
            dict(task=task.upper()), dict(task="{" + task + "}"),
            dict(task=task.replace("-", "")), dict(task=task + " "),
        ]
        for i, variant in enumerate(variants):
            with self.subTest(variant=variant):
                with self.assertRaisesRegex(Refused, "invalid_budget_bucket"):
                    strict.reserve(req("alternate-" + str(i), **variant),
                                   [candidate(expected=100, worst=100)])
        with self.assertRaisesRegex(Refused, "budget_ceiling"):
            strict.reserve(req("same-bucket"), [candidate(expected=1, worst=1)])

    def test_bad_bucket_already_in_ledger_fails_closed_on_reopen_and_use(self):
        self.b.reserve(req(), [candidate()])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE broker_calls SET day='2026-11-6'")
        with self.assertRaisesRegex(LedgerUnavailable, 'invalid_call_record'):
            Broker(self.path, self.caps)
        with self.assertRaisesRegex(LedgerUnavailable, 'invalid_call_record'):
            self.b.reserve(req('next'), [candidate()])

    def test_invalid_legacy_bucket_refused_before_migration(self):
        path = Path(self.tmp.name) / 'bad-buckets.sqlite'
        self._legacy(path)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE broker_calls SET task_id='t1' WHERE call_id='old'")
        with self.assertRaisesRegex(LedgerUnavailable, 'invalid_call_record'):
            Broker(path, self.caps)
        with sqlite3.connect(path) as db:
            self.assertEqual({r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}, {'broker_calls'})

    def test_selects_cheapest_validated_approved_private_available(self):
        selected = self.b.reserve(req(), [candidate("badprivacy", 1, 2, privacy={"public"}),
                                          candidate("offline", 0, 1, available=False),
                                          candidate("unapproved", 0, 1, approved=False),
                                          candidate("large", 8, 10), candidate("local", 3, 10)])
        self.assertEqual(selected.model, "local")

    def test_reservation_survives_restart_and_unknown_cost(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once("c1", dispatch_day='2026-11-06')
        b2 = Broker(self.path, self.caps)
        with self.assertRaises(Refused): b2.settle("c1", None)
        with self.assertRaises(Refused): b2.dispatch_once("c1", dispatch_day='2026-11-06')
        b2.reserve(req("c2"), [candidate()])
        with self.assertRaisesRegex(Refused, "budget_ceiling"):
            b2.reserve(req("c3"), [candidate(worst=1, expected=1)])
        self.assertEqual(b2.status("c1")[-1], "dispatched")

    def test_settle_frees_unused_reservation_and_prevents_replay(self):
        self.b.reserve(req(), [candidate()]); self.b.dispatch_once("c1", dispatch_day='2026-11-06'); self.b.settle("c1", 3)
        self.b.reserve(req("c2"), [candidate(worst=17)])
        with self.assertRaises(Refused): self.b.settle("c1", 3)
        with self.assertRaises(Refused): self.b.reserve(req(), [candidate()])

    def test_overrun_blocks_subsequent_calls(self):
        self.b.reserve(req(), [candidate()]); self.b.dispatch_once("c1", dispatch_day='2026-11-06')
        with self.assertRaisesRegex(Refused, "cost_variance"): self.b.settle("c1", 11)
        with self.assertRaisesRegex(Refused, "unresolved_cost_variance"):
            self.b.reserve(req("c2"), [candidate()])

    def test_concurrent_reservations_cannot_overspend(self):
        barrier = threading.Barrier(2); outcomes = []
        def reserve(call):
            b = Broker(self.path, self.caps); barrier.wait()
            try: b.reserve(req(call), [candidate(worst=15)]); outcomes.append("ok")
            except Refused: outcomes.append("refused")
        threads = [threading.Thread(target=reserve, args=(x,)) for x in ("c1", "c2")]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(outcomes, ["ok", "refused"])

    def test_each_cap_is_checked(self):
        for cap in (Caps(9,99,99,99), Caps(99,9,99,99), Caps(99,99,9,99), Caps(99,99,99,9)):
            b = Broker(self.path, cap)
            with self.assertRaisesRegex(Refused, "budget_ceiling"): b.reserve(req(), [candidate()])

    def test_no_implicit_more_expensive_escalation(self):
        with self.assertRaisesRegex(Refused, "budget_ceiling"):
            self.b.reserve(req(), [candidate("cheap",1,21), candidate("expensive",2,10)])

    def test_settlement_appends_only_unused_difference_and_survives_restart(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        self.b.settle('c1', 3)
        reopened = Broker(self.path, self.caps)
        self.assertEqual(reopened.entries('c1'),
                         (('reservation', 10, 'operation'), ('settlement', -7, 'operation')))
        self.assertEqual(reopened.status('c1')[2:], (10, 3, 'settled'))
        reopened.reserve(req('c2'), [candidate(worst=17)])
        with self.assertRaisesRegex(Refused, 'budget_ceiling'):
            reopened.reserve(req('c3'), [candidate(worst=1, expected=1)])

    def test_concurrent_settlements_release_once(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        barrier, outcomes = threading.Barrier(2), []
        def settle():
            b = Broker(self.path, self.caps)
            barrier.wait()
            try:
                b.settle('c1', 3)
                outcomes.append('ok')
            except Refused as exc:
                outcomes.append(str(exc))
        threads = [threading.Thread(target=settle) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertCountEqual(outcomes, ['ok', 'not_dispatched'])
        self.assertEqual(len(self.b.entries('c1')), 2)

    def test_failed_settlement_transaction_keeps_full_reservation(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TRIGGER injected_failure BEFORE UPDATE ON broker_calls
                        BEGIN SELECT RAISE(ABORT,'injected_failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected_failure'):
            self.b.settle('c1', 3)
        self.assertEqual(self.b.entries('c1'), (('reservation', 10, 'operation'),))
        self.assertEqual(self.b.status('c1')[-1], 'dispatched')

    def test_process_crash_before_commit_preserves_reservation(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        code = '''import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1],isolation_level=None)
db.execute('BEGIN IMMEDIATE')
db.execute("INSERT INTO broker_entries(call_id,kind,amount,origin) VALUES ('c1','settlement',-7,'operation')")
db.execute("UPDATE broker_calls SET state='settled',actual=3 WHERE call_id='c1'")
os._exit(0)
'''
        subprocess.run([sys.executable, '-c', code, str(self.path)], check=True, timeout=10)
        reopened = Broker(self.path, self.caps)
        self.assertEqual(reopened.entries('c1'), (('reservation', 10, 'operation'),))
        self.assertEqual(reopened.status('c1')[-1], 'dispatched')

    def test_entry_update_and_delete_are_refused(self):
        self.b.reserve(req(), [candidate()])
        for statement in ('UPDATE broker_entries SET amount=0', 'DELETE FROM broker_entries'):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append_only'):
                with sqlite3.connect(self.path) as db:
                    db.execute(statement)
        self.assertEqual(self.b.entries('c1'), (('reservation', 10, 'operation'),))

    def test_overrun_entry_persists_and_blocks_after_restart(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        with self.assertRaisesRegex(Refused, 'cost_variance'):
            self.b.settle('c1', 12)
        reopened = Broker(self.path, self.caps)
        self.assertEqual(reopened.entries('c1')[-1], ('settlement', 2, 'operation'))
        with self.assertRaisesRegex(Refused, 'unresolved_cost_variance'):
            reopened.reserve(req('c2'), [candidate()])

    def test_unknown_schema_version_refused_even_by_existing_instance(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE broker_meta SET version=999')
        with self.assertRaisesRegex(LedgerUnavailable, 'unknown_schema_version'):
            Broker(self.path, self.caps)
        with self.assertRaisesRegex(LedgerUnavailable, 'unknown_schema_version'):
            self.b.reserve(req(), [candidate()])

    def test_inconsistent_call_row_does_not_free_budget(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE broker_calls SET state='settled',actual=0")
        with self.assertRaisesRegex(LedgerUnavailable, 'ledger_inconsistent'):
            self.b.reserve(req('c2'), [candidate()])

    def _legacy(self, path, actual=3):
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE broker_calls (
                call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                task_class TEXT NOT NULL, privacy_class TEXT NOT NULL,
                day TEXT NOT NULL, month TEXT NOT NULL, model TEXT NOT NULL,
                provider TEXT NOT NULL, expected INTEGER NOT NULL,
                worst INTEGER NOT NULL, actual INTEGER,
                state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','settled','overrun')))''')
            db.execute('INSERT INTO broker_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('old', '12345678-1234-4234-8234-123456789abc', 'research', 'private', '2026-11-06', '2026-11',
                        'fake', 'synthetic', 4, 10, actual, 'settled'))
            db.execute('INSERT INTO broker_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('unknown', '22345678-1234-4234-8234-123456789abc', 'research', 'private', '2026-11-06', '2026-11',
                        'fake', 'synthetic', 4, 10, None, 'dispatched'))

    def test_known_legacy_snapshot_migrates_atomically_without_releasing_unknown(self):
        path = Path(self.tmp.name) / 'legacy.sqlite'
        self._legacy(path)
        b = Broker(path, self.caps)
        self.assertEqual(b.entries('old'), (('reservation', 10, 'legacy_snapshot'),
                                            ('settlement', -7, 'legacy_snapshot')))
        self.assertEqual(b.entries('unknown'), (('reservation', 10, 'legacy_snapshot'),))
        Broker(path, self.caps)
        self.assertEqual(len(b.entries('old')), 2)
        with self.assertRaisesRegex(Refused, 'not_reserved'):
            b.dispatch_once('unknown', dispatch_day='2026-11-06')

    def test_invalid_legacy_snapshot_refused_without_partial_migration(self):
        path = Path(self.tmp.name) / 'invalid.sqlite'
        self._legacy(path, actual=11)  # settled contradicts actual > worst
        with self.assertRaisesRegex(LedgerUnavailable, 'invalid_call_state'):
            Broker(path, self.caps)
        with sqlite3.connect(path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {'broker_calls'})

    def test_missing_store_is_not_created_by_open_or_existing_instance(self):
        missing = Path(self.tmp.name) / 'missing.sqlite'
        with self.assertRaisesRegex(LedgerUnavailable, 'store_unavailable'):
            Broker(missing, self.caps)
        self.assertFalse(missing.exists())
        self.b.reserve(req(), [candidate()])
        self.path.unlink()
        with self.assertRaisesRegex(LedgerUnavailable, 'store_unavailable'):
            self.b.reserve(req('c2'), [candidate()])
        self.assertFalse(self.path.exists())

    def test_empty_store_refused_and_bootstrap_never_replaces_existing(self):
        empty = Path(self.tmp.name) / 'empty.sqlite'
        empty.touch()
        with self.assertRaisesRegex(LedgerUnavailable, 'empty_store'):
            Broker(empty, self.caps)
        with self.assertRaisesRegex(LedgerUnavailable, 'bootstrap_refused'):
            Broker.bootstrap(empty, self.caps)
        self.b.reserve(req(), [candidate()])
        with self.assertRaisesRegex(LedgerUnavailable, 'bootstrap_refused'):
            Broker.bootstrap(self.path, self.caps)
        self.assertEqual(self.b.status('c1')[-1], 'reserved')

    def test_bootstrap_concurrent_processes_have_only_one_winner(self):
        fresh = Path(self.tmp.name) / 'fresh.sqlite'
        code = '''import sys
from model_broker.broker import Broker,Caps,LedgerUnavailable
try:
    Broker.bootstrap(sys.argv[1], Caps(50,20,30,60))
except LedgerUnavailable:
    print('refused')
else:
    print('created')
'''
        children = [subprocess.Popen([sys.executable, '-c', code, str(fresh)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(4)]
        results = [p.communicate(timeout=10) for p in children]
        self.assertTrue(all(p.returncode == 0 for p in children), results)
        self.assertCountEqual([out.strip() for out, err in results],
                              ['created', 'refused', 'refused', 'refused'])
        Broker(fresh, self.caps).reserve(req(), [candidate()])

    def test_overrun_blocks_already_reserved_dispatch_after_restart(self):
        self.b.reserve(req(), [candidate()])
        self.b.reserve(req('c2'), [candidate()])
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')
        with self.assertRaisesRegex(Refused, 'cost_variance'):
            self.b.settle('c1', 11)
        reopened = Broker(self.path, self.caps)
        with self.assertRaisesRegex(Refused, 'unresolved_cost_variance'):
            reopened.dispatch_once('c2', dispatch_day='2026-11-06')
        self.assertEqual(reopened.status('c2')[-1], 'reserved')
        self.assertEqual(reopened.entries('c2'), (('reservation', 10, 'operation'),))

    def test_tightened_caps_refuse_dispatch_without_releasing_reservation(self):
        self.b.reserve(req(), [candidate()])
        for cap in (Caps(9,99,99,99), Caps(99,9,99,99),
                    Caps(99,99,9,99), Caps(99,99,99,9)):
            reopened = Broker(self.path, cap)
            with self.assertRaisesRegex(Refused, 'budget_ceiling'):
                reopened.dispatch_once('c1', dispatch_day='2026-11-06')
            self.assertEqual(reopened.status('c1')[-1], 'reserved')
        self.b.dispatch_once('c1', dispatch_day='2026-11-06')

    def test_dispatch_once_process_race_has_single_winner(self):
        self.b.reserve(req(), [candidate()])
        code = '''import sys
from model_broker.broker import Broker,Caps,Refused
try:
    Broker(sys.argv[1], Caps(50,20,30,60)).dispatch_once('c1', dispatch_day='2026-11-06')
except Refused:
    print('refused')
else:
    print('dispatched')
'''
        children = [subprocess.Popen([sys.executable, '-c', code, str(self.path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(4)]
        results = [p.communicate(timeout=10) for p in children]
        self.assertTrue(all(p.returncode == 0 for p in children), results)
        self.assertCountEqual([out.strip() for out, err in results],
                              ['dispatched', 'refused', 'refused', 'refused'])


if __name__ == "__main__": unittest.main()



