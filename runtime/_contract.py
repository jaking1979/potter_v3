"""Load the C0 approval contract (contracts/v0/approval.py) as the top-level
module ``approval``, the same module object the C0 tests import, so that
EffectIntent / BoundAction instances compare equal across both."""
from __future__ import annotations

import sys
from pathlib import Path

_V0 = str(Path(__file__).resolve().parent.parent / "contracts" / "v0")
if _V0 not in sys.path:
    sys.path.insert(0, _V0)

from approval import ApprovalRefused, BoundAction, EffectIntent  # noqa: E402,F401
