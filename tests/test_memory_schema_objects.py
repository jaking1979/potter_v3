import unittest
import sqlite3
import tempfile
from pathlib import Path
from memory import MemoryStore, Invalid

class SchemaObjectTests(unittest.TestCase):
    def test_forget_refuses_trigger_before_it_copies_content(self):
        store = MemoryStore(lambda op, proof, payload: True)
        item = store.stage(request_id='r', key='synthetic-key', value='synthetic-secret', origin='external', source_ref='synthetic')
        store._db.execute("CREATE TRIGGER capture BEFORE UPDATE OF status ON item WHEN NEW.status='forgotten' BEGIN INSERT INTO audit(op,item_id,version) VALUES('captured',OLD.value,OLD.version); END")
        with self.assertRaises(Invalid):
            store.forget(item.id, item.version, 'proof')
        self.assertEqual(store._db.execute("SELECT count(*) FROM audit WHERE item_id='synthetic-secret'").fetchone()[0], 0)

    def test_view_and_existing_store_refused_on_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'memory.db'
            store = MemoryStore.bootstrap_offline(path, lambda *a: True, environment='synthetic')
            store.close()
            with sqlite3.connect(path) as db:
                db.execute('CREATE VIEW copied AS SELECT value FROM item')
            with self.assertRaises(Invalid):
                MemoryStore.open_offline(path, lambda *a: True, environment='synthetic')

if __name__ == '__main__': unittest.main()
