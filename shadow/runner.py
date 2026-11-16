"""The shadow service loop: poll Telegram, run turns, tick the Morning Brief.

Everything effectful is injected (``poll``, the front door, the brief), so the
loop itself holds no credentials and no transport. Rules:

* on start, replay events accepted before a crash (``frontend.recover``);
* one failing step never stops the loop: it is counted, logged by code only
  (never message text), and the loop backs off before the next poll;
* the loop ends only when ``stop()`` says so.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

MAX_BACKOFF_S = 60.0


class Runner:
    def __init__(self, *, frontend, brief=None, poll: Callable[[], list],
                 now: Callable[[], datetime], sleep: Callable[[float], None],
                 stop: Callable[[], bool], log: Callable[[str], None] = lambda s: None,
                 idle_s: float = 1.0):
        self._fe, self._brief, self._poll = frontend, brief, poll
        self._now, self._sleep, self._stop, self._log = now, sleep, stop, log
        self._idle = idle_s
        self._fails = 0

    def run(self) -> None:
        self._step("recover", self._fe.recover)
        while not self._stop():
            ok = self._once()
            if ok:
                self._fails = 0
                self._sleep(self._idle)
            else:
                self._fails += 1
                self._sleep(min(MAX_BACKOFF_S, self._idle * (2 ** min(self._fails, 6))))

    def _once(self) -> bool:
        ok = True
        try:
            updates = list(self._poll())
        except Exception as exc:
            self._log("poll_failed:" + type(exc).__name__)
            updates, ok = [], False
        for u in updates:
            if self._stop():
                break
            self._step("update", lambda u=u: self._fe.handle_update(u))
        if self._brief is not None:
            self._step("brief", lambda: self._brief.tick(self._now()))
        return ok

    def _step(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self._log(f"{name}_failed:{type(exc).__name__}")
