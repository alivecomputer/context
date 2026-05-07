"""Surface protocol + shared subprocess primitives (T7 of fn-18).

Each registered surface (alive-mcp, hermes, codex) implements the
``Surface`` protocol with two phase-distinct callables:

* ``probe()`` -- runs in phase 4. Detects presence + reads
  ``<surface> --version --json`` to learn the surface's version,
  compatibility, owned state paths, and the argv prefix the
  orchestrator should append to when dispatching the migrator. Probe
  is a READ-only operation: it MUST NOT mutate the world or invoke
  the migrator. Soft-fails on parse / non-zero / timeout / missing
  binary -- never raises.

* ``dispatch(world_root_resolved, retry_items=None)`` -- runs in
  phase 10. Invokes the migrator subprocess via the orchestrator's
  validated normative tail (``["--world", world_root_resolved,
  "--json"]`` plus optional ``["--retry-items", json.dumps(items)]``).
  Soft-fails on parse / non-zero / timeout / missing binary -- never
  raises.

This module is the canonical home for:

* The ``Surface`` Protocol class (phase contract).
* ``ProbeResult`` / ``DispatchResult`` dataclasses surfaces produce.
  (``ProbeResult`` is re-exported from ``orchestrator.py`` for back-
  compat with the no-op gate's existing imports; both modules expose
  the same dataclass.)
* ``MigratorArgvPrefixInvalid`` validation predicate
  (placeholder-free, list[str], no ``--world``).
* ``run_subprocess_capture`` -- the canonical subprocess wrapper that
  every surface uses. ``shell=False`` is non-negotiable; output
  capture follows the bible's Pure-JSON stdout truncation contract
  (2000 chars, byte length pre-decode, truncated flag).
* ``parse_version_json`` -- shared parser for the
  ``<surface> --version --json`` payload contract.

Stdlib-only (R10).
"""

from __future__ import annotations

import json
import os.path
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Re-exported from orchestrator so surface implementers + tests can
# import everything from one place. The dataclass definitions live in
# orchestrator.py because the no-op-gate code there has used them since
# T1; T7 extends the shape with default-valued fields rather than
# splitting the class.
from system_upgrade.orchestrator import ProbeError, ProbeResult


__all__ = (
    "Surface",
    "NotYetShippedSurface",
    "ProbeError",
    "ProbeResult",
    "DispatchResult",
    "STDOUT_TRUNCATION_CHARS",
    "DEFAULT_SUBPROCESS_TIMEOUT",
    "MigratorArgvPrefixInvalid",
    "validate_migrator_argv_prefix",
    "run_subprocess_capture",
    "SubprocessOutcome",
    "parse_version_json",
    "build_dispatch_argv",
    "truncate_for_record",
)


#: Pure-JSON stdout truncation contract -- 2000 chars (post-decode,
#: char-based, NOT byte-based) per the bible. Stderr follows the same
#: cap.
STDOUT_TRUNCATION_CHARS: int = 2000

#: Default timeout for both probe and dispatch subprocesses. 5s for
#: probe per spec; dispatch runs longer migrators so a different
#: caller-side timeout is plumbed via the ``timeout`` kwarg of
#: ``run_subprocess_capture``.
DEFAULT_SUBPROCESS_TIMEOUT: float = 5.0


#: Placeholder regex used by ``validate_migrator_argv_prefix``. Matches
#: any element shaped like ``"<word>"`` -- ``"<path>"``, ``"<world>"``,
#: ``"<root>"``, etc. Refusal mode for surfaces that didn't receive
#: round 9's argv-prefix change.
_PLACEHOLDER_RE = re.compile(r"<[a-zA-Z_]+>")


# ---------------------------------------------------------------------------
# Surface protocol + result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """Phase-10 per-surface dispatch result.

    Mirrors the migrator's wire-level JSON contract but adds
    orchestrator-side bookkeeping (raw stdout/stderr capture under
    the truncation contract, ``argv`` for forensic replay, total
    elapsed wall time).

    Surfaces produce this in ``Surface.dispatch``; the orchestrator
    aggregates them into the upgrade record at phase 12.

    Attributes
    ----------
    name : str
        Surface name; matches the corresponding ``ProbeResult.name``.
    status : str
        ``"ok" | "partial" | "failed" | "skipped"``.
        ``skipped`` is reserved for the orchestrator (e.g. when a
        ``ProbeResult`` was incompatible and dispatch was refused);
        surfaces never return ``skipped`` themselves.
    completed : list
        Migrator-defined items that the migrator reports as completed
        successfully on this run. Opaque to the orchestrator.
    needs_retry : list
        Migrator-defined items the migrator reports as needing retry
        on the next run. Each item shape is surface-specific.
    errors : list
        Structured error entries the migrator reported (string,
        ``{item, reason}`` dict, or anything the surface emitted).
    version_at_retry : str | None
        Required (and set by the orchestrator before recording) when
        ``needs_retry`` is non-empty. The surface itself does not set
        this -- the orchestrator copies it from the corresponding
        ``ProbeResult.version`` so the next run can run the version-
        mismatch stale-drop.
    argv : list[str]
        The full argv the orchestrator dispatched (after appending
        ``["--world", world_root_resolved, "--json"]`` and any
        retry-items tail). Recorded for forensic replay.
    dispatch_stdout : str
        UTF-8-decoded migrator stdout, truncated to 2000 chars.
    dispatch_stdout_bytes : int
        Original byte length of stdout BEFORE decode + truncation.
    dispatch_stdout_truncated : bool
        True iff ``dispatch_stdout`` was truncated.
    dispatch_stderr : str
    dispatch_stderr_bytes : int
    dispatch_stderr_truncated : bool
        Same triple for stderr.
    error_kind : str | None
        When ``status == "failed"``, the ``ProbeError.kind``-style
        marker (``parse_error`` / ``non_zero_exit`` / ``timeout`` /
        ``missing_binary`` / ``migrator_argv_prefix_invalid``).
    error_message : str
        Human-readable diagnostic paired with ``error_kind``.
    """

    name: str
    status: str = "ok"  # ok | partial | failed | skipped
    completed: List[Any] = field(default_factory=list)
    needs_retry: List[Any] = field(default_factory=list)
    errors: List[Any] = field(default_factory=list)
    version_at_retry: Optional[str] = None
    argv: List[str] = field(default_factory=list)
    dispatch_stdout: str = ""
    dispatch_stdout_bytes: int = 0
    dispatch_stdout_truncated: bool = False
    dispatch_stderr: str = ""
    dispatch_stderr_bytes: int = 0
    dispatch_stderr_truncated: bool = False
    error_kind: Optional[str] = None
    error_message: str = ""


class Surface:
    """Surface contract.

    Concrete surfaces subclass this and implement ``probe()`` and
    ``dispatch(world_root_resolved, retry_items=None)``. Both methods
    MUST be soft-fail: never raise on subprocess errors; capture the
    outcome into the returned dataclass and let the orchestrator
    aggregate.

    The Protocol-style typing in PEP 544 would be slightly cleaner,
    but a regular base class is more compatible with the
    ``stdlib-only, Python 3.9 floor`` constraint in the rest of the
    plugin (and with ``dataclasses`` typing in this codebase).
    """

    #: Stable surface identifier (e.g. ``"alive-mcp"``). Subclasses
    #: override.
    name: str = "unknown"

    def probe(self) -> "ProbeResult":  # pragma: no cover -- abstract
        raise NotImplementedError(
            "Surface subclass must implement probe()"
        )

    def dispatch(
        self,
        world_root_resolved: str,
        retry_items: Optional[List[Any]] = None,
        timeout: Optional[float] = None,
    ) -> "DispatchResult":  # pragma: no cover -- abstract
        raise NotImplementedError(
            "Surface subclass must implement dispatch()"
        )


class NotYetShippedSurface(Surface):
    """Parametric stub surface for surfaces that are not yet shipped.

    Single-source-of-truth for the detect-only / not-yet-shipped
    surface stub pattern. Concrete stubs (``HermesSurface`` /
    ``CodexSurface``) are now thin zero-arg subclasses that bind the
    surface ``name`` (and an optional handoff message) into the shared
    body below. Probe always reports ``compatible=False`` with a
    ``ProbeError(kind="not_yet_shipped")``; dispatch always returns
    ``status="skipped"``. The no-op gate treats ``not_yet_shipped`` as
    a non-hard-fail soft signal so an already-current world can still
    short-circuit.
    """

    #: Default handoff message for surfaces that don't override it.
    DEFAULT_HANDOFF_MESSAGE: str = (
        "{name} surface is not yet shipped; nothing to dispatch"
    )

    def __init__(
        self,
        name: str,
        handoff_message: Optional[str] = None,
    ) -> None:
        self.name = name
        self.handoff_message = (
            handoff_message
            if handoff_message is not None
            else self.DEFAULT_HANDOFF_MESSAGE.format(name=name)
        )

    def probe(self) -> "ProbeResult":
        return ProbeResult(
            name=self.name,
            present=False,
            compatible=False,
            version=None,
            state_paths=[],
            migrator_argv_prefix=None,
            probe_error=ProbeError(
                kind="not_yet_shipped",
                message=self.handoff_message,
            ),
        )

    def dispatch(
        self,
        world_root_resolved: str,
        retry_items: Optional[List[Any]] = None,
        timeout: Optional[float] = None,
        migrator_argv_prefix: Optional[Sequence[str]] = None,
    ) -> "DispatchResult":
        return DispatchResult(
            name=self.name,
            status="skipped",
            error_kind="not_yet_shipped",
            error_message=self.handoff_message,
        )


# ---------------------------------------------------------------------------
# Migrator argv-prefix validation
# ---------------------------------------------------------------------------


class MigratorArgvPrefixInvalid(ValueError):
    """Raised internally by ``validate_migrator_argv_prefix``.

    The surface contract REJECTS:

    * Non-list types (string, dict, None) for ``migrator_argv_prefix``.
    * Empty list or list with non-string / empty-string elements.
    * Any element matching the placeholder regex (``"<path>"``,
      ``"<world>"``, ``"<root>"``, etc).
    * Any element equal to ``"--world"`` -- the orchestrator owns
      that flag and a surface that pre-populates it is suspicious.

    Caught by ``probe_all`` and converted into a
    ``ProbeError(kind="migrator_argv_prefix_invalid")`` on the
    affected ``ProbeResult``; the result is marked
    ``compatible=False`` and dispatch is refused.
    """


def validate_migrator_argv_prefix(value: Any) -> List[str]:
    """Validate ``migrator_argv_prefix`` against the placeholder-free
    contract and return the canonical ``list[str]``.

    Raises ``MigratorArgvPrefixInvalid`` on any violation; the caller
    (``probe_all``) converts the exception into a soft-fail
    ``ProbeError`` on the corresponding ``ProbeResult``.
    """
    if not isinstance(value, list):
        raise MigratorArgvPrefixInvalid(
            "migrator_argv_prefix must be a list of strings, got "
            "{} ({!r})".format(type(value).__name__, value)
        )
    if not value:
        raise MigratorArgvPrefixInvalid(
            "migrator_argv_prefix must not be empty"
        )
    for idx, element in enumerate(value):
        if not isinstance(element, str):
            raise MigratorArgvPrefixInvalid(
                "migrator_argv_prefix[{}] must be a string, got "
                "{} ({!r})".format(idx, type(element).__name__, element)
            )
        if not element:
            raise MigratorArgvPrefixInvalid(
                "migrator_argv_prefix[{}] must not be empty".format(idx)
            )
        if _PLACEHOLDER_RE.search(element):
            raise MigratorArgvPrefixInvalid(
                "migrator_argv_prefix[{}] = {!r} contains a "
                "placeholder; the orchestrator owns the world path".format(
                    idx, element
                )
            )
        if element == "--world":
            raise MigratorArgvPrefixInvalid(
                "migrator_argv_prefix[{}] = '--world'; the "
                "orchestrator owns that flag and surfaces must not "
                "pre-populate it".format(idx)
            )
    return list(value)


def build_dispatch_argv(
    prefix: Sequence[str],
    world_root_resolved: str,
    retry_items: Optional[List[Any]] = None,
) -> List[str]:
    """Return the full dispatch argv: prefix + normative tail.

    The normative tail is
    ``["--world", world_root_resolved, "--json"]``. When retry-items
    are present, the orchestrator also appends
    ``["--retry-items", json.dumps(items)]`` (separate list elements,
    NOT a single concatenated string).
    """
    argv: List[str] = list(prefix)
    argv.extend(["--world", str(world_root_resolved), "--json"])
    if retry_items:
        # Use compact separators so the JSON arg is a single token --
        # but stays human-readable. ``sort_keys=False`` because retry
        # items are migrator-opaque and the orchestrator does not
        # impose canonical ordering on them.
        argv.extend(
            ["--retry-items", json.dumps(retry_items, separators=(",", ":"))]
        )
    return argv


# ---------------------------------------------------------------------------
# Subprocess capture (Pure-JSON stdout contract)
# ---------------------------------------------------------------------------


@dataclass
class SubprocessOutcome:
    """Outcome of a probe or dispatch subprocess invocation.

    Carries enough state for the caller to (a) parse stdout, (b) map
    soft-fail kinds, (c) record stdout/stderr under the truncation
    contract, and (d) compute the corresponding ``ProbeError`` /
    ``DispatchResult.error_kind``.

    Attributes
    ----------
    returncode : int | None
        Subprocess exit code; ``None`` on timeout (process killed
        before exit) or missing-binary (process never started).
    stdout : str
        UTF-8-decoded stdout, truncated to 2000 chars.
    stdout_bytes : int
        Original byte length pre-decode + pre-truncation.
    stdout_truncated : bool
    stderr : str
    stderr_bytes : int
    stderr_truncated : bool
    timed_out : bool
        True iff the subprocess hit ``timeout`` and was killed.
    missing_binary : bool
        True iff the executable was not found on PATH
        (FileNotFoundError from subprocess).
    error_kind : str | None
        Convenience: ``"timeout"`` / ``"missing_binary"`` / None. The
        caller layers parse / non-zero classification on top.
    """

    returncode: Optional[int] = None
    stdout: str = ""
    stdout_bytes: int = 0
    stdout_truncated: bool = False
    stderr: str = ""
    stderr_bytes: int = 0
    stderr_truncated: bool = False
    timed_out: bool = False
    missing_binary: bool = False
    error_kind: Optional[str] = None


def truncate_for_record(raw: bytes) -> Tuple[str, int, bool]:
    """Decode UTF-8 (errors=replace), truncate to 2000 chars.

    Returns ``(decoded_str, original_byte_len, truncated_bool)``.
    ``raw`` may be ``None`` (treated as empty) so callers don't have to
    guard.
    """
    if raw is None:
        return "", 0, False
    byte_len = len(raw)
    decoded = raw.decode("utf-8", errors="replace")
    if len(decoded) > STDOUT_TRUNCATION_CHARS:
        return decoded[:STDOUT_TRUNCATION_CHARS], byte_len, True
    return decoded, byte_len, False


def run_subprocess_capture(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,
) -> SubprocessOutcome:
    """Run ``argv`` and return the captured outcome.

    Contract (per task spec + bible Pure-JSON stdout convention):

    * ``shell=False`` -- non-negotiable. Never use ``shell=True``,
      ``os.system``, or ``subprocess.call(<string>)``. T14's audit
      grep enforces zero hits inside ``surfaces/``.
    * ``capture_output=True`` -- both stdout and stderr captured into
      bytes, never piped to the orchestrator's own stdout (that would
      corrupt the orchestrator's own JSON envelope).
    * On timeout: the subprocess is killed and ``communicate()`` is
      called a second time to drain whatever was buffered before the
      kill, so the partial stdout/stderr are recorded.
    * On FileNotFoundError (missing binary): returns an outcome with
      ``missing_binary=True`` and empty stdout/stderr.

    The caller is responsible for layering parse / non-zero
    classification on top of this return value.
    """
    # FileNotFoundError must surface as missing_binary -- subprocess.run
    # raises before any stream exists. Use Popen so we can drain on
    # timeout; communicate() handles buffer reads + kill semantics.
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is validated
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as exc:
        # Binary missing; nothing to capture.
        return SubprocessOutcome(
            returncode=None,
            missing_binary=True,
            error_kind="missing_binary",
            stderr=str(exc),
            stderr_bytes=len(str(exc).encode("utf-8")),
        )
    except OSError as exc:
        # Permission errors etc. surface as missing_binary too; the
        # operator's diagnostic is the same shape.
        return SubprocessOutcome(
            returncode=None,
            missing_binary=True,
            error_kind="missing_binary",
            stderr=str(exc),
            stderr_bytes=len(str(exc).encode("utf-8")),
        )

    try:
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            # Kill + drain the partial buffer per spec. communicate()
            # called a second time after kill returns whatever was
            # buffered before the kill; surface it under the
            # truncation contract.
            proc.kill()
            try:
                stdout_b, stderr_b = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                stdout_b, stderr_b = b"", b""
            # Wait for the process to actually exit so its returncode
            # is set and the OS doesn't leave a zombie. communicate()
            # already calls wait() internally on success; we add an
            # explicit wait() in case the second communicate() raised
            # without setting returncode.
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
            timed_out = True
    finally:
        # Explicitly close the captured streams. communicate() reads
        # them to completion but on Python 3.14 the BufferedReader
        # objects can outlive the process, triggering ResourceWarning
        # under -Wd. Close defensively.
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    out_str, out_bytes, out_trunc = truncate_for_record(stdout_b)
    err_str, err_bytes, err_trunc = truncate_for_record(stderr_b)

    return SubprocessOutcome(
        returncode=proc.returncode if not timed_out else None,
        stdout=out_str,
        stdout_bytes=out_bytes,
        stdout_truncated=out_trunc,
        stderr=err_str,
        stderr_bytes=err_bytes,
        stderr_truncated=err_trunc,
        timed_out=timed_out,
        missing_binary=False,
        error_kind="timeout" if timed_out else None,
    )


# ---------------------------------------------------------------------------
# --version --json payload parser (R11 contract)
# ---------------------------------------------------------------------------


def parse_version_json(stdout: str) -> Mapping[str, Any]:
    """Parse a surface's ``--version --json`` stdout.

    Validates the locked contract (R11):

    * Top-level value is a JSON object.
    * ``version`` is a non-empty string.
    * ``compatible`` is a bool.
    * ``state_paths`` is a list of strings.
    * ``migrator_argv_prefix`` passes ``validate_migrator_argv_prefix``.

    On any violation raises ``ValueError`` with a diagnostic; the
    caller (``probe_all``) converts the failure into a soft-fail
    ``ProbeError(kind="parse_error")`` on the corresponding
    ``ProbeResult``.

    Returns the validated dict (with ``migrator_argv_prefix`` already
    materialized into a fresh ``list[str]``).
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "stdout is not valid JSON: {}".format(exc)
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            "expected JSON object, got {}".format(type(data).__name__)
        )
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(
            "missing/invalid 'version' field (got {!r})".format(version)
        )
    compatible = data.get("compatible")
    if not isinstance(compatible, bool):
        raise ValueError(
            "missing/invalid 'compatible' field (got {!r})".format(compatible)
        )
    if "state_paths" not in data:
        # Required field: distinguishes "this surface explicitly owns
        # no state" (``[]``) from "this surface is too old / broken
        # to report its owned state" (missing). Phase 8 cleanup
        # relies on a deterministic answer here -- a missing field
        # would otherwise let a buggy surface advertise zero protected
        # paths and have its state silently swept.
        raise ValueError(
            "missing required 'state_paths' field; surface must "
            "advertise [] explicitly when it owns no state"
        )
    state_paths = data.get("state_paths")
    if not isinstance(state_paths, list) or not all(
        isinstance(p, str) and p for p in state_paths
    ):
        raise ValueError(
            "'state_paths' must be a list of non-empty strings "
            "(got {!r})".format(state_paths)
        )
    # Each element must be an absolute path. Phase-8 cleanup builds
    # its sweep-exclusion union from these strings; relative entries
    # cannot reliably match real filesystem locations and would
    # silently leave surface-owned state unprotected.
    for entry in state_paths:
        if not os.path.isabs(entry):
            raise ValueError(
                "'state_paths' entry {!r} must be an absolute path; "
                "the cleanup sweep cannot match relative paths "
                "against the resolved world root".format(entry)
            )
    prefix_raw = data.get("migrator_argv_prefix")
    # Raises MigratorArgvPrefixInvalid (a ValueError subclass).
    prefix = validate_migrator_argv_prefix(prefix_raw)
    return {
        "version": version.strip(),
        "compatible": compatible,
        "state_paths": list(state_paths),
        "migrator_argv_prefix": prefix,
    }
