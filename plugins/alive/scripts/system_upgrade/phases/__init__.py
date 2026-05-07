"""Per-phase implementations for ``system_upgrade.orchestrator``.

Each phase function lives in ``phases/<phase>.py`` (one file per phase).
``orchestrator.py`` re-exports each so fn-20's pinned import surface
(``from .orchestrator import phase_record``, etc.) keeps resolving.

Architecture rule (relaxed per fn-21 epic):
    Phase modules under ``phases/<phase>.py`` may NOT import another
    ``phases/<phase>.py`` module and may NOT import ``orchestrator.py``
    (would cycle once orchestrator re-exports phases). They MAY import
    ``phases/_shared.py`` AND any other ``system_upgrade/`` implementation
    module + stdlib + ``_common``. Cross-phase data flows via
    ``PipelineContext``.
"""
