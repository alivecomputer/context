"""World-fingerprint signal sources for content-based version detection.

Three signal source families, each implemented in its own module:

* ``path_existence``  -- presence/absence of canonical paths under the
  world root or a candidate walnut. The most numerous source family;
  carries v1, v2, v3.0/v3.1/v3.2 markers.
* ``bundle_schema``    -- scans walnut bundle YAML headers for canonical
  v3.1+ fields (``species``, ``phase``, ``goal``, ``context_routes``).
* ``hook_content``     -- scans user-extension files (``.alive/skills/``,
  ``.alive/rules/``, ``.alive/hooks/``) for the ``ALIVE_PLUGIN_ROOT``
  pattern that landed in v3.1 (commit f565c81).

Each source returns a list of :class:`SignalProbe` records describing
ONE probed feature -- present or absent, scoped per-world or per-walnut,
with a version inferred when the probe fires. ``DetectionReport.
all_signals_raw`` carries every probe (fired or not) for forensic
debugging and lowest-version-wins resolution.

Signal sources MUST NOT touch the disk after the start-of-run
:class:`FileSnapshot` is built. Every read goes through ``snapshot.read``
or ``snapshot.exists``; missing-from-snapshot is a contract violation
that surfaces as a hard error from the snapshot itself (caught by the
detection-side wrapper in :mod:`version_detect`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


__all__ = (
    "SignalProbe",
    "SCOPE_WORLD",
    "SCOPE_WALNUT",
    "SOURCE_PATH",
    "SOURCE_SCHEMA",
    "SOURCE_CONTENT",
    "ALL_SOURCES",
    "ALL_SCOPES",
)


SCOPE_WORLD = "world"
SCOPE_WALNUT = "walnut"
ALL_SCOPES = (SCOPE_WORLD, SCOPE_WALNUT)

SOURCE_PATH = "path"
SOURCE_SCHEMA = "schema"
SOURCE_CONTENT = "content"
ALL_SOURCES = (SOURCE_PATH, SOURCE_SCHEMA, SOURCE_CONTENT)


# Source-rank tie-breaker for lowest-version-wins resolution. Lower
# rank wins on ties (epic § Resolution policy).
SOURCE_RANK: Dict[str, int] = {
    SOURCE_PATH: 0,
    SOURCE_SCHEMA: 1,
    SOURCE_CONTENT: 2,
}


@dataclass(frozen=True)
class SignalProbe:
    """One probe of one fingerprint feature.

    Attributes
    ----------
    probe_id : str
        Stable identifier for the probe; ``all_signals_raw`` keys on this.
    source : str
        One of :data:`ALL_SOURCES`. Used for the source-rank tie-break.
    scope : str
        One of :data:`ALL_SCOPES`. ``walnut`` probes carry
        ``walnut_path`` so the resolver can group per-walnut.
    fired : bool
        True iff the underlying feature was detected in the snapshot.
    inferred_version : str | None
        Version string the probe imputes when it fires. ``None`` for
        absent probes.
    walnut_path : str | None
        Absolute path of the walnut for ``walnut`` scope probes.
    detail : str
        Short human-readable note (e.g. matched path, regex hit count).
    """

    probe_id: str
    source: str
    scope: str
    fired: bool
    inferred_version: Optional[str] = None
    walnut_path: Optional[str] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.source not in ALL_SOURCES:
            raise ValueError(
                "source {!r} not in {}".format(self.source, ALL_SOURCES)
            )
        if self.scope not in ALL_SCOPES:
            raise ValueError(
                "scope {!r} not in {}".format(self.scope, ALL_SCOPES)
            )
        if self.scope == SCOPE_WALNUT and self.walnut_path is None:
            raise ValueError(
                "walnut-scoped probe {} missing walnut_path".format(
                    self.probe_id
                )
            )

    def as_dict(self) -> Dict[str, Any]:
        """Serializable form for ``DetectionReport.all_signals_raw``."""
        return {
            "probe_id": self.probe_id,
            "source": self.source,
            "scope": self.scope,
            "fired": self.fired,
            "inferred_version": self.inferred_version,
            "walnut_path": self.walnut_path,
            "detail": self.detail,
        }
