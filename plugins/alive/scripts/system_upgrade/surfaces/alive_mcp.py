"""``alive-mcp`` surface (T7 of fn-18).

Implements the ``Surface`` contract for the alive-mcp companion CLI.

Probe contract -- ``alive-mcp --version --json`` is the v0.2 API
obligation locked here. The current alive-mcp v0.1 ships
``--version`` (text-only); the JSON variant ships in v0.2 (per
``alive-mcp/CHANGELOG.md`` § Deferred to v0.2). Until v0.2 ships,
probe will hit the soft-fail "parse_error" branch on the v0.1 text
output -- that's the design: an honest non-raising probe + an
actionable warning telling the operator to upgrade.

Dispatch contract -- ``alive-mcp upgrade --world <path> --json
[--retry-items <json>]``. Same JSON envelope as documented in the
epic spec (``status: ok|partial|failed``, ``completed[]``,
``needs_retry[]``, ``errors[]``).

Stdlib-only.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, Sequence

from system_upgrade.orchestrator import ProbeError, ProbeResult
from system_upgrade.surfaces._base import (
    DEFAULT_SUBPROCESS_TIMEOUT,
    DispatchResult,
    MigratorArgvPrefixInvalid,
    Surface,
    build_dispatch_argv,
    parse_version_json,
    run_subprocess_capture,
    truncate_for_record,
)


__all__ = ("AliveMcpSurface", "MIN_RECOMMENDED_VERSION", "UPGRADE_HINT")


def _version_below_floor(probed: str, floor: str) -> bool:
    """Return True iff ``probed < floor`` under semver comparison.

    Tolerates short forms (``"0.2"`` vs ``"0.2.0"``) and a leading
    ``"v"``. Falls back to False (no warning) on any parse failure
    so a probe with an unusual but compatible-by-the-surface's-own
    judgment version doesn't get a spurious warning.
    """
    try:
        from system_upgrade import _normalize_version
    except ImportError:  # pragma: no cover -- defensive
        return False
    try:
        return _normalize_version(probed) < _normalize_version(floor)
    except (ValueError, TypeError):
        return False


#: alive-mcp version that first ships the ``--version --json`` +
#: ``upgrade --world ... --json`` migrator contract.
MIN_RECOMMENDED_VERSION: str = "0.2.0"


#: Operator-facing actionable warning text. Verified install method:
#: alive-mcp ships as a hatchling-built Python package
#: (``[project.scripts] alive-mcp = "alive_mcp.__main__:main"``); the
#: install vector is pip per its CHANGELOG (``Real PyPI publication
#: held for explicit decision`` -- so for now a local install).
UPGRADE_HINT: str = (
    'pip install "alive-mcp>={}"'.format(MIN_RECOMMENDED_VERSION)
)


class AliveMcpSurface(Surface):
    """alive-mcp surface implementation."""

    name = "alive-mcp"

    #: Executable name resolved on PATH. Tests can monkeypatch the
    #: instance attribute or pass a ``binary=`` constructor kwarg to
    #: invoke a fake binary.
    def __init__(self, binary: str = "alive-mcp") -> None:
        self.binary = binary

    # ------------------------------------------------------------------
    # Phase 4: probe
    # ------------------------------------------------------------------

    def probe(self) -> ProbeResult:
        outcome = run_subprocess_capture(
            [self.binary, "--version", "--json"],
            timeout=DEFAULT_SUBPROCESS_TIMEOUT,
        )
        # Always record the captured subprocess outcome regardless of
        # outcome -- the per-run record needs to surface the diagnostic
        # via ``probe_subprocess.stdout`` / ``stderr`` etc.
        base = ProbeResult(
            name=self.name,
            present=not outcome.missing_binary,
            compatible=False,
            probe_subprocess=outcome,
        )
        if outcome.missing_binary:
            base.probe_error = ProbeError(
                kind="missing_binary",
                message=(
                    "alive-mcp binary not found on PATH; install via "
                    + UPGRADE_HINT
                ),
            )
            return base
        if outcome.timed_out:
            base.probe_error = ProbeError(
                kind="timeout",
                message=(
                    "alive-mcp --version --json timed out "
                    "({}s)".format(DEFAULT_SUBPROCESS_TIMEOUT)
                ),
            )
            return base
        if outcome.returncode != 0:
            base.probe_error = ProbeError(
                kind="non_zero_exit",
                message=(
                    "alive-mcp --version --json exited {}; stderr "
                    "snippet: {!r}".format(
                        outcome.returncode, outcome.stderr[:200]
                    )
                ),
            )
            return base
        # Parse + validate the JSON envelope.
        try:
            payload = parse_version_json(outcome.stdout)
        except MigratorArgvPrefixInvalid as exc:
            base.probe_error = ProbeError(
                kind="migrator_argv_prefix_invalid",
                message=str(exc),
            )
            base.version = None
            base.compatible = False
            return base
        except ValueError as exc:
            # Includes ordinary JSON parse failures + shape errors.
            base.probe_error = ProbeError(
                kind="parse_error",
                message=(
                    "alive-mcp --version --json output failed "
                    "validation: {}; install via {}".format(
                        exc, UPGRADE_HINT
                    )
                ),
            )
            return base
        # Success.
        base.version = payload["version"]
        base.compatible = bool(payload["compatible"])
        base.state_paths = list(payload["state_paths"])
        base.migrator_argv_prefix = list(payload["migrator_argv_prefix"])
        # Even when the surface reports compatible=True, surface a
        # non-hard-fail diagnostic when the parsed version is below
        # MIN_RECOMMENDED_VERSION so the operator sees the actionable
        # install command. Use kind="not_yet_shipped" -- it's a soft
        # signal that the no-op gate ignores (the surface IS reachable
        # and compatible from its own perspective; the warning is
        # advisory). This satisfies the spec's "actionable warnings"
        # acceptance bullet for alive-mcp < MIN_RECOMMENDED_VERSION.
        if _version_below_floor(base.version, MIN_RECOMMENDED_VERSION):
            base.probe_error = ProbeError(
                kind="not_yet_shipped",
                message=(
                    "alive-mcp version {} is below the recommended "
                    "floor ({}); upgrade via: {}".format(
                        base.version, MIN_RECOMMENDED_VERSION,
                        UPGRADE_HINT,
                    )
                ),
            )
        return base

    # ------------------------------------------------------------------
    # Phase 10: dispatch
    # ------------------------------------------------------------------

    def dispatch(
        self,
        world_root_resolved: str,
        retry_items: Optional[List[Any]] = None,
        timeout: Optional[float] = None,
        migrator_argv_prefix: Optional[Sequence[str]] = None,
    ) -> DispatchResult:
        # Defensive: dispatch_all should never call us without a
        # validated prefix. If it does, refuse rather than build a
        # bogus argv.
        if migrator_argv_prefix is None:
            return DispatchResult(
                name=self.name,
                status="failed",
                error_kind="migrator_argv_prefix_invalid",
                error_message=(
                    "dispatch invoked without migrator_argv_prefix; "
                    "probe should have set compatible=False"
                ),
            )
        argv = build_dispatch_argv(
            list(migrator_argv_prefix),
            world_root_resolved=world_root_resolved,
            retry_items=retry_items,
        )
        outcome = run_subprocess_capture(
            argv,
            timeout=(
                timeout if timeout is not None else DEFAULT_SUBPROCESS_TIMEOUT
            ),
        )
        result = DispatchResult(
            name=self.name,
            argv=list(argv),
            dispatch_stdout=outcome.stdout,
            dispatch_stdout_bytes=outcome.stdout_bytes,
            dispatch_stdout_truncated=outcome.stdout_truncated,
            dispatch_stderr=outcome.stderr,
            dispatch_stderr_bytes=outcome.stderr_bytes,
            dispatch_stderr_truncated=outcome.stderr_truncated,
        )
        if outcome.missing_binary:
            result.status = "failed"
            result.error_kind = "missing_binary"
            result.error_message = (
                "alive-mcp binary not found on PATH; install via "
                + UPGRADE_HINT
            )
            return result
        if outcome.timed_out:
            result.status = "failed"
            result.error_kind = "timeout"
            result.error_message = (
                "alive-mcp upgrade timed out (>{}s)".format(
                    timeout if timeout is not None
                    else DEFAULT_SUBPROCESS_TIMEOUT
                )
            )
            return result
        if outcome.returncode != 0:
            result.status = "failed"
            result.error_kind = "non_zero_exit"
            result.error_message = (
                "alive-mcp upgrade exited {}; stderr snippet: {!r}".format(
                    outcome.returncode, outcome.stderr[:200]
                )
            )
            return result
        # Parse the migrator's response envelope.
        try:
            payload = json.loads(outcome.stdout)
        except json.JSONDecodeError as exc:
            result.status = "failed"
            result.error_kind = "parse_error"
            result.error_message = (
                "alive-mcp upgrade stdout is not valid JSON: {}".format(exc)
            )
            return result
        if not isinstance(payload, dict):
            result.status = "failed"
            result.error_kind = "parse_error"
            result.error_message = (
                "alive-mcp upgrade payload is not a JSON object; got "
                "{}".format(type(payload).__name__)
            )
            return result
        status_raw = payload.get("status")
        if status_raw not in ("ok", "partial", "failed"):
            result.status = "failed"
            result.error_kind = "parse_error"
            result.error_message = (
                "alive-mcp upgrade returned invalid status "
                "{!r}".format(status_raw)
            )
            return result
        # Validate completed / needs_retry / errors STRICTLY. A
        # malformed payload that defaults missing fields to ``[]``
        # makes a status="partial" response look authoritative-but-
        # empty, which tells build_surfaces_record_section to ABANDON
        # the carry-forward retry items. That silently loses retry
        # state. Reject malformed payloads with a parse_error and
        # let carry-forward preserve the prior retry items.
        for field_name in ("completed", "needs_retry", "errors"):
            if field_name not in payload:
                result.status = "failed"
                result.error_kind = "parse_error"
                result.error_message = (
                    "alive-mcp upgrade payload missing required "
                    "field {!r}".format(field_name)
                )
                return result
            if not isinstance(payload[field_name], list):
                result.status = "failed"
                result.error_kind = "parse_error"
                result.error_message = (
                    "alive-mcp upgrade payload field {!r} must be a "
                    "list (got {})".format(
                        field_name, type(payload[field_name]).__name__
                    )
                )
                return result
        result.status = status_raw
        result.completed = list(payload["completed"])
        result.needs_retry = list(payload["needs_retry"])
        result.errors = list(payload["errors"])
        return result
