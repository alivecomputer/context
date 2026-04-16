"""Make ``python3 -m unittest discover tests`` work from a clean checkout.

Without this, contributors must install the package (``uv sync``, ``pip
install -e .``) or set ``PYTHONPATH=src`` before running the suite. With
this shim, ``unittest discover`` works out of the box -- the ``src/``
layout is resolved automatically when the test package is imported.

The shim is idempotent: if ``alive_mcp`` is already importable (installed
or on ``sys.path``), nothing is added. We only prepend ``src/`` when it
needs prepending.
"""
from __future__ import annotations

import os
import sys


_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
)

if os.path.isdir(_SRC) and _SRC not in sys.path:
    # Prepend so a local checkout beats any older installed copy during
    # tests. This is the standard pattern for src/-layout projects that
    # want "python -m unittest discover" to work without prior install.
    sys.path.insert(0, _SRC)
