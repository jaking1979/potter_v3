"""D4/T3 synthetic-only process deadline experiment (T22: no adapters/effects).

This is lifecycle isolation, not an OS security sandbox. Each request spawns a
fresh worker. Only bounded JSON crosses its private pipe; no pickle is received.
"""
from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import signal
import time
from dataclasses import dataclass

MAX_WIRE_BYTES = 16_384
MAX_ITEMS = 3
MAX_ID_CHARS = 256
MAX_TEXT_CHARS = 2_048
STOP_GRACE_S = .05
KILL_GRACE_S = .25


@dataclass(frozen=True)
class SyntheticProcessBackend:
    """Closed synthetic scenarios; never supply a connector or callable.

    items are plain (id, text) pairs, not executable worker/adaptor objects.
    Fault modes exist solely to exercise parent cleanup/fail-closed behavior.
    """
    items: tuple[tuple[str, str], ...] = ()
    delay_s: float = 0
    mode: str = 'normal'

    def __post_init__(self):
        if (self.mode not in {'normal', 'blocking', 'ignore_termination',
                              'exception', 'malformed', 'oversized'}
                or not isinstance(self.delay_s, (int, float))
                or not math.isfinite(self.delay_s) or self.delay_s < 0
                or type(self.items) is not tuple or len(self.items) > MAX_ITEMS
                or any(type(p) is not tuple or len(p) != 2
                       or type(p[0]) is not str or not 0 < len(p[0]) <= MAX_ID_CHARS
                       or type(p[1]) is not str or len(p[1]) > MAX_TEXT_CHARS
                       for p in self.items)):
            raise ValueError('invalid synthetic process configuration')


def _worker(sender, receiver, config):
    receiver.close()
    try:
        if config.mode == 'ignore_termination':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Deliberately blocks the child's interpreter, not the parent's loop.
        time.sleep(config.delay_s)
        if config.mode == 'exception':
            raise RuntimeError('synthetic worker exception')
        if config.mode == 'malformed':
            payload = b'{"items":[["bad",42]]}'
        elif config.mode == 'oversized':
            payload = b'x' * (MAX_WIRE_BYTES + 1)
        else:
            payload = json.dumps({'items': config.items}, ensure_ascii=True).encode()
        sender.send_bytes(payload)
    except Exception:
        # Exception text can contain backend data; expose no details.
        pass
    finally:
        sender.close()


def _decode(payload):
    from retrieval.core import Evidence
    value = json.loads(payload)
    if type(value) is not dict or set(value) != {'items'}:
        raise ValueError('invalid result envelope')
    rows = value['items']
    if type(rows) is not list or len(rows) > MAX_ITEMS:
        raise ValueError('invalid result count')
    items = []
    for row in rows:
        if (type(row) is not list or len(row) != 2
                or type(row[0]) is not str or not 0 < len(row[0]) <= MAX_ID_CHARS
                or type(row[1]) is not str or len(row[1]) > MAX_TEXT_CHARS):
            raise ValueError('invalid evidence')
        items.append(Evidence(*row))
    return tuple(items)


def _stop(process):
    if process.is_alive():
        process.terminate()
        process.join(STOP_GRACE_S)
    if process.is_alive():
        process.kill()
    # Reap before returning so no late worker survives into another turn.
    process.join(KILL_GRACE_S)
    if process.is_alive():
        raise RuntimeError('synthetic worker could not be reaped')
    process.close()


async def bounded_search(config, query, timeout):
    start = time.monotonic()
    status, items, _ = await _search(config, query, timeout)
    elapsed = time.monotonic() - start
    if status == 'complete' and elapsed >= timeout:
        return 'deadline', (), elapsed
    return status, items, elapsed


async def _search(config, query, timeout):
    if (type(query) is not str or len(query) > 4_096
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 30):
        raise ValueError('invalid isolated request')
    start = time.monotonic()
    receiver, sender = multiprocessing.get_context('spawn').Pipe(duplex=False)
    process = multiprocessing.get_context('spawn').Process(
        target=_worker, args=(sender, receiver, config), daemon=True)
    started = False
    try:
        process.start()
        started = True
        sender.close()
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                return 'deadline', (), elapsed
            if receiver.poll():
                try:
                    payload = receiver.recv_bytes(MAX_WIRE_BYTES)
                    items = _decode(payload)
                    # Decode/startup are also charged to the request deadline.
                    elapsed = time.monotonic() - start
                    if elapsed >= timeout:
                        return 'deadline', (), elapsed
                    return 'complete', items, elapsed
                except (EOFError, OSError, ValueError, TypeError, UnicodeError):
                    return 'unavailable', (), time.monotonic() - start
            if not process.is_alive():
                return 'unavailable', (), time.monotonic() - start
            await asyncio.sleep(min(.005, timeout - elapsed))
    except Exception:
        return 'unavailable', (), time.monotonic() - start
    finally:
        sender.close()
        receiver.close()
        if started:
            _stop(process)
        else:
            process.close()
