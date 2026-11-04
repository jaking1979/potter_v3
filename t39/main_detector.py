"""T39 direct-to-main detector (acceptance test 1).

"Any unreceipted commit on main is a security incident: it is alerted,
reverted, and blocks release." Run at least hourly. Each run:

1. Resolves main's first-parent history since the last checked commit from Git
   itself, with an explicit --git-dir and a scrubbed environment, so a
   redirected or enclosing repository cannot be read instead (cf. PR #9
   review).
2. Requires every first-parent commit C on main to be accounted for by an
   issued receipt, under the merge policy proposed on the PLAN:
     * fast-forward: C itself is the receipted commit; or
     * merge commit: C's second parent is a receipted commit AND C's tree
       equals that receipt's tree (the reviewed tree landed unchanged).
3. On any unaccounted commit: alert, write the release-block file (the release
   builder and updater refuse while it exists), and return a revert plan
   (the commits to revert, newest first). It does NOT push a revert; that is a
   reviewed change like any other.
4. Fails loud: a Git or registry error is reported as a detector failure and
   also blocks release, never as "clean".

The checkpoint only advances when a run is clean, so an incident keeps being
reported until it is resolved through review.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from t39.receipt_registry import ReceiptRegistry

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")


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
    block_error: str | None = None   # set if the release block could not be persisted

    @property
    def clean(self) -> bool:
        return self.error is None and not self.unreceipted


def run_detector(*, git_dir: Path, branch: str, baseline: str, checkpoint_path: Path,
                 block_path: Path, registry_open: Callable[[], ReceiptRegistry],
                 alert: Callable[[str], None], now: datetime) -> Report:
    git_dir = Path(git_dir).resolve()
    try:
        if Path(_git(git_dir, "rev-parse", "--absolute-git-dir").strip()) != git_dir:
            raise DetectorError("git dir resolved somewhere else")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
            raise DetectorError("bad branch name")
        tip = _git(git_dir, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()
        start = baseline
        if checkpoint_path.exists():
            start = json.loads(checkpoint_path.read_text())["last_clean"]
        if not (_HEX40.fullmatch(tip) and _HEX40.fullmatch(start)):
            raise DetectorError("non-SHA-1 commit id")
        # The checkpoint must still be an ancestor of main; history rewritten
        # underneath it is itself an incident.
        try:
            _git(git_dir, "merge-base", "--is-ancestor", start, tip)
        except DetectorError:
            raise DetectorError("main no longer descends from the last clean commit "
                                "(force-push or rewrite)") from None
        commits = _git(git_dir, "rev-list", "--first-parent", "--reverse",
                       f"{start}..{tip}").split()
        parents, trees = {}, {}
        for c in commits:
            line = _git(git_dir, "rev-list", "--parents", "-n", "1", c).split()
            parents[c] = line[1:]
            trees[c] = _git(git_dir, "rev-parse", f"{c}^{{tree}}").strip()
        candidates = sorted({c for c in commits} |
                            {p[1] for p in parents.values() if len(p) == 2})
        registry = registry_open()
        try:
            issued = registry.issued_trees(candidates) if candidates else {}
        finally:
            registry.close()
    except Exception as exc:
        report = Report(checked=[], error=f"{type(exc).__name__}: {exc}")
        _raise_incident(report, block_path, alert, now, f"detector failed: {report.error}",
                        f"T39 detector FAILED on {branch}: {report.error}")
        return report

    report = Report(checked=commits)
    for c in commits:
        ok = c in issued
        p = parents[c]
        if not ok and len(p) == 2 and p[1] in issued and issued[p[1]] == trees[c]:
            ok = True
        if not ok:
            report.unreceipted.append(c)
    if report.unreceipted:
        report.revert_plan = list(reversed(report.unreceipted))
        _raise_incident(report, block_path, alert, now,
                        f"unreceipted commits on {branch}: {report.unreceipted}",
                        f"T39 SECURITY INCIDENT: {len(report.unreceipted)} unreceipted commit(s) "
                        f"on {branch}; revert {report.revert_plan}")
        return report
    tmp = checkpoint_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_clean": tip, "at": now.isoformat()}))
    os.replace(tmp, checkpoint_path)
    return report


def _raise_incident(report: Report, block_path: Path, alert: Callable[[str], None],
                    now: datetime, reason: str, message: str) -> None:
    """Block and alert independently: neither failure may suppress the other."""
    try:
        Path(block_path).write_text(json.dumps({"blocked_at": now.isoformat(),
                                                "reason": reason}))
    except Exception as exc:
        report.block_error = f"{type(exc).__name__}: {exc}"
    suffix = ("; release blocked" if report.block_error is None else
              f"; RELEASE BLOCK NOT PERSISTED ({report.block_error}), block manually")
    alert(message + suffix)     # an alert failure propagates: the run must not look clean


def release_blocked(block_path: Path) -> bool:
    """Builder/updater call this first; the block is cleared only by a reviewed fix."""
    return Path(block_path).exists()
