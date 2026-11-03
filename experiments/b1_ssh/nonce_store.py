"""Scratch-only SQLite experiment; not a production T39 receipt store.

Caller owns the database location, connection and lifecycle. This module does
not authenticate receipt facts, guarantee filesystem durability, fence hosts,
handle revocation, or provide an authorized release/merge path.
"""
import re
import sqlite3

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def prepare(connection):
    """Create experimental schema in a caller-controlled scratch database."""
    connection.execute('CREATE TABLE IF NOT EXISTS used_nonces '
                       '(nonce TEXT PRIMARY KEY, commit_id TEXT NOT NULL, '
                       'tree_id TEXT NOT NULL, receipt_hash TEXT NOT NULL)')
    connection.commit()


def consume_pair(connection, nonces, binding):
    """Atomically record both nonces; reject invalid or previously used inputs."""
    if (not isinstance(nonces, tuple) or len(nonces) != 2
            or any(not isinstance(x, str) or not _HEX64.fullmatch(x) for x in nonces)
            or nonces[0] == nonces[1]):
        raise ValueError('two distinct nonce hashes required')
    if (not isinstance(binding, dict) or set(binding) != {'commit', 'tree', 'receipt_hash'}
            or any(not isinstance(binding[k], str) or not _HEX40.fullmatch(binding[k])
                   for k in ('commit', 'tree'))
            or not isinstance(binding['receipt_hash'], str)
            or not _HEX64.fullmatch(binding['receipt_hash'])):
        raise ValueError('candidate and receipt binding required')
    if connection.in_transaction:
        raise ValueError('caller transaction must be closed')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.executemany('INSERT INTO used_nonces VALUES (?, ?, ?, ?)',
                               [(nonce, binding['commit'], binding['tree'], binding['receipt_hash'])
                                for nonce in nonces])
        connection.commit()
        return True
    except sqlite3.IntegrityError:
        connection.rollback()
        return False
    except BaseException:
        connection.rollback()
        raise
