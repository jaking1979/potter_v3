"""Promotion to main under the fast-forward-only policy (T39).

Candidate construction: a change is reviewed as ONE commit whose parent is the
current authoritative main tip (authors squash, then rebase onto main, before
requesting review). Promotion fast-forwards main to exactly that receipted
commit with an ordinary, non-forced push, so the remote itself refuses it if
main moved in the meantime.

* Stale review after rebase: rebasing changes the commit SHA, which has no
  receipt, so it must be re-reviewed (T39 test 4: a commit added after review
  voids the receipt).
* Concurrent promotion: two candidates built on the same main tip; the first
  wins, and the second is no longer a child of the tip, so it is refused here
  and by the remote's fast-forward check. It must be rebased and re-reviewed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from t39.main_detector import _HEX40, DetectorError, _git
from t39.receipt_registry import ReceiptRegistry


class PromotionRefused(RuntimeError):
    pass


def promote(*, git_dir: Path, remote: str, branch: str, candidate: str,
            authoritative_tip: Callable[[], str],
            registry_open: Callable[[], ReceiptRegistry]) -> None:
    git_dir = Path(git_dir).resolve()
    if not _HEX40.fullmatch(candidate):
        raise PromotionRefused("candidate must be a full commit SHA")
    try:
        tip = authoritative_tip()
        parents = _git(git_dir, "rev-list", "--parents", "-n", "1", candidate).split()[1:]
        tree = _git(git_dir, "rev-parse", f"{candidate}^{{tree}}").strip()
    except DetectorError as exc:
        raise PromotionRefused(f"cannot resolve candidate or main: {exc}") from exc
    if parents != [tip]:
        raise PromotionRefused("candidate must be exactly one commit on top of current main "
                               "(squash and rebase, then re-review)")
    registry = registry_open()
    try:
        if registry.issued_trees([candidate]).get(candidate) != tree:
            raise PromotionRefused("candidate has no issued receipt for its exact tree")
    finally:
        registry.close()
    try:
        # Plain push: the remote rejects anything that is not a fast-forward.
        _git(git_dir, "push", "--porcelain", remote, f"{candidate}:refs/heads/{branch}")
    except DetectorError as exc:
        raise PromotionRefused("remote refused the fast-forward (main moved?)") from exc
