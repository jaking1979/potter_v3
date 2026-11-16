"""Append-only JSONL cost log (the T40 first step). No prompt or reply text."""
from __future__ import annotations

import json
import os

_STR = ("ts", "call_id", "task_id", "model", "provider", "state")
_INT = ("reserved_usd_micros",)
_OPT_INT = ("input_tokens", "output_tokens", "actual_usd_micros")
STATES = frozenset({"settled", "overrun", "unknown", "refused"})


class CostLog:
    def __init__(self, path):
        self._path = os.fspath(path)

    def append(self, record: dict) -> None:
        if type(record) is not dict or set(record) != set(_STR + _INT + _OPT_INT):
            raise ValueError("cost record has wrong fields")
        for k in _STR:
            if type(record[k]) is not str or not record[k] or len(record[k]) > 200:
                raise ValueError(f"bad {k}")
        for k in _INT:
            if type(record[k]) is not int or record[k] < 0:
                raise ValueError(f"bad {k}")
        for k in _OPT_INT:
            if record[k] is not None and (type(record[k]) is not int or record[k] < 0):
                raise ValueError(f"bad {k}")
        if record["state"] not in STATES:
            raise ValueError("bad state")
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self) -> list[dict]:
        try:
            with open(self._path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []
