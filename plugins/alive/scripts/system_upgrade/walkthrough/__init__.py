"""Walkthrough user-extension migration -- T8 of fn-18.

Two phase-distinct concerns (see epic § Approach):

* :mod:`walkthrough.decide` -- phase 7 (``walkthrough_decide``). Pure
  decisions / UX. Consumes the catalog matches that T3's pre-scan
  stored on ``DetectionReport.walkthrough_eligible_matches`` (NO
  re-scanning, NO re-walking the catalog), renders a per-match prompt,
  and records the operator's y/n/q decisions in
  :class:`WalkthroughDecisions`. **No filesystem writes.** The dry-run
  invariant is enforced by audit grep on this module.

* :mod:`walkthrough.apply` -- phase 9 (called by T9/T10 plugin
  migration). Consumes :class:`WalkthroughDecisions.accepted` and
  rewrites the targeted files: writes a ``<basename>.bak.<UTC-iso-ts>``
  sibling first (atomic, fsync), then the in-place rewrite
  (atomic, fsync). Rewrite payload is **derived from the catalog at
  apply time** -- ``decisions.accepted`` carries
  ``(path, pattern_id, match_span)`` tuples, never pre-computed bytes
  (: rewrite logic stays in ``retired_patterns.py`` as
  the single source of truth).

* :mod:`walkthrough.diff_render` -- helpers that produce the 3-5 line
  excerpts the prompt prints and the "show full diff" branch fans out
  to. Pure / read-only.

Public surface re-exported from this package:

* :class:`WalkthroughDecisions` -- structured output of phase 7.
* :class:`WalkthroughApplyReport` -- structured output of phase 9.
* :func:`decide` -- phase-7 entry.
* :func:`apply` -- phase-9 entry.

The orchestrator stub for ``phase_walkthrough_decide`` continues to
live in :mod:`system_upgrade.orchestrator`; T8 ships the package + API
contract, the orchestrator stub replacement is wired up by the same
task that lands the v2->v3 migration phase (T9/T10).
"""

from __future__ import annotations

from .decide import (  # noqa: F401
    AcceptedDecision,
    SkippedDecision,
    WalkthroughDecisions,
    decide,
)
from .apply import (  # noqa: F401
    WalkthroughApplyReport,
    AppliedRewrite,
    SkippedApply,
    apply,
)


__all__ = (
    "AcceptedDecision",
    "AppliedRewrite",
    "SkippedApply",
    "SkippedDecision",
    "WalkthroughApplyReport",
    "WalkthroughDecisions",
    "apply",
    "decide",
)
