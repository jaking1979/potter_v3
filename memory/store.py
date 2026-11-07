"""Offline-only T15/T27 memory staging prototype.

The injectable verifier is a *test seam*, not evidence of authentication.
No network, Notion writes, effect journal or policy integration is implemented.
Explicit local synthetic stores support offline restart/concurrency experiments. Never use this prototype for production memory.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, asdict
from typing import Callable


class Denied(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


@dataclass(frozen=True)
class Item:
    id: str
    key: str
    value: str
    origin: str
    status: str
    sensitivity: str
    confidence: str
    source_ref: str
    version: int
    supersedes: str | None
    contradicts: str | None


@dataclass(frozen=True)
class Retrieval:
    active: tuple[Item, ...]
    provisional: tuple[Item, ...]
    excluded_ids: tuple[str, ...]


_ACTIVE_INDEX = "CREATE UNIQUE INDEX one_active_key ON item(key) WHERE status='active'"
_COLUMNS = {
    'item': ('id','key','value','origin','status','sensitivity','confidence','source_ref','version','supersedes','contradicts'),
    'request': ('request_id','payload','item_id'),
    'audit': ('seq','op','item_id','version'),
    'metadata': ('schema','environment'),
}
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE item (id TEXT PRIMARY KEY, key TEXT NOT NULL,
 value TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL,
 sensitivity TEXT NOT NULL, confidence TEXT NOT NULL,
 source_ref TEXT NOT NULL, version INTEGER NOT NULL,
 supersedes TEXT, contradicts TEXT);
CREATE UNIQUE INDEX one_active_key ON item(key) WHERE status='active';
CREATE TABLE request (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, item_id TEXT NOT NULL);
CREATE TABLE audit (seq INTEGER PRIMARY KEY, op TEXT NOT NULL, item_id TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE metadata (schema TEXT NOT NULL, environment TEXT NOT NULL);
INSERT INTO metadata VALUES ('potter.memory.offline/v1','synthetic');
PRAGMA user_version=1;
COMMIT;
"""


class MemoryStore:
    """An offline model of staged and promoted items; memory-only by default.

    `verify` must authenticate an operation and its *exact* payload in the
    future real integration. This prototype accepts only a test callback.
    SQLite changes, compare-and-set checks and audit share a transaction.
    """
    def __init__(self, verify: Callable[[str, object, dict], bool]):
        self._initialize(verify, ":memory:", create=True)

    @classmethod
    def bootstrap_offline(cls, path, verify, *, environment):
        """Explicit new synthetic store; never overwrite or enable production."""
        if environment != 'synthetic':
            raise Invalid('synthetic environment required')
        path = Path(path).absolute()
        with path.open('xb'):
            pass
        result = cls.__new__(cls)
        result._initialize(verify, path.as_uri() + '?mode=rw', create=True, uri=True)
        return result

    @classmethod
    def open_offline(cls, path, verify, *, environment):
        """Missing/unknown stores fail closed; never create a replacement."""
        if environment != 'synthetic':
            raise Invalid('synthetic environment required')
        path = Path(path).absolute()
        if not path.is_file() or path.is_symlink():
            raise Invalid('existing regular store required')
        result = cls.__new__(cls)
        result._initialize(verify, path.as_uri() + '?mode=rw', create=False, uri=True)
        return result

    def _initialize(self, verify, database, *, create, uri=False):
        if not callable(verify):
            raise Invalid("verifier required")
        self._verify = verify
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(database, uri=uri, timeout=10,
                                   check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA trusted_schema=OFF')
            self._db.execute('PRAGMA secure_delete=ON')
            if create:
                # Exclusive bootstrap owns an empty file, never a migration.
                self._db.executescript(_SCHEMA)
            with self._transaction(write=False):
                if self._db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Invalid('store integrity')
        except Exception:
            self._db.close()
            self._closed = True
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def _check_schema(self):
        if self._db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise Invalid('unknown store version')
        tables = {r[0] for r in self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {'item', 'request', 'audit', 'metadata'}:
            raise Invalid('unknown store tables')
        meta = self._db.execute('SELECT schema,environment FROM metadata').fetchall()
        if [tuple(r) for r in meta] != [('potter.memory.offline/v1', 'synthetic')]:
            raise Invalid('unknown store metadata')
        for table, columns in _COLUMNS.items():
            actual = tuple(row[1] for row in self._db.execute('PRAGMA table_info(' + table + ')'))
            if actual != columns:
                raise Invalid('unknown store columns')
        # The partial unique index is essential to the one-active-value invariant.
        row = self._db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='one_active_key'").fetchone()
        if not row or row[0] != _ACTIVE_INDEX:
            raise Invalid('active-key invariant unavailable')

    @contextmanager
    def _transaction(self, *, write=True):
        # BEGIN IMMEDIATE takes the cross-process write reservation before reads,
        # so compare-and-set, request dedupe, mutation and audit share a snapshot.
        with self._lock:
            if self._closed:
                raise Invalid('store closed')
            self._db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                self._check_schema()
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _auth(self, operation: str, proof: object, payload: dict) -> None:
        try:
            accepted = self._verify(operation, proof, dict(payload)) is True
        except Exception:
            accepted = False
        if not accepted:
            raise Denied(operation)

    @staticmethod
    def _text(value: str, field: str, limit: int = 2048) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise Invalid(field)
        return value

    def stage(self, *, request_id: str, key: str, value: str, origin: str,
              source_ref: str, sensitivity: str = 'normal', confidence: str = 'unknown',
              direct_proof: object = None) -> Item:
        for name, v in [('request_id', request_id), ('key', key), ('value', value),
                        ('source_ref', source_ref)]:
            self._text(v, name)
        if origin not in {'direct', 'inferred', 'external', 'imported'}:
            raise Invalid('origin')
        if sensitivity not in {'normal', 'sensitive'}:
            raise Invalid('sensitivity')
        if confidence not in {'unknown', 'low', 'medium', 'high'}:
            raise Invalid('confidence')
        payload = dict(key=key, value=value, origin=origin, source_ref=source_ref,
                       sensitivity=sensitivity, confidence=confidence)
        # A caller cannot self-assert a direct statement. The fake verifier
        # must bind provenance to the content; all other sources remain staged.
        if origin == 'direct':
            self._auth('direct_statement', direct_proof, payload)
        packed = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=False).encode('utf-8')).hexdigest()
        with self._transaction():
            old = self._db.execute('SELECT * FROM request WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['payload'] != packed:
                    raise Conflict('idempotency key reused with different payload')
                return self._get(old['item_id'])
            active = self._db.execute("SELECT id,value FROM item WHERE key=? AND status='active'", (key,)).fetchone()
            contradicts = active['id'] if active and active['value'] != value else None
            ident = str(uuid.uuid4())
            self._db.execute('INSERT INTO item VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                             (ident, key, value, origin, 'staged', sensitivity,
                              confidence, source_ref, 1, None, contradicts))
            self._db.execute('INSERT INTO request VALUES (?,?,?)', (request_id, packed, ident))
            self._audit('stage', ident, 1)
            return self._get(ident)

    @staticmethod
    def _review_args(ident, expected_version, operation, batch):
        if (not isinstance(ident, str) or not ident
                or type(expected_version) is not int or expected_version < 1
                or operation not in ('promote', 'undo', 'forget')
                or type(batch) is not bool or (operation != 'promote' and batch)):
            raise Invalid('invalid review binding')

    def _review_snapshot(self, ident, expected_version, operation, batch):
        self._review_args(ident, expected_version, operation, batch)
        item = self._get(ident)
        if item.version != expected_version:
            raise Conflict('stale review version')
        # Bind all rows the operation could change/restore. A newly staged
        # same-key row invalidates a prior forget proposal, rather than silently
        # enlarging the approved deletion set. No external views are covered.
        if operation == 'forget':
            rows = self._db.execute("SELECT * FROM item WHERE key=? AND status!='forgotten' ORDER BY id", (item.key,)).fetchall()
        elif operation == 'promote':
            rows = self._db.execute("SELECT * FROM item WHERE key=? AND status='active' ORDER BY id", (item.key,)).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM item WHERE id=?", (item.supersedes,)).fetchall()
        snapshot = dict(schema='potter.memory.review-offline/v0', operation=operation,
                        expected_version=expected_version, batch=batch,
                        item=asdict(item), affected=[dict(r) for r in rows])
        packed = json.dumps(snapshot, sort_keys=True, separators=(',', ':'),
                            ensure_ascii=False)
        return dict(snapshot=snapshot, digest=hashlib.sha256(packed.encode('utf-8')).hexdigest())

    def review_snapshot(self, ident, expected_version, *, operation, batch=False):
        """Detached exact local proposal; contains content, never log as a trace.

        The future issuer/verifier must bind operation + digest. A test callback
        that ignores that binding still supplies no authentication guarantee.
        """
        with self._transaction(write=False):
            return self._review_snapshot(ident, expected_version, operation, batch)

    def _authorize_snapshot(self, operation, ident, expected_version, proof, batch=False):
        proposal = self._review_snapshot(ident, expected_version, operation, batch)
        payload = dict(id=ident, expected_version=expected_version,
                       snapshot_digest=proposal['digest'], batch=batch)
        self._auth(operation, proof, payload)

    def promote(self, ident: str, expected_version: int, proof: object, *, batch: bool = False) -> Item:
        self._review_args(ident, expected_version, 'promote', batch)
        with self._transaction():
            self._authorize_snapshot('promote', ident, expected_version, proof, batch)
            item = self._get(ident)
            if item.status != 'staged' or item.version != expected_version:
                raise Conflict('stale or non-staged candidate')
            if batch and (item.sensitivity == 'sensitive' or item.origin != 'direct'):
                raise Denied('batch promotion limited to nonsensitive direct statements')
            active = self._db.execute("SELECT * FROM item WHERE key=? AND status='active'", (item.key,)).fetchone()
            # The active fact may have changed since the staged contradiction
            # was created. Never silently overwrite an unseen newer fact.
            if active and item.contradicts != active['id']:
                raise Conflict('active version changed since staging')
            if active:
                self._db.execute("UPDATE item SET status='superseded',version=version+1 WHERE id=?", (active['id'],))
                self._audit('supersede', active['id'], active['version'] + 1)
            self._db.execute("UPDATE item SET status='active',version=version+1,supersedes=? WHERE id=?",
                             (active['id'] if active else None, ident))
            self._audit('promote', ident, item.version + 1)
            return self._get(ident)

    def undo(self, ident: str, expected_version: int, proof: object) -> Item:
        self._review_args(ident, expected_version, 'undo', False)
        with self._transaction():
            self._authorize_snapshot('undo', ident, expected_version, proof)
            item = self._get(ident)
            if item.status != 'active' or item.version != expected_version or not item.supersedes:
                raise Conflict('cannot undo changed or root memory')
            prior = self._get(item.supersedes)
            if prior.status != 'superseded':
                raise Conflict('prior item changed')
            self._db.execute("UPDATE item SET status='undone',version=version+1 WHERE id=?", (ident,))
            self._db.execute("UPDATE item SET status='active',version=version+1 WHERE id=?", (prior.id,))
            self._audit('undo', ident, item.version + 1)
            self._audit('restore', prior.id, prior.version + 1)
            return self._get(prior.id)

    def forget(self, ident: str, expected_version: int, proof: object) -> None:
        self._review_args(ident, expected_version, 'forget', False)
        with self._transaction():
            self._authorize_snapshot('forget', ident, expected_version, proof)
            item = self._get(ident)
            if item.version != expected_version or item.status == 'forgotten':
                raise Conflict('stale item')
            # Erase every local revision and staged claim under this fact key,
            # not just the selected current row. External views and external
            # backups remain outside this isolated experiment.
            rows = self._db.execute("SELECT id,version FROM item WHERE key=? AND status!='forgotten'", (item.key,)).fetchall()
            self._db.execute("UPDATE item SET status='forgotten', value='', version=version+1 WHERE key=? AND status!='forgotten'", (item.key,))
            for row in rows:
                self._audit('forget', row['id'], row['version'] + 1)

    def retrieve(self, key: str, *, consequential: bool) -> Retrieval:
        if type(consequential) is not bool:
            raise Invalid('explicit task tier required')
        with self._transaction(write=False):
            rows = self._db.execute("SELECT * FROM item WHERE key=? AND status IN ('active','staged') ORDER BY rowid", (key,)).fetchall()
            items = [Item(**dict(x)) for x in rows]
            active = tuple(x for x in items if x.status == 'active')
            staged = tuple(x for x in items if x.status == 'staged')
            # Never place contents of excluded staged claims in consequential
            # context. IDs are provided so a separate review surface can show them.
            excluded = tuple(x.id for x in staged if consequential and x.origin != 'direct')
            provisional = tuple(x for x in staged if not consequential or x.origin == 'direct')
            return Retrieval(active, provisional, excluded)

    def _get(self, ident: str) -> Item:
        row = self._db.execute('SELECT * FROM item WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise Invalid('unknown item')
        return Item(**dict(row))

    def _audit(self, op: str, ident: str, version: int) -> None:
        self._db.execute('INSERT INTO audit (op,item_id,version) VALUES (?,?,?)', (op, ident, version))
