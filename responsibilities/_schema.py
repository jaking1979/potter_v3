"""E2 tables, created in the shared C2/C3/E1 SQLite file.

* ``responsibility_state``     - key/value: the schedule watermark per
                                 responsibility (latest slot submitted).
* ``responsibility_approvals`` - the one E1 approval request issued for each
                                 (task, action hash). Written in the same
                                 transaction as ``ApprovalIssuer.request``; the
                                 primary key forbids a second one.
* ``responsibility_holds``     - tasks whose approval state could not be
                                 reconciled unambiguously during an upgrade.
                                 A held task is never prompted again and never
                                 resumed from a guessed approval; an operator
                                 resolves it (``ResponsibilityRunner.resolve_hold``).
* ``responsibility_finals``    - completed tasks whose long-work "done" update
                                 and final message are still owed. Inserted in
                                 the SAME transaction as C2's terminal commit,
                                 delivered after it, re-delivered (idempotently)
                                 by the recovery sweep until marked delivered.
* ``responsibility_progress``  - per task: when its first T16 state was
                                 reported (the long-work clock, so the 30 s ack
                                 survives a restart). ``history`` is unused
                                 (kept for layout compatibility): what was told
                                 lives only in E1's durable outbox.

``meta.responsibilities_schema`` pins the layout; any other value fails closed.

Upgrades (``e2.v0`` / ``e2.v1`` / ``e2.v2`` -> ``e2.v3``) run inside ONE transaction with
the version bump, so an interrupted upgrade leaves the old version and no
partial claims. Before ``e2.v1`` there were no claims, and ``e2.v1`` itself was
reached from ``e2.v0`` with an empty migration, so both reconcile existing E1
requests (``_reconcile_legacy``). Every upgrade also records tasks that had
already FAILED or ESCALATED as delivered (``_baseline_terminal_finals``), since
``e2.v3`` recovers terminal-failure finals from C2's state. Old E2 code refuses to open the upgraded file
(its own version check), but a process already running old code cannot be
fenced from here: stop every old runner before upgrading.
"""
from __future__ import annotations

import json
import re
import sqlite3

from runtime import Database, JournalUnavailable

SCHEMA_VERSION = "e2.v3"
UPGRADABLE = ("e2.v0", "e2.v1", "e2.v2")
RECONCILE_FROM = ("e2.v0", "e2.v1")
_HASH = re.compile(r"^[0-9a-f]{64}$")
CLAIMABLE = ("pending", "approved")
LIVE_TASK_STATES = ("queued", "running", "waiting_approval")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS responsibility_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    # One E1 approval request per (task, action), ever: the find-or-create is
    # decided inside the same BEGIN IMMEDIATE transaction as the E1 insert,
    # and this primary key makes a second claim for the same action impossible.
    """CREATE TABLE IF NOT EXISTS responsibility_approvals (
        task_id TEXT NOT NULL,
        action_hash TEXT NOT NULL,
        approval_id TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        PRIMARY KEY (task_id, action_hash))""",
    """CREATE TABLE IF NOT EXISTS responsibility_holds (
        task_id TEXT PRIMARY KEY,
        step_id TEXT NOT NULL,
        action_ref TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS responsibility_finals (
        task_id TEXT PRIMARY KEY,
        responsibility TEXT NOT NULL,
        outcome TEXT NOT NULL,
        created_at REAL NOT NULL,
        delivered_at REAL)""",
    """CREATE TABLE IF NOT EXISTS responsibility_progress (
        task_id TEXT PRIMARY KEY,
        started_at REAL NOT NULL,
        history TEXT NOT NULL)""",
)


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone() is not None


def _reconcile_legacy(c: sqlite3.Connection, now: float) -> None:
    """Inside the upgrade transaction: give every live task's approval step
    exactly the E1 request it already has, or hold it for an operator.

    For each (task, step, action reference) in a live task's checkpoint
    without a claim:
      * exactly one E1 request for (task, action hash), pending or approved
        -> claim it (no new prompt; an approval already given stays valid);
      * none, and the task is waiting on this very step -> nothing: no prompt
        was ever sent (E1 writes the request and its prompt in one
        transaction), so the first one is issued normally;
      * anything else - several requests, a single terminal one (denied,
        expired, voided, cancelled), none for a step that is not the waiting
        one, or a pre-hash reference (an approval id) -> HOLD. Never re-issue,
        never pick one: authority is not guessed from ambiguous records."""
    if not (_has_table(c, "tasks") and _has_table(c, "approval_requests")):
        return
    rows = c.execute("SELECT id, state, checkpoint FROM tasks WHERE state IN (?,?,?)"
                     " AND checkpoint IS NOT NULL", LIVE_TASK_STATES).fetchall()
    for task_id, state, raw in rows:
        try:
            cp = json.loads(raw) or {}
            approvals = dict(cp.get("approvals") or {})
        except (ValueError, TypeError, AttributeError):
            _hold(c, task_id, "?", str(raw)[:64], "unreadable checkpoint", now)
            continue
        for step_id, ref in sorted(approvals.items()):
            if not isinstance(ref, str) or not _HASH.match(ref):
                _hold(c, task_id, step_id, str(ref), "legacy approval reference", now)
                continue
            if c.execute("SELECT 1 FROM responsibility_approvals WHERE task_id=? AND"
                         " action_hash=?", (task_id, ref)).fetchone():
                continue
            reqs = c.execute("SELECT approval_id, status FROM approval_requests"
                             " WHERE task_id=? AND action_hash=? ORDER BY rowid",
                             (task_id, ref)).fetchall()
            if len(reqs) == 1 and reqs[0][1] in CLAIMABLE:
                c.execute("INSERT INTO responsibility_approvals (task_id, action_hash,"
                          " approval_id, created_at) VALUES (?,?,?,?)",
                          (task_id, ref, reqs[0][0], now))
            elif not reqs and state == "waiting_approval" and cp.get("waiting") == step_id:
                continue
            else:
                why = (f"{len(reqs)} legacy requests" if len(reqs) != 1
                       else f"legacy request is {reqs[0][1]}")
                _hold(c, task_id, step_id, ref, why, now)


def _baseline_terminal_finals(c: sqlite3.Connection, now: float) -> None:
    """e2.v3 makes terminal FAILED/ESCALATED finals durable (recovered from
    C2's terminal state). Tasks that already ended under older code were
    handled best-effort then; record them as delivered, so an upgrade does not
    re-announce history."""
    if not _has_table(c, "tasks"):
        return
    c.execute("INSERT OR IGNORE INTO responsibility_finals (task_id, responsibility, outcome,"
              " created_at, delivered_at) SELECT id, kind, state, updated_at, ? FROM tasks"
              " WHERE state IN ('failed','escalated')", (now,))


def _hold(c: sqlite3.Connection, task_id: str, step_id: str, ref: str, reason: str,
          now: float) -> None:
    c.execute("INSERT OR IGNORE INTO responsibility_holds (task_id, step_id, action_ref,"
              " reason, created_at) VALUES (?,?,?,?,?)", (task_id, step_id, ref, reason, now))


def ensure(db: Database, *, now: float = 0.0) -> None:
    try:
        with db.tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt)
            c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES"
                      " ('responsibilities_schema', ?)", (SCHEMA_VERSION,))
            got = c.execute("SELECT value FROM meta WHERE key='responsibilities_schema'"
                            ).fetchone()[0]
            if got in UPGRADABLE:
                if got in RECONCILE_FROM:
                    _reconcile_legacy(c, now)
                _baseline_terminal_finals(c, now)
                c.execute("UPDATE meta SET value=? WHERE key='responsibilities_schema'",
                          (SCHEMA_VERSION,))
                got = SCHEMA_VERSION
            if got != SCHEMA_VERSION:
                raise JournalUnavailable(f"responsibilities schema {got!r},"
                                         f" expected {SCHEMA_VERSION!r}")
    except sqlite3.Error as exc:
        raise JournalUnavailable(f"responsibility tables unusable: {exc}") from exc


def get_state(c: sqlite3.Connection, key: str) -> str | None:
    row = c.execute("SELECT value FROM responsibility_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO responsibility_state (key, value) VALUES (?,?)"
              " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def load_progress(c: sqlite3.Connection, task_id: str) -> tuple[float, list] | None:
    row = c.execute("SELECT started_at, history FROM responsibility_progress WHERE task_id=?",
                    (task_id,)).fetchone()
    return (row[0], json.loads(row[1])) if row else None


def save_progress(c: sqlite3.Connection, task_id: str, started_at: float, history: list) -> None:
    c.execute("INSERT INTO responsibility_progress (task_id, started_at, history) VALUES (?,?,?)"
              " ON CONFLICT(task_id) DO UPDATE SET history=excluded.history",
              (task_id, started_at, json.dumps(history, separators=(",", ":"))))
