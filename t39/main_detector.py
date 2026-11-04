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
     * every first-parent commit after it to BE a receipted commit. main moves
       only by fast-forward to exactly reviewed commits, so the deployed commit
       is always the exact commit a T39 receipt covers (Dolores review on
       54cacc9: a --no-ff merge commit is not itself receipted).
   A clean run anchors a 'clear' event {branch, tip, at}. Anything else
   anchors an 'incident' and alerts. The alert is attempted even if anchoring
   fails, and the run then still reports failure.

2. `release_allowed` (every consumer, before build, deploy, update or
   rollback). It independently re-reads main's CURRENT tip from Git and returns
   True ONLY when (a) that tip is exactly the commit being deployed, (b) that
   commit has an issued receipt, and (c) the latest anchored detector event
   for the branch is a 'clear' of that same commit within MAX_CLEARANCE_AGE.
   It does not depend on an incident having been recorded: once main moves to
   an unreceipted commit, every earlier clear is useless because (a) fails,
   and the new tip fails (b) and (c). Missing, stale, unreadable or incident
   state refuses.

Detector writes are compare-and-set on the branch's latest detector event, so
a delayed or concurrent run cannot append a stale clear after a newer result.

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
                 alert: Callable[[str], None], now: datetime,
                 authoritative_tip: Callable[[], str] | None = None) -> Report:
    git_dir = Path(git_dir).resolve()
    tip = None
    try:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
            raise DetectorError("bad branch name")
        if Path(_git(git_dir, "rev-parse", "--absolute-git-dir").strip()) != git_dir:
            raise DetectorError("git dir resolved somewhere else")
        if authoritative_tip is not None:
            tip = authoritative_tip()          # the remote's main, not a local ref
            if not _HEX40.fullmatch(tip):
                raise DetectorError("bad authoritative tip")
            _git(git_dir, "rev-parse", "--verify", f"{tip}^{{commit}}")  # fetched locally?
        else:
            tip = _git(git_dir, "rev-parse", "--verify",
                       f"refs/heads/{branch}^{{commit}}").strip()
        registry = registry_open()
        try:
            last = registry.last_detector_event(branch)
            last_seq = None if last is None else last["seq"]
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
            issued = registry.issued_trees(commits) if commits else {}
            report = Report(checked=commits)
            for c in commits:
                tree = _git(git_dir, "rev-parse", f"{c}^{{tree}}").strip()
                if issued.get(c) != tree:          # exact receipted commit and tree
                    report.unreceipted.append(c)
            if report.unreceipted:
                report.revert_plan = list(reversed(report.unreceipted))
                _incident(registry, report, alert, branch, tip, start, now, last_seq,
                          f"T39 SECURITY INCIDENT: {len(report.unreceipted)} unreceipted "
                          f"commit(s) on {branch}; revert {report.revert_plan}")
                return report
            try:
                registry.record_detector("clear", {"branch": branch, "tip": tip,
                                                   "at": now.isoformat()}, last_seq)
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
                prev = registry.last_detector_event(branch)
                _incident(registry, report, alert, branch, tip or "", baseline, now,
                          None if prev is None else prev["seq"],
                          f"T39 detector FAILED on {branch}: {report.error}")
            finally:
                registry.close()
        except Exception as exc2:
            report.anchor_error = f"{type(exc2).__name__}: {exc2}"
            alert(f"T39 detector FAILED on {branch}: {report.error}; incident NOT anchored "
                  f"({report.anchor_error}); deploys stay refused")
        return report


def _incident(registry: ReceiptRegistry, report: Report, alert: Callable[[str], None],
              branch: str, tip: str, start: str, now: datetime, last_seq: int | None,
              message: str) -> None:
    """Anchor the incident and alert, independently of each other."""
    try:
        registry.record_detector("incident", {
            "branch": branch, "tip": tip, "from": start, "at": now.isoformat(),
            "unreceipted": report.unreceipted, "revert_plan": report.revert_plan,
            "error": report.error}, last_seq)
    except Exception as exc:
        report.anchor_error = f"{type(exc).__name__}: {exc}"
    alert(message + ("" if report.anchor_error is None
                     else f"; incident NOT anchored ({report.anchor_error})")
          + "; deploys refused")


def ls_remote_tip(git_dir: Path, remote: str, branch: str) -> str:
    """Authoritative tip of `branch` on `remote` (e.g. GitHub); raises if unreachable."""
    out = _git(Path(git_dir), "ls-remote", "--exit-code", remote, f"refs/heads/{branch}").split()
    if len(out) != 2 or not _HEX40.fullmatch(out[0]):
        raise DetectorError("unexpected ls-remote output")
    return out[0]


def release_allowed(*, git_dir: Path, registry_open: Callable[[], ReceiptRegistry],
                    branch: str, commit: str, now: datetime,
                    authoritative_tip: Callable[[], str], purpose: str = "deploy",
                    max_age: timedelta = MAX_CLEARANCE_AGE) -> bool:
    """Fail-closed gate for every builder, updater, rollback and manual deploy.

    `authoritative_tip` reads main from the authoritative remote at call time
    (never a possibly stale local ref); if it fails, nothing is allowed.

    purpose="deploy": `commit` IS the authoritative tip, carries an issued
      receipt for its exact tree, and has a fresh anchored 'clear'.
    purpose="rollback": `commit` carries an issued receipt for its exact tree
      and lies on the authoritative main's first-parent chain at or before the
      last anchored 'clear' tip. No freshness requirement, and it is allowed
      during an open incident: rolling back to cleared code is the remedy.

    Consumers call this again AFTER deploying (`purpose="deploy"`); if it now
    fails, main moved during the deploy window and they roll back to the last
    cleared commit via `purpose="rollback"`.
    """
    try:
        if not _HEX40.fullmatch(commit) or ".." in branch or purpose not in ("deploy", "rollback"):
            return False
        git_dir = Path(git_dir).resolve()
        if Path(_git(git_dir, "rev-parse", "--absolute-git-dir").strip()) != git_dir:
            return False
        tip = authoritative_tip()
        if not _HEX40.fullmatch(tip):
            return False
        # Objects must be present locally (the consumer fetched); never fall back
        # to the local ref for the tip itself.
        mainline = _git(git_dir, "rev-list", "--first-parent", tip).split()
        tree = _git(git_dir, "rev-parse", f"{commit}^{{tree}}").strip()
        registry = registry_open()
        try:
            if registry.issued_trees([commit]).get(commit) != tree:
                return False                # the exact commit must carry a receipt
            last = registry.last_detector_event(branch)
            last_clear = last if (last is not None and last["kind"] == "clear") else None
            if last is not None and last_clear is None:
                # An incident is open: find the last clear before it for rollback.
                for e in reversed(registry.detector_events(branch)):
                    if e["kind"] == "clear":
                        last_clear = e
                        break
        finally:
            registry.close()
        if purpose == "deploy":
            if commit != tip or last is None or last["kind"] != "clear":
                return False
            if last["body"]["tip"] != commit:
                return False
            at = datetime.fromisoformat(last["body"]["at"])
            return -MAX_CLOCK_SKEW <= now - at <= max_age
        # rollback
        if last_clear is None:
            return False
        cleared = last_clear["body"]["tip"]
        if cleared not in mainline or commit not in mainline:
            return False
        return mainline.index(commit) >= mainline.index(cleared)   # at or before cleared tip
    except Exception:
        return False
