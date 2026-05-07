"""``system_upgrade.migrations`` -- per-version migration runners (T9+).

Each migration runner consumes phase-3's ``DetectionReport`` plus the
phase-7 ``WalkthroughDecisions`` and produces a ``MigrationReport``.
The orchestrator (phase 9, ``plugin_migrate``) calls each applicable
runner in order (v2 -> v3.0 first, then v3.0 -> v3.1, then v3.1 -> v3.2).

The orchestrator merges every runner's ``MigrationReport`` into the
final upgrade record at phase 12. Individual runners write only:

* ``<world>/.alive/upgrades/<iso-ts>-runstate.yaml`` -- forensic-only
  per-operation log (via :mod:`._record`).
* ``<world>/.alive/upgrades/<iso-ts>-retroactive.yaml`` -- backfill
  record for messy worlds (via :mod:`._retroactive`).

Runners NEVER write the canonical final record (``<iso-ts>.yaml``);
that is owned by phase 12 of the orchestrator. The strict filename
pattern enforced by ``surfaces.load_prior_final_record`` excludes the
``-runstate`` and ``-retroactive`` suffixes by design.

Stdlib-only (R10): no PyYAML / ruamel.

Lazy-loading note
-----------------
``v2_to_v3_0.py`` carries a 1,540 LOC body that historically loaded on
every ``import system_upgrade.migrations`` because of the eager
re-export below. The body is now deferred via PEP 562 ``__getattr__``:
only attribute access to ``run_v2_to_v3_0`` triggers the runner load.
``MigrationReport`` and ``OpResult`` resolve eagerly from
:mod:`._record` (cheap stdlib-only types) so callers can read the
result-type surface without paying the runner's import cost. The
sibling runners ``v3_0_to_v3_1`` / ``v3_1_to_v3_2`` are tiny by
comparison and stay eager. PEP 562 fires on attribute access (incl.
``hasattr``); it does NOT fire on ``dir()`` or star-import name
resolution -- the project audit checked for those triggers and found
none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Eager: cheap stdlib-only result + report dataclasses. Resolving
# ``migrations.MigrationReport`` / ``migrations.OpResult`` MUST NOT
# load ``v2_to_v3_0.py``; binding them at module-import time guarantees
# attribute access never falls through to ``__getattr__``.
from ._record import MigrationReport, OpResult

# Eager: small sibling runners (no 1,540 LOC body to defer).
from .v3_0_to_v3_1 import run_v3_0_to_v3_1
from .v3_1_to_v3_2 import run_v3_1_to_v3_2


if TYPE_CHECKING:
    # Static analysers see the symbol without triggering an import at
    # runtime. The real binding happens lazily via ``__getattr__``.
    from .v2_to_v3_0 import run_v2_to_v3_0


__all__ = (
    "MigrationReport",
    "OpResult",
    "run_v2_to_v3_0",
    "run_v3_0_to_v3_1",
    "run_v3_1_to_v3_2",
)


# Names that genuinely live in ``v2_to_v3_0.py`` and require the
# 1,540 LOC body to load before resolution. ``MigrationReport`` and
# ``OpResult`` are intentionally NOT in this set -- they resolve from
# the eager bindings above and never trigger v2 load.
_LAZY_V2_NAMES = frozenset({"run_v2_to_v3_0"})


def __getattr__(name: str):  # PEP 562
    """Lazy-load symbols that live in ``v2_to_v3_0.py``.

    Fires only when an attribute is missing from the module's normal
    namespace. ``MigrationReport`` / ``OpResult`` are bound eagerly
    from :mod:`._record`, so this hook never resolves them and the
    1,540 LOC ``v2_to_v3_0`` body stays out of the import graph until a
    caller actually asks for ``run_v2_to_v3_0``.
    """
    if name in _LAZY_V2_NAMES:
        from . import v2_to_v3_0 as _v2  # local import = lazy load

        value = getattr(_v2, name)
        # Cache on the package so subsequent lookups skip ``__getattr__``.
        globals()[name] = value
        return value
    raise AttributeError(
        "module {!r} has no attribute {!r}".format(__name__, name)
    )
