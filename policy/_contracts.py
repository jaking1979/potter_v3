"""Single import point for the C0 contracts.

`contracts/v0` is not a Python package; tests import it as the top-level module
`approval`. Importing it the same way here keeps ONE module object, so
`isinstance(x, approval.BoundAction)` agrees between callers and the engine.
"""
import sys
from pathlib import Path

_V0 = str(Path(__file__).resolve().parent.parent / "contracts" / "v0")
if _V0 not in sys.path:
    sys.path.insert(0, _V0)

import approval  # noqa: E402

__all__ = ["approval"]
