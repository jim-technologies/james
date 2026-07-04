"""Composition root package; bootstraps sys.path for the flat repo layout.

Importing this package (e.g. via ``python -m app``) puts the repo root and the
generated stubs (gen/python) on sys.path before the entry point's imports run,
so the flat layout works regardless of the current working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
for _path in (str(_root), str(_root / "gen" / "python")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
