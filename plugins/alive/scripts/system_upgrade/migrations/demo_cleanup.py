"""Demo-skill ``_stage_outputs/`` cleanup, marker-file driven (T10).

The fn-17 demo skill installs entities from ``<world>/_stage_outputs/``
into the canonical walnut layout via ``step_6_install_entities``, then
clears the staging directory. A *healthy* demo run leaves no
``_stage_outputs/`` behind once the install commits.

This module surfaces the cleanup-or-skip decision system-upgrade phase
9 needs when a world still has ``_stage_outputs/`` lying around. The
authoritative signal is the marker file the demo skill drops into the
staging directory:

    ``<world>/_stage_outputs/.demo-state.yaml``

Three cases the cleanup contract recognises (per epic spec § Approach
"Demo-detection heuristic" / gap-analyst J4):

1. **Marker present, ``complete: true``** -- install committed; the
   leftover directory is a forensic remnant we can safely cleanup.

2. **Marker present, ``complete: false``** -- install in progress (the
   skill is mid-pipeline). System-upgrade MUST NOT touch the directory;
   we surface a warning and skip. The operator handles this by
   completing or aborting the demo run before re-attempting upgrade.

3. **Marker absent, but ``_stage_outputs/entities/`` present** --
   pre-fn-17 abandoned demo (an older run that never had the marker
   contract). Cleanup is safe; the v3.2 install step would have already
   relocated this content if the demo had run cleanly under fn-17. We
   flag the cleanup with reason ``"pre-fn-17 abandoned demo"`` so the
   operator sees the provenance in the upgrade record.

Idempotency: re-running this op against an already-cleaned world is a
no-op (no marker, no entities/, returns ``None`` -- nothing to record).

Stdlib-only (R10): YAML read uses a tiny regex parser sufficient for the
top-level ``complete`` key.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Dict, Optional


__all__ = (
    "DemoCleanupResult",
    "decide_cleanup",
    "run_demo_cleanup",
)


#: Filename the demo skill writes inside the staging directory while a
#: run is in flight; the canonical fn-17 marker contract.
DEMO_STATE_MARKER: str = ".demo-state.yaml"

#: Sub-directory the v3.2 demo skill writes per-walnut entity payloads
#: into during stage 0-4. Presence-without-marker imputes a pre-fn-17
#: abandoned demo (see module docstring).
ENTITIES_SUBDIR: str = "entities"


#: Top-level YAML-key extractor for the marker. We only ever read
#: ``complete:`` (boolean). Anything else lives in the live demo-state
#: JSON, not the staging marker.
_MARKER_KEY_RE = re.compile(
    r"^complete\s*:\s*(true|false|True|False|yes|no|Yes|No)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class DemoCleanupResult:
    """One demo-cleanup decision + its applied effect.

    Attributes
    ----------
    action :
        ``"cleanup"``       -- ``_stage_outputs/`` was (or would be)
                               removed.
        ``"skip"``          -- in-flight demo run; do nothing.
        ``"failed"``        -- planned cleanup raised on
                               ``shutil.rmtree`` (or another I/O
                               failure mid-removal). Distinct from
                               ``"skip"`` so the migration runner can
                               surface the failure as
                               ``OpResult.status="failed"`` and halt
                               on ``halt_on_failure=True`` instead of
                               silently completing the phase with
                               ``_stage_outputs/`` still on disk.
        ``"absent"``        -- nothing to do (no marker, no entities/).
                               Returned by :func:`decide_cleanup`; the
                               runner short-circuits before this leaks
                               into a report.
    reason :
        Human-readable note suitable for the upgrade record. One of:
          * ``"complete demo run"``
          * ``"in-flight demo run"``
          * ``"pre-fn-17 abandoned demo"``
          * ``"no demo state present"``
          * ``"cleanup removal failed"`` (paired with ``action="failed"``)
    marker_present :
        Whether ``_stage_outputs/.demo-state.yaml`` was on disk at
        decide time.
    entities_dir_present :
        Whether ``_stage_outputs/entities/`` was on disk at decide time.
    stage_outputs_path :
        Absolute path the decision applied to.
    removed :
        ``True`` when the runner actually deleted the directory.
        ``False`` for ``"skip"``, ``"absent"``, or ``dry_run=True``.
    detail :
        Optional extra detail (warning text, etc.).
    """

    action: str
    reason: str
    marker_present: bool
    entities_dir_present: bool
    stage_outputs_path: str
    removed: bool = False
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict form suitable for runstate codec serialisation."""
        return {
            "op_type": "demo_cleanup",
            "action": self.action,
            "reason": self.reason,
            "marker_present": self.marker_present,
            "entities_dir_present": self.entities_dir_present,
            "stage_outputs_path": self.stage_outputs_path,
            "removed": self.removed,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Marker parsing
# ---------------------------------------------------------------------------


def _read_marker_complete(marker_path: str) -> Optional[bool]:
    """Return the ``complete`` boolean from the marker, or ``None``.

    ``None`` covers:

    * marker file missing
    * unreadable / decode failure
    * ``complete:`` key absent or unparseable

    The caller treats ``None`` from a present marker as conservative
    "in-flight" (we never delete on a malformed marker).
    """
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    m = _MARKER_KEY_RE.search(text)
    if m is None:
        return None
    val = m.group(1).lower()
    if val in ("true", "yes"):
        return True
    if val in ("false", "no"):
        return False
    return None


# ---------------------------------------------------------------------------
# Decision API (no filesystem writes)
# ---------------------------------------------------------------------------


def decide_cleanup(world_root: str) -> DemoCleanupResult:
    """Classify a world's ``_stage_outputs/`` state. Read-only.

    Returns a :class:`DemoCleanupResult` carrying the action the
    runner should take. Pure side-effect-free classification so the
    orchestrator's ``--dry-run`` path can surface the same decision
    without writes.

    Edge cases:

    * ``_stage_outputs/`` absent entirely -> ``action="absent"``.
    * Marker present, parseable, ``complete: true`` -> ``"cleanup"``,
      reason ``"complete demo run"``.
    * Marker present, parseable, ``complete: false`` -> ``"skip"``,
      reason ``"in-flight demo run"``.
    * Marker present but unparseable / unreadable -> ``"skip"`` with
      detail noting the malformed marker (conservative; never delete
      a directory whose state we can't establish).
    * Marker absent, ``entities/`` present -> ``"cleanup"``, reason
      ``"pre-fn-17 abandoned demo"``.
    * Marker absent, ``entities/`` absent -> ``"absent"``.
    """
    world_root = os.path.abspath(world_root)
    stage_root = os.path.join(world_root, "_stage_outputs")

    if not os.path.isdir(stage_root):
        return DemoCleanupResult(
            action="absent",
            reason="no demo state present",
            marker_present=False,
            entities_dir_present=False,
            stage_outputs_path=stage_root,
        )

    marker_path = os.path.join(stage_root, DEMO_STATE_MARKER)
    entities_path = os.path.join(stage_root, ENTITIES_SUBDIR)
    marker_present = os.path.isfile(marker_path)
    entities_present = os.path.isdir(entities_path)

    if marker_present:
        complete = _read_marker_complete(marker_path)
        if complete is True:
            return DemoCleanupResult(
                action="cleanup",
                reason="complete demo run",
                marker_present=True,
                entities_dir_present=entities_present,
                stage_outputs_path=stage_root,
            )
        if complete is False:
            return DemoCleanupResult(
                action="skip",
                reason="in-flight demo run",
                marker_present=True,
                entities_dir_present=entities_present,
                stage_outputs_path=stage_root,
                detail=(
                    "demo-state marker reports complete: false; refusing "
                    "to remove _stage_outputs/ while a demo run is in "
                    "flight. Complete or reset the demo first."
                ),
            )
        # complete is None -- malformed marker. Skip conservatively.
        return DemoCleanupResult(
            action="skip",
            reason="in-flight demo run",
            marker_present=True,
            entities_dir_present=entities_present,
            stage_outputs_path=stage_root,
            detail=(
                "demo-state marker present but 'complete:' key was "
                "missing or unparseable; refusing to remove "
                "_stage_outputs/ on indeterminate state."
            ),
        )

    # Marker absent -- consult entities/ for pre-fn-17 abandoned-demo
    # evidence.
    if entities_present:
        return DemoCleanupResult(
            action="cleanup",
            reason="pre-fn-17 abandoned demo",
            marker_present=False,
            entities_dir_present=True,
            stage_outputs_path=stage_root,
        )

    return DemoCleanupResult(
        action="absent",
        reason="no demo state present",
        marker_present=False,
        entities_dir_present=False,
        stage_outputs_path=stage_root,
    )


# ---------------------------------------------------------------------------
# Runner (filesystem mutation)
# ---------------------------------------------------------------------------


def run_demo_cleanup(
    world_root: str,
    *,
    dry_run: bool = False,
) -> Optional[DemoCleanupResult]:
    """Execute the cleanup decided by :func:`decide_cleanup`.

    Returns ``None`` when the decision was ``"absent"`` (nothing to
    record on the migration report). Returns the
    :class:`DemoCleanupResult` for ``"cleanup"``, ``"skip"``, and
    ``"failed"`` (the latter when the planned removal raises an
    ``OSError``).

    Under ``dry_run=True`` no removal happens; the result still carries
    ``action="cleanup"`` (the planned action) but ``removed=False``.
    Mirrors the v2 -> v3.0 runner's per-op dry-run shape so the
    orchestrator's planned-output contract is consistent across
    migration phases.

    The ``"failed"`` action is distinct from ``"skip"`` so the migration
    runner can map removal failures to ``OpResult.status="failed"``
    and honour ``halt_on_failure=True``. Demoting failures to skip
    would silently complete the v3.1 -> v3.2 phase with
    ``_stage_outputs/`` still on disk and the resume marker promoted
    to COMPLETED -- a regression captured by the test suite.
    """
    decision = decide_cleanup(world_root)
    if decision.action == "absent":
        return None

    if decision.action == "skip" or dry_run:
        return decision

    # action == "cleanup", dry_run=False -> remove the directory.
    try:
        shutil.rmtree(decision.stage_outputs_path)
    except OSError as exc:
        # Surface the error as a distinct ``failed`` action so the
        # migration runner can map it to ``OpResult.status="failed"``
        # and halt the phase under ``halt_on_failure=True`` instead of
        # silently completing while ``_stage_outputs/`` remains on
        # disk. The directory is left in place for the operator's
        # manual reconciliation; the runstate captures the OSError.
        return DemoCleanupResult(
            action="failed",
            reason="cleanup removal failed",
            marker_present=decision.marker_present,
            entities_dir_present=decision.entities_dir_present,
            stage_outputs_path=decision.stage_outputs_path,
            removed=False,
            detail="removal failed: {}".format(exc),
        )

    return DemoCleanupResult(
        action="cleanup",
        reason=decision.reason,
        marker_present=decision.marker_present,
        entities_dir_present=decision.entities_dir_present,
        stage_outputs_path=decision.stage_outputs_path,
        removed=True,
        detail=decision.detail,
    )
