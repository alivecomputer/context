"""Plugin tool version reader (T3 of fn-18).

Reads ``plugin.json`` to capture the *tool* version -- the migrator's
own version, NOT the world source version. Consumed by T6's resume
validation; never participates in world-version inference.

The strict architectural distinction (epic § Tool version vs world
version) is enforced two ways:

* This module is the only place in the system_upgrade package that
  reads ``plugin.json`` for a version field. ``version_detect.py``
  must NOT import ``plugin.json`` at all -- it imports this module
  and stores the result on ``DetectionReport.tool_version_at_run``,
  separate from the world-fingerprint signals.

* The reader uses ``json.loads`` against the live file (NOT the
  start-of-run snapshot). It runs once at run start in phase 2 and
  the result is threaded through; T6 reads the same value from the
  detection report rather than re-reading the file.

Failure modes:

* Missing manifest        -> returns ``"unknown"`` (lock-meta sidecar
                              already tolerates this).
* Malformed JSON          -> returns ``"unknown"`` (defensive; resume
                              validation surfaces the skew elsewhere).
* Missing ``version`` key  -> returns ``"unknown"``.

Stdlib-only (R10): no PyYAML / ruamel.
"""

from __future__ import annotations

import json
import os
from typing import Optional


__all__ = ("read_tool_version",)


def read_tool_version(plugin_root: str) -> str:
    """Return the plugin's declared version, or ``"unknown"`` on any error.

    The plugin manifest sits at ``<plugin_root>/.claude-plugin/plugin.json``
    (canonical layout per the Claude Code plugin contract). Errors are
    swallowed by design: a system-upgrade run that cannot read its own
    manifest must still complete (the resume-skew check is informational,
    not load-bearing for first-run correctness).
    """
    if not plugin_root:
        return "unknown"
    manifest = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "unknown"
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version:
        return "unknown"
    return version
