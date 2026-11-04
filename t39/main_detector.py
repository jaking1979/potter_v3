"""T39 direct-to-main detector and deploy gate (acceptance test 1).

"Any unreceipted commit on main is a security incident: it is alerted,
reverted, and blocks release." "A direct push to main is flagged within 1 h
and is never deployable."

Two halves, both backed by the anchor log (the authority; see
receipt_registry), so the audit trail is durable and a database-only writer
cannot fake it:

1. `run_detector` (hourly). It reads main's first-parent history from Git with
   an explicit --git-dir and a scrubbed environment. It starts from the last
   anchored 'clear' tip for the branch, or the trusted baseline before the
   first run, and requires:
     * that start point to lie ON main's first-parent chain. Mere ancestry is
       not enough: a rewritten mainline that keeps the old tip only as a
       merge's second parent is a rewrite (Dolores review on 68c925b);
     * every first-parent commit after it to be accounted for by an issued
       receipt: either the receipted commit itself, or a merge whose second
       parent is receipted and whose tree equals that receipt's tree.
   A clean run anchors a 'clear' event {branch, tip, at}. Anything else
   anchors an 'incident' and alerts. The alert is attempted even if anchoring
   fails, and the run then still reports failure.

2. `release_allowed` (every consumer, before build, deploy, update or
   rollback). It returns True ONLY when the latest anchored detector event for
   the branch is a 'clear' whose tip is exactly the commit being deployed and
   whose time is within MAX_CLEARANCE_AGE. A missing, stale, unreadable or
   incident state refuses. So a commit pushed after the last clean run is not
   deployable until the detector has cleared it ("never deployable between
   runs"), and losing a block file can no longer unblock anything: the
   positive clearance is what is required.

It never pushes a revert; the revert plan goes into the incident and alert.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from t39.receipt_registry import ReceiptRegistry

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
MAX_CLEARANCE_AGE = timedelta(minutes=65)
MAX_CLOCK_SKEW = timedelta(minutes=2)


class DetectorError(RuntimeError):
    pass


def _git(git_dir: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_CEILING_DIRECTORIES=str(Path(git_dir).parent))
    try:
        return subprocess.run(["git", f"--git-dir={git_dir}", *args], env=env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30).stdout.decode("ascii")
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError) as exc:
        raise DetectorError(f"git failed: {args[0]}") from exc


@dataclass
class Report:
    checked: list[str]
    unreceipted: list[str] = field(default_factory=list)
    revert_plan: list[str] = field(default_factory=list)   # newest first
    error: str | None = None
    anchor_error: str | None = None   # outcome could not be anchored

    @property
    def clean(self) -> bool:
        return self.error is None and not self.unreceipted and self.anchor_error is None


def run_detector(*, git_dir: Path, branch: str, baseline: str,
                 registry_open: Callable[[], ReceiptRegistry],
                 alert: Callable[[str], None], now: datetime) -> Report:
    git_dir = Path(git_dir).resolve()
    tip = None
    try:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
            raise DetectorError("bad branch name")
        if Path(_git(git_dir, "rev-parse", "--absolute-git-dir").strip()) != git_dir:
            raise DetectorError("git dir resolved somewhere else")
        tip = _git(git_dir, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()
        registry = registry_open()
        try:
            last = registry.last_detector_event(branch)
            start = baseline
            if last is not None and last["kind"] == "clear":
                start = last["body"]["tip"]
            elif last is not None:                       # unresolved incident: re-check
                start = last["body"].get("from", baseline)
            if not (_HEX40.fullmatch(tip) and _HEX40.fullmatch(start)):
                raise DetectorError("non-SHA-1 commit id")
            mainline = _git(git_dir, "rev-list", "--first-parent", tip).split()
            if start not in mainline:
                raise DetectorError("last clean tip is not on main's first-parent chain "
                                    "(force-push or history rewrite)")
            commits = mainline[:mainline.index(start)][::-1]   # oldest first, after start
            parents, trees = {}, {}
            for c in commits:
                parents[c] = _git(git_dir, "rev-list", "--parents", "-n", "1", c).split()[1:]
                trees[c] = _git(git_dir, "rev-parse", f"{c}^{{tree}}").strip()
            candidates = sorted(set(commits) |
                                {p[1] for p in parents.values() if len(p) == 2})
            issued = registry.issued_trees(candidates) if candidates else {}

            report = Report(checked=commits)
            for c in commits:
                p = parents[c]
                ok = c in issued or (len(p) == 2 and p[1] in issued and issued[p[1]] == trees[c])
                if not ok:
                    report.unreceipted.append(c)
            if report.unreceipted:
                report.revert_plan = list(reversed(report.unreceipted))
                _incident(registry, report, alert, branch, tip, start, now,
                          f"T39 SECURITY INCIDENT: {len(report.unreceipted)} unreceipted "
                          f"commit(s) on {branch}; revert {report.revert_plan}")
                return report
            try:
                registry.record_detector("clear", {"branch": branch, "tip": tip,
                                                   "at": now.isoformat()})
            except Exception as exc:
                report.anchor_error = f"{type(exc).__name__}: {exc}"
                alert(f"T39 detector could not anchor clearance for {branch}; "
                      f"deploys stay refused: {report.anchor_error}")
            return report
        finally:
            registry.close()
    except Exception as exc:
        report = Report(checked=[], error=f"{type(exc).__name__}: {exc}")
        try:
            registry = registry_open()
            try:
                _incident(registry, report, alert, branch, tip or "", baseline, now,
                          f"T39 detector FAILED on {branch}: {report.error}")
            finally:
                registry.close()
        except Exception as exc2:
            report.anchor_error = f"{type(exc2).__name__}: {exc2}"
            alert(f"T39 detector FAILED on {branch}: {report.error}; incident NOT anchored "
                  f"({report.anchor_error}); deploys stay refused")
        return report


def _incident(registry: ReceiptRegistry, report: Report, alert: Callable[[str], None],
              branch: str, tip: str, start: str, now: datetime, message: str) -> None:
    """Anchor the incident and alert, independently of each other."""
    try:
        registry.record_detector("incident", {
            "branch": branch, "tip": tip, "from": start, "at": now.isoformat(),
            "unreceipted": report.unreceipted, "revert_plan": report.revert_plan,
            "error": report.error})
    except Exception as exc:
        report.anchor_error = f"{type(exc).__name__}: {exc}"
    alert(message + ("" if report.anchor_error is None
                     else f"; incident NOT anchored ({report.anchor_error})")
          + "; deploys refused")


def release_allowed(*, registry_open: Callable[[], ReceiptRegistry], branch: str,
                    commit: str, now: datetime,
                    max_age: timedelta = MAX_CLEARANCE_AGE) -> bool:
    """Fail closed: True only for a fresh anchored 'clear' of exactly this commit."""
    try:
        if not _HEX40.fullmatch(commit):
            return False
        registry = registry_open()
        try:
            last = registry.last_detector_event(branch)
        finally:
            registry.close()
        if last is None or last["kind"] != "clear" or last["body"]["tip"] != commit:
            return False
        at = datetime.fromisoformat(last["body"]["at"])
        return -MAX_CLOCK_SKEW <= now - at <= max_age
    except Exception:
        return False
