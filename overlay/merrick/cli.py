"""Merrick's command line. Used by scheduled command jobs and by the Merrick agent.

  merrick ask "<request>"        route + fast path; prints JSON (path, intent, outcome, reply, and for a fallback
                                 the time budget and deadline)
  merrick budget-check --deadline EPOCH       exit 0 while time remains, exit 3 when the budget is used up
  merrick fallback-end --id ID --started EPOCH --outcome done|budget_hit|failed
  merrick report [--days 7] [--json]          path counts and the most common misses
  merrick cold-watch --to ID [--dry-run]      silent unless a threshold is crossed
  merrick collect                             hourly: room snapshot, thermal episodes, rate calibration
  merrick tick [--to ID]                      run due one-shot intentions (changes devices)
  merrick intend / intentions / cancel        register, list and cancel one-shot intentions

Settings (flags or environment): --house / MERRICK_HOUSE (house file), --state-dir / MERRICK_STATE_DIR,
--ha-cmd / MERRICK_HA_CMD (the Home Assistant bridge command; see habridge.py). No address or token is handled here.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import budget, coldwatch, departure, fastpath, intentions, pathlog, thermal
from .ha import SafeHome
from .habridge import BridgeHomeAssistant


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _save(path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)                                       # a crash never leaves a half-written file


@contextlib.contextmanager
def _locked(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _house(ns) -> dict:
    path = ns.house or os.environ.get("MERRICK_HOUSE") or str(_state(ns) / "house.json")
    house = _load(path, None)
    if not isinstance(house, dict) or not house.get("zones"):
        raise SystemExit(f"merrick: house file missing or empty: {path}")
    return house


def _state(ns) -> Path:
    d = ns.state_dir or os.environ.get("MERRICK_STATE_DIR")
    if not d:
        raise SystemExit("merrick: set --state-dir or MERRICK_STATE_DIR")
    return Path(d)


def _client(ns) -> BridgeHomeAssistant:
    cmd = ns.ha_cmd or os.environ.get("MERRICK_HA_CMD", "")
    if not cmd.strip():
        raise SystemExit("merrick: Home Assistant is not configured (set --ha-cmd or MERRICK_HA_CMD)")
    return BridgeHomeAssistant(cmd)


def _send(ns, text: str) -> bool:
    if ns.dry_run or not ns.to:
        print(text)
        return True
    r = subprocess.run(shlex.split(ns.openclaw) + ["message", "send", "--channel", ns.channel, "--target", ns.to,
                                                   "--message", text], capture_output=True, text=True)
    return r.returncode == 0


def _decider(cmd: str):
    """Wrap an external decision command (text on stdin, JSON on stdout) as the router's injected function."""
    def decide(text: str):
        r = subprocess.run(shlex.split(cmd), input=text, capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout) if r.returncode == 0 else None
    return decide


def cmd_ask(ns) -> int:
    state, house = _state(ns), _house(ns)
    home = SafeHome(_client(ns), house)
    out = fastpath.handle(ns.text, home, house, state / "path-log.jsonl",
                          decide=_decider(ns.decide_cmd or os.environ.get("MERRICK_DECIDE_CMD", "")) if (ns.decide_cmd or os.environ.get("MERRICK_DECIDE_CMD")) else None,
                          model=_load(state / "model.json", {}))
    if out.get("pending_departure"):
        with _locked(state):
            _save(state / "departure-pending.json", out.pop("pending_departure"))
    if out["path"] == "fallback":
        now = time.time()
        out.update({"budget_s": ns.budget, "started_epoch": int(now), "deadline_epoch": int(now + ns.budget),
                    "instruction": "Not a recognised request. Say in one line what you are about to check, work in "
                                   "small steps, run budget-check before each step, and when it exits 3 stop and "
                                   "report what is observed, estimated and unknown. Then run fallback-end."})
    print(json.dumps(out, indent=1))
    return 0


def cmd_confirm_departure(ns) -> int:
    """Run ONLY when the owner has explicitly confirmed. Locks and arms what the house file lists, with read-back."""
    state, house = _state(ns), _house(ns)
    with _locked(state):
        pending = _load(state / "departure-pending.json", None)
        status, text = departure.confirm(SafeHome(_client(ns), house), house, pending, time.time())
        if status != "stopped":                                # a used or refused confirmation cannot be replayed
            try:
                (state / "departure-pending.json").unlink()
            except OSError:
                pass
    pathlog.append(state / "path-log.jsonl", pathlog.record("confirm_departure", "fast", 0.0, status,
                                                            request="confirm departure", source="table"))
    print(json.dumps({"outcome": status, "reply": text}, indent=1))
    return 0 if status == "done" else 1


def cmd_budget_check(ns) -> int:
    left = ns.deadline - time.time()
    if left > 0:
        print(json.dumps({"remaining_s": int(left), "expired": False}))
        return 0
    print(json.dumps({"remaining_s": 0, "expired": True,
                      "instruction": "STOP. Reply now with what is observed, estimated and unknown. Do not continue."}))
    return 3


def cmd_fallback_end(ns) -> int:
    ms = max(0.0, time.time() - ns.started) * 1000.0
    ok = pathlog.append(_state(ns) / "path-log.jsonl", pathlog.fallback_end(ns.id, ms, ns.outcome))
    return 0 if ok else 1


def cmd_report(ns) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=ns.days)).strftime("%Y-%m-%dT%H:%M:%SZ") if ns.days else None
    rep = pathlog.report(pathlog.read(_state(ns) / "path-log.jsonl"), since=since)
    print(json.dumps(rep, indent=1) if ns.json else pathlog.render(rep))
    return 0


def cmd_cold_watch(ns) -> int:
    state, house = _state(ns), _house(ns)
    readings = fastpath.cold_readings(SafeHome(_client(ns), house), house)
    saved = _load(state / "cold-watch.json", {})
    findings = coldwatch.evaluate(readings, coldwatch.Thresholds.from_house(house), saved.get("previous"))
    report, seen = coldwatch.new_findings(findings, saved.get("seen") or {}, readings["ts"])
    if report and not _send(ns, coldwatch.render(report, readings)):
        return 1                                             # keep the old state so the next run tries again
    if not ns.dry_run:
        prev = {"ts": readings["ts"], "basement_f": readings["basement_f"]} \
            if readings["basement_f"] is not None else saved.get("previous")
        _save(state / "cold-watch.json", {"seen": seen, "previous": prev})
    unreadable = [f["text"] for f in findings if f["severity"] == "unreadable"]
    if unreadable:
        print("cold-watch: " + "; ".join(unreadable), file=sys.stderr)
        return 1                                             # the schedule watchdog reports a failed job
    return 0


def cmd_collect(ns) -> int:
    state, house = _state(ns), _house(ns)
    client = _client(ns)
    home = SafeHome(client, house)
    now = datetime.now(timezone.utc)
    temps = {name: home.value(s.get("entity", "")) for name, s in (house.get("sensors") or {}).items()}
    for zid, z in house["zones"].items():
        temps["zone:" + zid] = home.zone_reading(zid)["room_f"]
    with open(state / "room-snapshots.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": now.isoformat(), "temps": temps}) + "\n")
    failed = 0
    with _locked(state):
        model = _load(state / "model.json", {})
        zones = model.setdefault("zones", {})
        for zid, z in house["zones"].items():
            sensor = z.get("room_sensor")
            if not (sensor and z.get("climate_entity")):
                continue                                     # no authoritative room sensor: nothing to learn from
            zm = zones.setdefault(zid, {})
            since = zm.get("learned_until") or (now - timedelta(hours=24)).isoformat()
            try:
                eps = thermal.classify_episodes(client.history(z["climate_entity"], since),
                                                client.history(sensor, since), now)
            except Exception as e:                           # noqa: BLE001 - one zone failing must not stop the rest
                print(f"collect: {zid}: history unavailable ({type(e).__name__})", file=sys.stderr)
                failed += 1
                continue
            eps = [e for e in eps[:-1] if e["start_time"] >= since]     # the last stretch is still in progress
            if not eps:
                continue
            with open(state / "thermal-episodes.jsonl", "a") as fh:
                fh.writelines(json.dumps({"zone": zid, **e}) + "\n" for e in eps)
            heat, cool = thermal.calibration_rates(eps)
            for key, rates in (("heating", heat), ("cooling", cool)):
                cur = zm.setdefault(key, {})
                mean, n, conf = thermal.update_rate(cur.get("mean_f_per_hour"), int(cur.get("samples") or 0), rates)
                cur.update({"mean_f_per_hour": mean, "samples": n, "confidence": conf})
            zm["learned_until"] = eps[-1]["end_time"]
        model["updated"] = now.isoformat()
        _save(state / "model.json", model)
    return 1 if failed else 0


def cmd_tick(ns) -> int:
    state, house = _state(ns), _house(ns)
    with _locked(state):
        store = _load(state / "intentions.json", {"intentions": []})
        if not any(i.get("status") == "pending" for i in store.get("intentions", [])):
            return 0                                         # nothing pending: do not even contact Home Assistant
        now = intentions.utcnow()
        events = intentions.tick(store, SafeHome(_client(ns), house), now)
        intentions.prune(store, now)
        _save(state / "intentions.json", store)
    if events and not _send(ns, intentions.render(events)):
        return 1
    return 1 if any(e["status"] == "failed" for e in events) else 0


def cmd_intend(ns) -> int:
    state, house = _state(ns), _house(ns)
    entity = ((house["zones"].get(ns.zone) or {}).get("climate_entity"))
    if not entity:
        raise SystemExit(f"merrick: unknown zone {ns.zone!r}")
    action = {"kind": "set_temperature", "entity_id": entity, "temperature": ns.temperature}
    with _locked(state):
        store = _load(state / "intentions.json", {"intentions": []})
        now = intentions.utcnow()
        if ns.until:
            if ns.revert_temperature is None:
                raise SystemExit("merrick: --until needs --revert-temperature (what to go back to)")
            made = intentions.register_period(store, ns.description, ns.due, ns.until, action,
                                              {**action, "temperature": ns.revert_temperature}, now)
        else:
            made = (intentions.register(store, ns.description, ns.due, action, now),)
        _save(state / "intentions.json", store)
    print(json.dumps(made, indent=1))
    return 0


def cmd_intentions(ns) -> int:
    print(json.dumps(_load(_state(ns) / "intentions.json", {"intentions": []}), indent=1))
    return 0


def cmd_cancel(ns) -> int:
    state = _state(ns)
    with _locked(state):
        store = _load(state / "intentions.json", {"intentions": []})
        ok = intentions.cancel(store, ns.id)
        _save(state / "intentions.json", store)
    print(json.dumps({"cancelled": ok}))
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="merrick")
    ap.add_argument("--house")
    ap.add_argument("--state-dir")
    ap.add_argument("--ha-cmd")
    ap.add_argument("--openclaw", default="openclaw")
    ap.add_argument("--channel", default="telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ask"); p.add_argument("text"); p.add_argument("--decide-cmd")
    p.add_argument("--budget", type=float, default=budget.DEFAULT_BUDGET_S); p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("budget-check"); p.add_argument("--deadline", type=float, required=True)
    p.set_defaults(fn=cmd_budget_check)
    p = sub.add_parser("fallback-end"); p.add_argument("--id", required=True)
    p.add_argument("--started", type=float, required=True)
    p.add_argument("--outcome", choices=("done", "budget_hit", "failed"), required=True)
    p.set_defaults(fn=cmd_fallback_end)
    p = sub.add_parser("report"); p.add_argument("--days", type=int, default=7)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_report)
    for name, fn in (("cold-watch", cmd_cold_watch), ("tick", cmd_tick)):
        p = sub.add_parser(name); p.add_argument("--to"); p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)
    sub.add_parser("collect").set_defaults(fn=cmd_collect)
    sub.add_parser("confirm-departure").set_defaults(fn=cmd_confirm_departure)
    p = sub.add_parser("intend"); p.add_argument("--description", required=True)
    p.add_argument("--due", required=True, help="ISO time with zone"); p.add_argument("--zone", required=True)
    p.add_argument("--temperature", type=float, required=True); p.add_argument("--until")
    p.add_argument("--revert-temperature", type=float); p.set_defaults(fn=cmd_intend)
    sub.add_parser("intentions").set_defaults(fn=cmd_intentions)
    p = sub.add_parser("cancel"); p.add_argument("id"); p.set_defaults(fn=cmd_cancel)
    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
