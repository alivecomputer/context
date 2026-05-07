"""Stdlib-only helpers shared across system_upgrade phase implementations.

Houses two helpers that previously had near-duplicate bodies in
``orchestrator.py`` (`phase_record` operations enumeration +
`_enumerate_planned_operations`) and in `retired_patterns.py` /
`verify.py` (UTF-8 char-to-byte offset computation):

* :func:`enumerate_operations` -- aggregates cleanup_report + migration
  reports (+ optional pre-upgrade backup tarball) into the canonical
  ``operations: List[Dict[str, Any]]`` shape consumed by both the
  forensic record (full-run) and the dry-run plan file.
* :func:`compute_byte_offsets` -- given UTF-8 ``text`` and a
  ``(start_chars, end_chars)`` regex span, return ``(start_bytes,
  end_bytes)`` so non-ASCII content keeps consistent positions across
  catalog-match dataclasses (``CatalogMatch`` and
  ``CatalogMatchFinding``).

Co-located here (not in ``_common.py``): ``enumerate_operations``
references shapes that are system_upgrade-specific (cleanup reports,
migration op blobs, retired-signal tail mapping). Bundling the two
helpers in one module keeps the system_upgrade package's vocabulary
out of the broader scripts-level shared module.

Co-move from ``orchestrator.py``: :data:`_PATH_TAIL_TO_SIGNAL` and
:func:`_signal_id_for_path` MUST live here too. ``enumerate_operations``
references them for the cleanup ``detail`` field; if this module
imported them back from ``orchestrator.py`` (which will re-export
phase functions in fn-21.7) we would create an import cycle.

Stdlib-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# Map filesystem path tails to retired-pattern signal-ids the canary
# audit consults via op blob ``detail`` field. Centralised here so the
# canary's ``CANARY_RETIRED_SIGNALS`` round-trip stays sharp.
_PATH_TAIL_TO_SIGNAL = (
    (".alive/scripts", "scripts"),
    (".alive/atoms", "atoms"),
    (".alive/computed", "computed"),
    (".alive/locks", "locks"),
    (".alive/overrides.md", "overrides"),
    (".alive/upgrade-plan.html", "upgrade-plan"),
)


def _signal_id_for_path(path: str) -> str:
    """Best-effort signal-id stamp for *path* (canary record search)."""
    for tail, sig in _PATH_TAIL_TO_SIGNAL:
        if path.endswith(tail) or ("/" + tail) in path:
            return sig
    return ""


def enumerate_operations(
    cleanup_report: Any = None,
    migration_reports: Optional[List[Any]] = None,
    backup_tarball_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Aggregate ops from cleanup + migration (+ optional backup tarball).

    Returns a list of plain dicts in the canonical operation vocabulary
    shared by both the full-run forensic record (``phase_record``) and
    the dry-run plan file (``_enumerate_planned_operations``). Dicts --
    not dataclasses -- keep the YAML emitter and the invariant tests
    trivial.

    Branches:
        * ``cleanup_report`` -- iterates ``deleted`` (status=applied,
          detail = retired-signal tail mapping) + ``skipped`` pairs
          (status=skipped, detail = caller-supplied reason).
        * ``migration_reports`` -- flattens each report's ``operations``
          attribute into the dict shape (op_type, status, target /
          path / to_path / detail / walnut_root).
        * ``backup_tarball_path`` -- when supplied (full-run only),
          appends a single ``backup_tarball`` op so the rollback
          pointer surfaces in the canonical record. Omitted from the
          dry-run plan path (planning doesn't take a tarball).

    All three sources are independently optional; missing sources
    contribute zero ops.
    """

    operations: List[Dict[str, Any]] = []

    # Cleanup operations.
    if cleanup_report is not None:
        for path in getattr(cleanup_report, "deleted", ()):
            operations.append({
                "op_type": "cleanup_delete",
                "status": "applied",
                "target": path,
                "path": path,
                "detail": _signal_id_for_path(path),
            })
        for path, reason in getattr(cleanup_report, "skipped", ()):
            operations.append({
                "op_type": "cleanup_skipped",
                "status": "skipped",
                "target": path,
                "path": path,
                "detail": reason,
            })

    # Migration operations.
    for mig in (migration_reports or []):
        for op in getattr(mig, "operations", ()):
            operations.append({
                "op_type": op.op_type,
                "status": op.status,
                "target": op.to_path or op.from_path,
                "path": op.from_path,
                "to_path": op.to_path,
                "detail": op.detail,
                "walnut_root": op.walnut_root,
            })

    # Backup operation (single op so the rollback pointer surfaces in
    # the canonical record). Full-run only -- planning omits it.
    if backup_tarball_path:
        operations.append({
            "op_type": "backup_tarball",
            "status": "applied",
            "target": backup_tarball_path,
            "path": backup_tarball_path,
            "detail": "pre-upgrade tarball",
        })

    return operations


def compute_byte_offsets(
    text: str,
    start_chars: int,
    end_chars: int,
) -> Tuple[int, int]:
    """Translate a Python char-span ``(start_chars, end_chars)`` over
    *text* into a UTF-8 byte-span ``(start_bytes, end_bytes)``.

    Catalog-match dataclasses (``CatalogMatch`` in ``retired_patterns``
    and ``CatalogMatchFinding`` in ``verify``) record byte offsets so
    non-ASCII content keeps consistent positions across the API.
    Both call sites consume this tuple and build their own dataclass.
    """

    start_bytes = len(text[:start_chars].encode("utf-8"))
    end_bytes = start_bytes + len(text[start_chars:end_chars].encode("utf-8"))
    return start_bytes, end_bytes
