"""Lets a command job run `python3 <install dir>/merrick <command> ...` without setting PYTHONPATH."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from merrick.cli import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
