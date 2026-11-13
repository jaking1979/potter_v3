"""Build an E2 database with LEGACY E2 code (tests/legacy/<dir>), for the
upgrade tests. Run as a subprocess so the legacy ``responsibilities`` package
is the one imported:

    python3 legacy_fixture.py <legacy dir> <scenario> <tmp> [<then-dir> ...]

Each extra ``<then-dir>`` reopens the same database with that legacy version
afterwards (e.g. e2_467b2b9 then e2_e6a7af6: the reproduced v0 -> v1 path).
Prints JSON describing what was created. Offline: fakes only.

Scenarios (bill.watch: a lookup step, then a pay step that needs approval):
  pending          waiting, one pending request
  approved         waiting, its one request approved (not yet polled)
  approved_queued  approved and polled: queued to resume
  terminal         waiting, its one request denied (not yet polled)
  none             waiting, no request (worker died between parking and issuing)
  multiple         waiting, two pending requests for the same action (the old race)
  multiple_queued  as multiple, then the newer one approved and polled: queued
  denied_polled    denied and polled: FAILED under the old code (alert sent then)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _use(legacy: str) -> None:
    for name in [m for m in sys.modules if m == "responsibilities" or
                 m.startswith("responsibilities.") or m == "resp_world"]:
        del sys.modules[name]
    sys.path[:] = [str(HERE / "legacy" / legacy), str(HERE)] + [
        p for p in sys.path if p not in (str(HERE / "legacy" / legacy), str(HERE))]
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))      # after the legacy dir: it must win


def build(legacy: str, scenario: str, tmp: str) -> dict:
    _use(legacy)
    import resp_world as rw
    import responsibilities
    assert Path(responsibilities.__file__).resolve().is_relative_to(HERE / "legacy" / legacy)
    w = rw.World(tmp)
    w.runner.on_event("bill.due", {"id": f"legacy-{scenario}", "amount": 42})
    out = {"legacy": legacy, "scenario": scenario}

    if scenario == "none":
        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()
        w.runner._issue = boom
        try:
            w.runner.run_once()
        except Crash:
            pass
        task = w.runner.tasks.by_trigger(f'resp:bill.watch:event:{json.dumps([["id", "legacy-none"]], separators=(",", ":"))}')
        out["task_id"] = task.id
        return out

    r = w.runner.run_once()
    assert r.outcome == "waiting_approval", r
    out["task_id"], out["approval_id"] = r.task_id, r.detail
    if scenario == "approved":
        assert w.say(f"/approve {r.detail}").outcome == "approved"
    elif scenario == "approved_queued":
        assert w.say(f"/approve {r.detail}").outcome == "approved"
        w.runner.poll_approvals()
    elif scenario == "terminal":
        assert w.say(f"/deny {r.detail}").outcome == "denied"
    elif scenario == "denied_polled":
        assert w.say(f"/deny {r.detail}").outcome == "denied"
        w.runner.poll_approvals()
    elif scenario in ("multiple", "multiple_queued"):
        task = w.runner.tasks.get(r.task_id)
        action = w.runner._waiting_step(task)[2]
        w.now += 1
        with w.db.tx() as c:
            out["second_approval_id"] = w.issuer.request(c, action, summary="duplicate")
        if scenario == "multiple_queued":
            assert w.say(f"/approve {out['second_approval_id']}").outcome == "approved"
            w.runner.poll_approvals()
    elif scenario != "pending":
        raise SystemExit(f"unknown scenario {scenario}")
    return out


def reopen(legacy: str, tmp: str) -> None:
    _use(legacy)
    import resp_world as rw
    rw.World(tmp)                                   # runs that version's ensure()


if __name__ == "__main__":
    legacy, scenario, tmp, *then = sys.argv[1:]
    info = build(legacy, scenario, tmp)
    for later in then:
        reopen(later, tmp)
    print(json.dumps(info))
