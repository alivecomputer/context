"""``alive system-upgrade`` -- argparse subcommand registration.

T1 ships the full CLI surface (every flag the redesign needs). Phase
implementations are owned by T3-T11; T1's ``handle()`` dispatches to
the orchestrator skeleton, runs preflight, and exits cleanly when the
no-op short-circuit fires (using the orchestrator's gate). For phases
that remain stubs, ``handle()`` reports a structured "phase not
implemented" message and exits 1 -- so any premature CLI call against
T1 fails loud rather than silently passing.

The ``register(subparsers)`` function follows the existing
``_SUBCOMMANDS`` convention. ``SCHEMA_METADATA`` is attached so
``alive schema system-upgrade`` introspects the new subcommand
cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional


__all__ = ("SCHEMA_METADATA", "register", "handle")


SCHEMA_METADATA: Dict[str, Any] = {
    "description": (
        "Upgrade an ALIVE world from any prior version to the current "
        "target. Handles intra-v3 minor migrations (v3.0->v3.1, "
        "v3.1->v3.2), retroactive version detection, multi-surface "
        "orchestration, walkthrough user-extension migration, "
        "world-state cleanup, partial-failure resume, and dry-run "
        "previews."
    ),
    "stdout_shape": {
        "ok": "bool",
        "exit_code": "int",
        "error_code": (
            "str|null -- routing surface for refusal subcategories "
            "(unsafe_target:..., dirty_stash, syncthing_active, "
            "half_sync_marker, boundary_violation:..., "
            "submodule_mount_refused, missing_world, ...)"
        ),
        "error": "str|null -- human-readable explanation when ok is false",
        "world_root": "str|null -- resolved (realpath) target world root",
        "phase_reached": (
            "str|null -- name of the last phase that ran "
            "(preflight | snapshot | ... | release)"
        ),
        "noop_short_circuit": "bool -- true when phase 5 fired",
    },
    "exit_codes": {
        "0": "success",
        "1": (
            "general failure / preflight refusal / unsafe target / "
            "boundary violation / dirty state (subcategory in "
            "error_code)"
        ),
        "2": "usage error (bad flags, mutex violation)",
        "3": (
            "not found (target world missing, resume marker absent, "
            "rollback timestamp not found)"
        ),
        "4": "permission (filesystem permission errors)",
        "5": "lock contention (UpgradeLockBusy)",
    },
    "examples": [
        {
            "input": "alive system-upgrade --dry-run --plan-output /tmp/p.txt",
            "output_excerpt": (
                '{"ok": true, "noop_short_circuit": false, ...}'
            ),
        },
        {
            "input": "alive system-upgrade /path/to/legacy-v1-world",
            "output_excerpt": (
                '{"ok": true, "world_root": "/path/to/...", '
                '"phase_reached": "release"}'
            ),
        },
    ],
}


# Help text for the legacy-aware-resolver fallback. Mirrored into the
# argparse description so ``--help`` documents the behaviour.
_LEGACY_RESOLVER_HELP = (
    "Optional positional path of the world to upgrade. May be absolute or "
    "relative to cwd. Mutually exclusive with --world-root. When neither "
    "is supplied, system-upgrade walks up from cwd looking for a "
    "high-confidence world marker (.alive/, two canonical numbered "
    "domain dirs, .walnut/, _core/+companion.md, _core/+now.md, or the "
    "companion+now+tasks triple); the first hit wins. Un-numbered legacy "
    "domains (lowercase archive/, life/, ventures/, ...) are NOT "
    "auto-detected -- pass --world-root explicitly for those."
)


def _add_args(parser: argparse.ArgumentParser) -> None:
    """Attach every flag in the redesign's CLI surface."""
    # World target (positional + --world-root, mutually exclusive).
    parser.add_argument(
        "world_path",
        nargs="?",
        default=None,
        help=_LEGACY_RESOLVER_HELP,
        metavar="<world-path>",
    )
    parser.add_argument(
        "--world-root",
        dest="world_root",
        default=None,
        help=(
            "Explicit target world root path. Mutually exclusive with "
            "the positional <world-path>. Required for un-numbered "
            "legacy domain layouts (auto-detection refuses to guess for "
            "destructive operations on legacy folder shapes)."
        ),
    )

    # Dry-run + plan-output.
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help=(
            "Read-only after containment, with three narrow allowed "
            "writes (lock + lock-meta inside .alive/, optional .alive/ "
            "dir creation as a precondition, --plan-output plan file). "
            "Lock files are released at phase 13."
        ),
    )
    parser.add_argument(
        "--plan-output",
        dest="plan_output",
        default=None,
        help=(
            "Path for the dry-run plan file. Required when --dry-run "
            "is supplied without --json."
        ),
    )

    # Resume / force-run.
    parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=False,
        help=(
            "Resume a partial-failure run from the most-recent "
            "*-resume.yaml marker. Refuses on tool_version skew "
            "(NOT bypassed by --force) and on world-state divergence."
        ),
    )
    parser.add_argument(
        "--force",
        dest="force",
        action="store_true",
        default=False,
        help=(
            "Bypass world-state divergence on resume. Does NOT bypass "
            "tool_version_at_run skew (that is a hard refusal) and "
            "does NOT bypass any preflight guard."
        ),
    )
    parser.add_argument(
        "--force-run",
        dest="force_run",
        action="store_true",
        default=False,
        help=(
            "Bypass the phase-5 no-op short-circuit so already-current "
            "worlds still re-emit verify + record. Does NOT bypass any "
            "preflight guard."
        ),
    )
    parser.add_argument(
        "--assume-empty-world",
        dest="assume_empty_world",
        action="store_true",
        default=False,
        help=(
            "Phase-3 detection: bypass the _kernel/ requirement when "
            "fingerprint signals are unanimous-empty (T3 owns the gate)."
        ),
    )

    # Verbose.
    parser.add_argument(
        "-v", "--verbose",
        dest="verbose",
        action="count",
        default=0,
        help="Increase progress verbosity (-vv is step-level).",
    )

    # Non-interactive + ext-migration.
    parser.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        default=False,
        help=(
            "Skip every TTY prompt. Combined with --unsafe-confirm-target "
            "to bypass home/cloud confirm-required gates without a "
            "type-back loop."
        ),
    )
    parser.add_argument(
        "--ext-migration",
        dest="ext_migration",
        choices=("skip", "backup-only", "rewrite", "abort"),
        default=None,
        help=(
            "Walkthrough user-extension migration policy when running "
            "--non-interactive. skip = no rewrites. backup-only = "
            "write .bak.<ts> sibling but leave the original untouched. "
            "rewrite = write .bak.<ts> AND rewrite the original to the "
            "catalog's replacement (default in non-interactive mode "
            "when this flag is omitted). abort = refuse on any "
            "retired-pattern hit (pass explicitly to opt into hard "
            "refusal)."
        ),
    )

    # Surfaces.
    parser.add_argument(
        "--surfaces",
        dest="surfaces",
        default="all",
        help=(
            "Surface dispatch policy. 'all' (default) probes + "
            "dispatches every known surface (alive-mcp, Hermes, Codex). "
            "'none' skips per-surface probe + dispatch (the prior-record "
            "needs_retry[] load STILL runs). Otherwise a CSV list of "
            "surface names."
        ),
    )

    # Rollback.
    parser.add_argument(
        "--rollback",
        dest="rollback",
        nargs="?",
        const="LATEST",
        default=None,
        metavar="<timestamp>",
        help=(
            "Without an argument: list available pre-upgrade tarballs "
            "at .alive/upgrades/ (sorted by timestamp descending, with "
            "size + relative-age columns). With an ISO-8601 filename "
            "timestamp argument: extract the matching tarball into "
            ".alive/.rollback-<ts>/ for inspection and print the "
            "manual restore procedure. Full automated swap is "
            "deferred to v3.3."
        ),
    )

    # Override flags for preflight guards.
    parser.add_argument(
        "--force-dirty",
        dest="force_dirty",
        action="store_true",
        default=False,
        help="Bypass the dirty-session-stash refusal.",
    )
    parser.add_argument(
        "--syncthing-coordinated",
        dest="syncthing_coordinated",
        action="store_true",
        default=False,
        help="Bypass the Syncthing-active refusal (operator paused sync).",
    )
    parser.add_argument(
        "--force-incomplete-sync",
        dest="force_incomplete_sync",
        action="store_true",
        default=False,
        help="Bypass the half-sync-marker refusal.",
    )
    parser.add_argument(
        "--unsafe-confirm-target",
        dest="unsafe_confirm_target",
        action="store_true",
        default=False,
        help=(
            "Bypass the home/cloud confirm-required path-policy gate. "
            "Combined with TTY type-back in interactive mode OR "
            "sufficient alone in --non-interactive mode. NEVER "
            "bypasses deny categories."
        ),
    )

    # Sweep / staleness knobs.
    parser.add_argument(
        "--keep-tarballs",
        dest="keep_tarballs",
        type=int,
        default=30,
        help=(
            "Sweep age cutoff in days (default 30). Tarballs older "
            "than this are pruned during phase 8 cleanup."
        ),
    )
    parser.add_argument(
        "--resume-staleness",
        dest="resume_staleness",
        type=int,
        default=24,
        help=(
            "Resume marker staleness cutoff in hours (default 24). "
            "Older markers refuse to resume without --force."
        ),
    )

    # JSON mode.
    parser.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        default=False,
        help="Emit a JSON envelope on stdout for agent consumption.",
    )

    # plugin-root mirror (existing convention).
    parser.add_argument(
        "--plugin-root",
        default=None,
        help=(
            "Override the ALIVE plugin root directory (defaults: "
            "$ALIVE_PLUGIN_ROOT, then auto-discovery)."
        ),
    )


def _emit(args: argparse.Namespace, payload: Dict[str, Any], exit_code: int) -> int:
    """Print the result envelope and return the exit code."""
    if args.json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if not payload.get("ok", False):
            err = payload.get("error") or "preflight refusal"
            code = payload.get("error_code") or ""
            sys.stderr.write(
                "system-upgrade: {} ({})\n".format(err, code)
            )
        else:
            phase = payload.get("phase_reached") or "unknown"
            short = (
                "no-op short-circuit"
                if payload.get("noop_short_circuit") else
                "phase reached: {}".format(phase)
            )
            print("system-upgrade: {}".format(short))
            # T11 acceptance criterion 5: post-upgrade output
            # includes a one-line pointer to rollback availability
            # whenever phase_backup wrote a tarball.
            pointer = payload.get("rollback_pointer")
            if pointer:
                print(pointer)
    return exit_code


def _resolve_target(
    args: argparse.Namespace,
    cwd: Optional[str] = None,
) -> str:
    """Resolve the lexical target path from args; raises ResolveError on miss.

    Mutex check on (positional, --world-root) is done here because
    argparse's mutually_exclusive_group does not compose with optional
    positionals. Returning a single lexical path simplifies the
    preflight chain.
    """
    from .target_resolver import resolve_target_world  # noqa: PLC0415

    if args.world_path is not None and args.world_root is not None:
        # Surface as a usage error (exit 2). The cli.handler maps this.
        raise _UsageError(
            "positional <world-path> and --world-root are mutually "
            "exclusive; pass exactly one."
        )

    explicit = args.world_path or args.world_root
    if explicit is not None:
        # Caller supplied a path -- normalize and return without
        # consulting the resolver (system-upgrade is the one command
        # that operates on legacy worlds).
        return os.path.abspath(
            os.path.expanduser(os.path.expandvars(explicit))
        )

    return resolve_target_world(cwd=cwd)


class _UsageError(Exception):
    """Translates to argparse-style exit code 2."""


def handle(args: argparse.Namespace) -> int:
    """T1 dispatcher.

    Runs preflight, lock acquire, then exits with a structured
    "phase X not implemented" envelope at the first stub phase.
    Detection + downstream phases land in T3-T11.
    """
    from . import preflight  # noqa: PLC0415
    from .lock import UpgradeLock, UpgradeLockBusy  # noqa: PLC0415
    from .target_resolver import ResolveError  # noqa: PLC0415

    # --rollback short-circuits the upgrade pipeline entirely. T11
    # owns the read-side rollback flow (list + extract); the upgrade
    # lock and the full preflight chain are NOT acquired -- rollback
    # is an inspection mode, not a destructive op against world state
    # the way an upgrade is. The handler resolves the target
    # lexically, normalises to a realpath'd world root, then dispatches
    # to ``rollback.run_rollback`` which prints the result + returns
    # the right exit code.
    if getattr(args, "rollback", None) is not None:
        return _handle_rollback(args)

    # --dry-run invariant: a dry run must produce SOMETHING the
    # operator can consume -- either a JSON envelope on stdout
    # (--json) or a plan file at --plan-output. Surfacing this as a
    # usage error early avoids confusing "the dry-run ran but I have
    # nothing" sessions.
    if args.dry_run and not args.json_mode and not args.plan_output:
        return _emit(args, {
            "ok": False,
            "exit_code": 2,
            "error_code": "usage",
            "error": (
                "--dry-run requires --plan-output <path> or --json so "
                "the operator can consume the planned operations; "
                "passing neither leaves the dry-run with no output."
            ),
            "world_root": None,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 2)

    # Resolve the lexical target.
    try:
        target_lexical = _resolve_target(args)
    except _UsageError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": 2,
            "error_code": "usage",
            "error": str(exc),
            "world_root": None,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 2)
    except ResolveError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": 3,
            "error_code": exc.hint_kind,
            "error": exc.message,
            "world_root": None,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 3)

    # Pre-lock chain (steps 0a + 1 + 1a + 2 + 3). When the policy gate
    # surfaces ``unsafe_target_tty_confirm_required:*`` (interactive
    # mode + --unsafe-confirm-target on a home/cloud target), run the
    # type-back prompt and retry the chain with non_interactive=True.
    refusal = _run_pre_lock_with_tty_retry(args, target_lexical, preflight)
    if isinstance(refusal, preflight.PreflightRefusal):
        return _emit(args, {
            "ok": False,
            "exit_code": refusal.exit_code,
            "error_code": refusal.error_code,
            "error": refusal.message,
            "world_root": None,
            "phase_reached": "preflight",
            "noop_short_circuit": False,
        }, refusal.exit_code)
    world_root_resolved = refusal  # success path returns the resolved root

    # Acquire the upgrade lock. PermissionError / OSError from the
    # flock + lock-meta writes is normalized into the structured
    # exit-code-4 envelope; the surrounding orchestrator never sees
    # a raw filesystem exception.
    tool_version = _read_tool_version(args)
    lock = UpgradeLock(world_root_resolved)
    try:
        lock.acquire(tool_version=tool_version)
    except UpgradeLockBusy as busy:
        return _emit(args, {
            "ok": False,
            "exit_code": preflight.EXIT_LOCK_CONTENTION,
            "error_code": "upgrade_lock_busy",
            "error": str(busy),
            "world_root": world_root_resolved,
            "phase_reached": "preflight",
            "noop_short_circuit": False,
            "holder": busy.holder,
        }, preflight.EXIT_LOCK_CONTENTION)
    except PermissionError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": preflight.EXIT_PERMISSION,
            "error_code": "permission:lock_acquire",
            "error": (
                "permission denied acquiring upgrade lock at {}: {}"
                .format(lock.lock_path, exc)
            ),
            "world_root": world_root_resolved,
            "phase_reached": "preflight",
            "noop_short_circuit": False,
        }, preflight.EXIT_PERMISSION)
    except OSError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": preflight.EXIT_PERMISSION,
            "error_code": "permission:lock_acquire",
            "error": (
                "filesystem error acquiring upgrade lock at {}: {}"
                .format(lock.lock_path, exc)
            ),
            "world_root": world_root_resolved,
            "phase_reached": "preflight",
            "noop_short_circuit": False,
        }, preflight.EXIT_PERMISSION)

    try:
        # Post-lock chain (steps 5 + 6 + 7).
        try:
            preflight.run_post_lock_chain(
                world_root_resolved,
                force_dirty=args.force_dirty,
                syncthing_coordinated=args.syncthing_coordinated,
                force_incomplete_sync=args.force_incomplete_sync,
            )
        except preflight.PreflightRefusal as ref:
            return _emit(args, {
                "ok": False,
                "exit_code": ref.exit_code,
                "error_code": ref.error_code,
                "error": ref.message,
                "world_root": world_root_resolved,
                "phase_reached": "preflight",
                "noop_short_circuit": False,
            }, ref.exit_code)

        # Drive phases 2..13 through the orchestrator. The pipeline
        # honours the no-op short-circuit gate (phase 5, R20) and any
        # phase-stub fail-loud surfaces between here and T11.
        # Filesystem write failures (e.g. the no-op record write at
        # .alive/upgrades/<ts>.yaml hitting ENOSPC, EACCES, EROFS)
        # are wrapped in PhaseWriteError by the orchestrator so we
        # can keep ``phase_reached`` in the documented PHASE_NAMES
        # namespace.
        from .orchestrator import (  # noqa: PLC0415
            PhaseWriteError, run_pipeline,
        )
        from . import resume as _resume_mod  # noqa: PLC0415

        # The lock-meta sidecar carries our started_iso; reuse it so
        # the no-op record's started_at lines up with the lock record.
        started_iso = _read_lock_started_iso(lock)
        try:
            result = run_pipeline(
                args,
                world_root_resolved=world_root_resolved,
                tool_version=tool_version,
                started_iso=started_iso,
            )
        except _resume_mod.ResumeRefusal as ref:
            # ``--resume`` validation fired one of the documented
            # refusal codes (resume_marker_missing / _unreadable /
            # _stale / _world_diverged / _tool_version_skew /
            # _step_unknown / _already_done). Map to the structured
            # exit envelope rather than letting it escape as an
            # uncaught exception (round 5 F1).
            #
            # Exit-code mapping aligned with the documented preflight
            # codes (EXIT_NOT_FOUND for missing marker; EXIT_GENERAL
            # for the rest). The marker-missing case is a "couldn't
            # find what you asked me to resume" -- routes to exit 3
            # for parity with missing-world preflight refusals.
            ref_code = getattr(ref, "code", "resume_refusal")
            if ref_code == "resume_marker_missing":
                exit_code = preflight.EXIT_NOT_FOUND
            else:
                exit_code = preflight.EXIT_GENERAL
            return _emit(args, {
                "ok": False,
                "exit_code": exit_code,
                "error_code": ref_code,
                "error": str(ref),
                "world_root": world_root_resolved,
                "phase_reached": "resume",
                "noop_short_circuit": False,
            }, exit_code)
        except PhaseWriteError as pw:
            envelope_msg = (
                "permission denied during {} write: {}".format(
                    pw.phase, pw.cause,
                )
                if isinstance(pw.cause, PermissionError) else
                "filesystem error during {} write: {}".format(
                    pw.phase, pw.cause,
                )
            )
            return _emit(args, {
                "ok": False,
                "exit_code": preflight.EXIT_PERMISSION,
                "error_code": "permission:{}_write".format(pw.phase),
                "error": envelope_msg,
                "world_root": world_root_resolved,
                "phase_reached": pw.phase,
                "noop_short_circuit": (
                    pw.phase == "noop_short_circuit"
                ),
            }, preflight.EXIT_PERMISSION)
        if result.error_code is not None:
            return _emit(args, {
                "ok": False,
                "exit_code": 1,
                "error_code": result.error_code,
                "error": result.error,
                "world_root": world_root_resolved,
                "phase_reached": result.phase_reached,
                "noop_short_circuit": result.noop_short_circuit,
            }, 1)

        envelope = {
            "ok": True,
            "exit_code": 0,
            "error_code": None,
            "error": None,
            "world_root": world_root_resolved,
            "phase_reached": result.phase_reached,
            "noop_short_circuit": result.noop_short_circuit,
            "noop_record_path": result.noop_record_path,
            "backup_tarball_path": result.backup_tarball_path,
        }
        # Per acceptance criterion 5 (T11 of fn-18): the skill output
        # post-upgrade includes a one-line pointer to rollback
        # availability. Wired here so the success envelope carries
        # the pointer text whenever phase_backup wrote a tarball.
        if result.backup_tarball_path:
            from .rollback import (  # noqa: PLC0415
                build_post_upgrade_pointer,
            )
            envelope["rollback_pointer"] = build_post_upgrade_pointer(
                result.backup_tarball_path,
            )
        return _emit(args, envelope, 0)
    finally:
        report = lock.release()
        if (
            report.get("warnings")
            and getattr(args, "verbose", 0) > 0
        ):
            for w in report["warnings"]:
                sys.stderr.write("lock release warning: {}\n".format(w))


def _run_pre_lock_with_tty_retry(
    args: argparse.Namespace,
    target_lexical: str,
    preflight: Any,
) -> Any:
    """Run the pre-lock chain, prompting for TTY type-back if needed.

    Returns either:
        * the resolved ``world_root`` string on success;
        * a ``PreflightRefusal`` on terminal refusal (caller emits).

    The TTY type-back loop fires only when:
        * a refusal has error_code starting with
          ``unsafe_target_tty_confirm_required:`` (i.e. the gate
          escalated for interactive confirmation), AND
        * stdin is a TTY.

    On match, the operator is prompted to type the target path back
    exactly once. A correct match retries the chain with
    ``non_interactive=True`` so the gate's confirm-required branch
    accepts the bypass. Any other refusal (deny, missing world,
    boundary violation, etc.) is returned to the caller verbatim.
    """
    try:
        return preflight.run_pre_lock_chain(
            target_lexical,
            unsafe_confirm_target=args.unsafe_confirm_target,
            non_interactive=args.non_interactive,
        )
    except preflight.PreflightRefusal as ref:
        if not ref.error_code.startswith(
            "unsafe_target_tty_confirm_required:"
        ):
            return ref
        # The refusal asks for an interactive type-back. Honour it
        # only when stdin is a real TTY; otherwise rebuild the
        # refusal with a non-TTY-specific actionable message
        # (telling the operator to pass --non-interactive or run
        # from a terminal). The neutral message from
        # ``run_path_policy_gate`` is the gate's contract -- this
        # caller layer adds the I/O-context-specific wording.
        if not sys.stdin.isatty():
            return preflight.PreflightRefusal(
                exit_code=ref.exit_code,
                error_code=ref.error_code,
                message=(
                    ref.message
                    + "; stdin is not a TTY -- pass --non-interactive "
                    "together with --unsafe-confirm-target, or run "
                    "from a TTY to type the target path back."
                ),
            )
        # The path the operator MUST type back is the path the gate
        # actually objected to -- which may be the resolved path (when
        # a symlink resolves into ~/Dropbox or $HOME). Carrying the
        # pass-specific path through ``ref.confirm_path`` is what
        # makes the symlink-bypass refusal honest.
        expected_path = ref.confirm_path or target_lexical
        sys.stderr.write(ref.message + "\n")
        sys.stderr.write(
            "Type {!r} back exactly to confirm: ".format(expected_path)
        )
        sys.stderr.flush()
        try:
            typed = sys.stdin.readline().rstrip("\n").rstrip("\r")
        except (EOFError, KeyboardInterrupt):
            return ref
        # Compare the typed input EXACTLY against the expected path.
        # Per the spec the type-back is a literal confirmation -- no
        # whitespace normalization. ``readline`` already stripped the
        # trailing newline (and a stray CR on Windows-style stdin);
        # ``.strip()`` would weaken the safety gate by accepting
        # pasted-with-whitespace inputs like ``" /tmp/world "`` for
        # a destructive home/cloud target.
        if typed != expected_path:
            # Mismatch -- treat as a fresh refusal so the operator
            # sees the failure clearly. Reuse the original error_code
            # prefix but flip the suffix so receivers can distinguish
            # "they didn't try" from "they typed wrong".
            return preflight.PreflightRefusal(
                exit_code=ref.exit_code,
                error_code=(
                    ref.error_code.replace(
                        "unsafe_target_tty_confirm_required",
                        "unsafe_target_tty_confirm_mismatch",
                    )
                ),
                message=(
                    "type-back mismatch (got {!r}, expected {!r})"
                    .format(typed, expected_path)
                ),
            )
        # Match: retry with non_interactive=True so the gate's
        # confirm-required branch passes silently.
        try:
            return preflight.run_pre_lock_chain(
                target_lexical,
                unsafe_confirm_target=True,
                non_interactive=True,
            )
        except preflight.PreflightRefusal as ref2:
            return ref2


def _handle_rollback(args: argparse.Namespace) -> int:
    """Dispatch ``--rollback`` (list mode + extract mode).

    No lock acquired; no preflight chain run. Rollback is a read-side
    inspection flow (extract is also "destructive" in the narrow
    sense that it writes a sibling directory at
    ``<world>/.alive/.rollback-<ts>/``, but it never mutates the
    operator's existing ``.alive/`` or ``_kernel/`` state -- the
    manual swap is documented in the printed restore procedure for
    operator-driven application).

    Exit codes:
        * 0 -- list rendered, or extract succeeded.
        * 1 -- generic refusal (invalid timestamp, already-extracted,
          extraction failure / LD22 refusal).
        * 3 -- not found (no upgrades dir, or no matching tarball at
          requested timestamp).
        * 2 -- usage error (positional + --world-root mutex).
    """
    from .target_resolver import ResolveError  # noqa: PLC0415
    from . import rollback as rollback_mod  # noqa: PLC0415

    # Resolve the lexical target. The rollback path does NOT walk the
    # full preflight chain -- a typo in the world path should fail
    # loud here without the path-policy gate engaging.
    try:
        target_lexical = _resolve_target(args)
    except _UsageError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": 2,
            "error_code": "usage",
            "error": str(exc),
            "world_root": None,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 2)
    except ResolveError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": 3,
            "error_code": exc.hint_kind,
            "error": exc.message,
            "world_root": None,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 3)

    # Normalise to a realpath. Rollback does NOT enforce the
    # path-policy gate (system / home / cloud) the way the destructive
    # upgrade path does -- inspection of an existing upgrade tarball
    # is a read of the operator's own state, not a write to a
    # protected location.
    try:
        world_root_resolved = os.path.realpath(target_lexical)
    except OSError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": 3,
            "error_code": "missing_world",
            "error": "could not resolve {!r}: {}".format(
                target_lexical, exc,
            ),
            "world_root": None,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 3)

    # Existence check on the resolved world root. Without this, a
    # typo in --world-root (e.g. /does/not/exist) silently falls
    # through to ``list_tarballs`` (which returns a fake "no
    # upgrades directory" report) or ``extract_tarball`` (which
    # raises ``rollback_no_upgrades_dir`` -- misdiagnosing a missing
    # world as "this world has never been upgraded"). Fail loud here
    # with the same ``missing_world`` envelope the upgrade path
    # uses, so a typo surfaces uniformly across both flows.
    #
    # We use ``os.stat`` instead of ``os.path.isdir`` so an existing
    # but unreadable world surfaces as ``rollback_permission`` /
    # exit 4 (the documented permission contract) rather than
    # ``missing_world`` / exit 3. ``os.path.isdir`` swallows
    # ``PermissionError`` and returns ``False``, which would mask
    # the real failure.
    try:
        world_st = os.stat(world_root_resolved)
    except FileNotFoundError:
        return _emit(args, {
            "ok": False,
            "exit_code": 3,
            "error_code": "missing_world",
            "error": (
                "target world root does not exist: {} "
                "(resolved from {!r})"
            ).format(world_root_resolved, target_lexical),
            "world_root": world_root_resolved,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 3)
    except PermissionError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": 4,
            "error_code": "rollback_permission",
            "error": (
                "permission denied probing world root {}: {}"
            ).format(world_root_resolved, exc),
            "world_root": world_root_resolved,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 4)
    except OSError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": 4,
            "error_code": "rollback_permission",
            "error": (
                "filesystem error probing world root {}: {}"
            ).format(world_root_resolved, exc),
            "world_root": world_root_resolved,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 4)
    import stat as _stat  # noqa: PLC0415
    if not _stat.S_ISDIR(world_st.st_mode):
        return _emit(args, {
            "ok": False,
            "exit_code": 3,
            "error_code": "missing_world",
            "error": (
                "target world root is not a directory: {} "
                "(resolved from {!r})"
            ).format(world_root_resolved, target_lexical),
            "world_root": world_root_resolved,
            "phase_reached": None,
            "noop_short_circuit": False,
        }, 3)

    rb_arg = args.rollback
    if rb_arg == rollback_mod.LATEST_SENTINEL:
        # List mode -- the operator passed --rollback with NO
        # argument. Per the task spec, this is the listing flow, NOT
        # an "extract latest" shortcut.
        try:
            report = rollback_mod.list_tarballs(world_root_resolved)
        except rollback_mod.RollbackError as exc:
            # Permission errors and other listing failures surface
            # via RollbackError now -- route to the standard envelope.
            return _emit(args, {
                "ok": False,
                "exit_code": exc.exit_code,
                "error_code": exc.error_code,
                "error": exc.message,
                "world_root": world_root_resolved,
                "phase_reached": "rollback_list",
                "noop_short_circuit": False,
            }, exc.exit_code)
        if args.json_mode:
            payload = {
                "ok": True,
                "exit_code": 0,
                "error_code": None,
                "error": None,
                "world_root": world_root_resolved,
                "phase_reached": "rollback_list",
                "noop_short_circuit": False,
                "rollback_mode": "list",
                "upgrades_dir": report.upgrades_dir,
                "upgrades_dir_present": report.upgrades_dir_present,
                "entries": [
                    {
                        "timestamp": e.timestamp,
                        "absolute_path": e.absolute_path,
                        "size_bytes": e.size_bytes,
                        "age_seconds": e.age_seconds,
                    }
                    for e in report.entries
                ],
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(rollback_mod.format_list_report(report))
        return 0

    # Extract mode -- the operator passed --rollback <iso-ts>.
    try:
        result = rollback_mod.extract_tarball(
            world_root_resolved, rb_arg,
        )
    except rollback_mod.RollbackError as exc:
        return _emit(args, {
            "ok": False,
            "exit_code": exc.exit_code,
            "error_code": exc.error_code,
            "error": exc.message,
            "world_root": world_root_resolved,
            "phase_reached": "rollback_extract",
            "noop_short_circuit": False,
        }, exc.exit_code)

    if args.json_mode:
        payload = {
            "ok": True,
            "exit_code": 0,
            "error_code": None,
            "error": None,
            "world_root": world_root_resolved,
            "phase_reached": "rollback_extract",
            "noop_short_circuit": False,
            "rollback_mode": "extract",
            "tarball_path": result.tarball_path,
            "extract_dir": result.extract_dir,
            "manifest_entries": list(result.manifest_entries),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(rollback_mod.format_restore_procedure(
            result, world_root_resolved, rb_arg,
        ))
    return 0


def _read_lock_started_iso(lock: Any) -> str:
    """Read ``started_iso`` from the lock-meta sidecar, or synthesize."""
    try:
        with open(lock.lock_meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("started_iso"), str):
            return data["started_iso"]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        from _common import iso_now  # noqa: PLC0415
        return iso_now()
    except ImportError:
        from datetime import datetime, timezone  # noqa: PLC0415
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_tool_version(args: argparse.Namespace) -> str:
    """Best-effort read of plugin version from ``plugin.json``.

    On any error returns ``"unknown"`` -- the lock-meta sidecar never
    fails because we couldn't read the plugin manifest.
    """
    try:
        from _common import resolve_plugin_root  # noqa: PLC0415
    except ImportError:
        return "unknown"
    try:
        plugin_root = resolve_plugin_root(args.plugin_root)
    except FileNotFoundError:
        return "unknown"
    manifest = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("version", "unknown"))
    except (OSError, json.JSONDecodeError):
        return "unknown"


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Register the ``system-upgrade`` subcommand on the dispatcher."""
    parser = subparsers.add_parser(
        "system-upgrade",
        help=SCHEMA_METADATA["description"],
        description=SCHEMA_METADATA["description"],
    )
    _add_args(parser)
    # Stash SCHEMA_METADATA for ``alive schema`` introspection.
    try:
        from schema import SCHEMA_METADATA_DEFAULT_KEY  # noqa: PLC0415
    except ImportError:
        SCHEMA_METADATA_DEFAULT_KEY = "_schema_metadata"
    parser.set_defaults(
        _handler=handle,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )
    return parser
