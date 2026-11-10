"""D4/T3 synthetic-only process deadline experiment (T22: no adapters/effects).

This is lifecycle isolation, not an OS security sandbox. Each request spawns a
fresh worker. Only bounded JSON crosses its private socket; no pickle is received.
"""
from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import signal
import socket
import struct
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
        if (type(self.mode) is not str
                or self.mode not in {'normal', 'blocking', 'ignore_termination',
                              'exception', 'malformed', 'oversized',
                              'partial_header', 'partial_body', 'short_frame'}
                or type(self.delay_s) not in (int, float)
                or not math.isfinite(self.delay_s) or self.delay_s < 0
                or type(self.items) is not tuple or len(self.items) > MAX_ITEMS
                or any(type(p) is not tuple or len(p) != 2
                       or type(p[0]) is not str or not 0 < len(p[0]) <= MAX_ID_CHARS
                       or type(p[1]) is not str or len(p[1]) > MAX_TEXT_CHARS
                       for p in self.items)):
            raise ValueError('invalid synthetic process configuration')


def _validated_config(config):
    # A subclass/custom reduce hook is not a closed synthetic scenario. Rebuild
    # the plain frozen record before spawn; do not pickle arbitrary caller state.
    if type(config) is not SyntheticProcessBackend:
        raise ValueError('expected closed synthetic process configuration')
    return SyntheticProcessBackend(config.items, config.delay_s, config.mode)


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
        frame = struct.pack('!I', len(payload)) + payload
        if config.mode == 'partial_header':
            sender.sendall(frame[:2])
            time.sleep(10)
        elif config.mode == 'partial_body':
            sender.sendall(frame[:6])
            time.sleep(10)
        elif config.mode == 'short_frame':
            sender.sendall(frame[:2])
        else:
            sender.sendall(frame)
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
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or not 0 < timeout <= 30):
        raise ValueError('invalid isolated request')
    config = _validated_config(config)
    start = time.monotonic()
    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    wire = bytearray()
    payload_size = None
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
            try:
                # recv never waits for a complete frame. A partial header/body
                # must still yield to the event loop, deadline and cancellation.
                chunk = receiver.recv(MAX_WIRE_BYTES + 5 - len(wire))
            except BlockingIOError:
                chunk = None
            if chunk == b'':
                return 'unavailable', (), time.monotonic() - start
            if chunk:
                wire.extend(chunk)
                if len(wire) >= 4 and payload_size is None:
                    payload_size = struct.unpack('!I', wire[:4])[0]
                    if payload_size > MAX_WIRE_BYTES:
                        return 'unavailable', (), time.monotonic() - start
                if payload_size is not None and len(wire) >= payload_size + 4:
                    try:
                        if len(wire) != payload_size + 4:
                            raise ValueError('trailing frame data')
                        items = _decode(wire[4:])
                        elapsed = time.monotonic() - start
                        if elapsed >= timeout:
                            return 'deadline', (), elapsed
                        return 'complete', items, elapsed
                    except (ValueError, TypeError, UnicodeError):
                        return 'unavailable', (), time.monotonic() - start
            # A process can exit while complete or partial data remains buffered.
            # Drain until a complete frame or socket EOF instead of consulting
            # process liveness here.
            await asyncio.sleep(min(.005, timeout - elapsed))
    except Exception:
        return 'unavailable', (), time.monotonic() - start
    finally:
        sender.close()
        receiver.close()
        if started:
            # A resistant child can consume the full join grace. Do that work
            # off the event loop and finish reaping even on repeated cancellation.
            cleanup = asyncio.create_task(asyncio.to_thread(_stop, process))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()  # Reaping failure is an intentional hard alarm.
            if cancelled:
                raise asyncio.CancelledError
        else:
            process.close()

