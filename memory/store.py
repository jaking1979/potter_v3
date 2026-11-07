"""Offline-only T15/T27 memory staging prototype.

The injectable verifier is a *test seam*, not evidence of authentication.
No network, filesystem persistence, Notion writes, effect journal or policy
integration is implemented. Never use this prototype for production memory.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
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


class MemoryStore:
    """A serialized in-memory model of staged and promoted items.

    `verify` must authenticate an operation and its *exact* payload in the
    future real integration. This prototype accepts only a test callback.
    SQLite changes, compare-and-set checks and audit share a transaction.
    """
    def __init__(self, verify: Callable[[str, object, dict], bool]):
        if not callable(verify):
            raise Invalid("verifier required")
        self._verify = verify
        self._lock = threading.RLock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript('''
          CREATE TABLE item (id TEXT PRIMARY KEY, key TEXT NOT NULL,
            value TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL,
            sensitivity TEXT NOT NULL, confidence TEXT NOT NULL,
            source_ref TEXT NOT NULL, version INTEGER NOT NULL,
            supersedes TEXT, contradicts TEXT);
          CREATE UNIQUE INDEX one_active_key ON item(key) WHERE status='active';
          CREATE TABLE request (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                                item_id TEXT NOT NULL);
          CREATE TABLE audit (seq INTEGER PRIMARY KEY, op TEXT NOT NULL,
                              item_id TEXT NOT NULL, version INTEGER NOT NULL);
        ''')

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
        with self._lock, self._db:
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

    def promote(self, ident: str, expected_version: int, proof: object, *, batch: bool = False) -> Item:
        payload = dict(id=ident, expected_version=expected_version, batch=batch)
        self._auth('promote', proof, payload)
        with self._lock, self._db:
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
        self._auth('undo', proof, dict(id=ident, expected_version=expected_version))
        with self._lock, self._db:
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
        self._auth('forget', proof, dict(id=ident, expected_version=expected_version))
        with self._lock, self._db:
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
        with self._lock:
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
