"""CLI registration for `alive demo` subcommand group.

`scripts/cli.py` appends this module's `register` to the `_SUBCOMMANDS` list
so the dispatcher exposes `alive demo <subcommand>`. As of fn-2-2zz.12 every
user-facing subcommand body is implemented:

* ``list`` -- enumerate promoted demo worlds + partials, render the
  6-column bordered-block table.
* ``activate <ref> [--confirm]`` -- 3-step ref resolution (label / ULID
  prefix / ambiguous-match envelope), pre-check, Stage 5 step 8-10
  re-activation against an existing world.
* ``deactivate`` -- restore the cached previous world-root pointer.
* ``delete <ref> [--confirm]`` -- destroy a demo world after the
  irreversibility surface; refuses on the active world.
* ``status`` -- print demo-state.json with self-heal applied.

Pipeline-internal subcommands (``stage2`` ... ``preset`` / ``validate``)
are unchanged from the earlier landings.

Module layering: ``_common``, ``_world_root_io``, and ``lib.py`` are the
only internal imports. State manipulation goes through ``state.py``. The
skill router (``SKILL.md``) shells out to the CLI rather than reaching
into this module directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Bootstrap import paths. The plugin's existing convention (cf. cli.py,
# doctor.py, promote.py) is flat-modules-on-sys.path rather than packages, so
# `scripts/` must be importable for `_common` / `_world_root_io` lookups.
# Sibling files in this directory (`state.py`, `lib.py`) have generic names
# that could collide with other plugins' modules; we load them through
# importlib with namespaced sys.modules keys so they never clobber a future
# top-level `state` or `lib` import.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_HERE, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _load_sibling(module_name: str, filename: str):
    """Load a sibling .py file under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = f"alive_demo.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, filename)
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


demo_state = _load_sibling("state", "state.py")
demo_lib = _load_sibling("lib", "lib.py")


def _load_stage2():
    """Load `stages/stage2.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.stage2"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "stage2.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _load_stage3():
    """Load `stages/stage3.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.stage3"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "stage3.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _load_stage4():
    """Load `stages/stage4.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.stage4"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "stage4.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _load_stage5():
    """Load `stages/stage5.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.stage5"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "stage5.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _load_preset():
    """Load `stages/preset.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.preset"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "preset.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _load_activate_existing():
    """Load `stages/activate_existing.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.activate_existing"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "activate_existing.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _load_deactivate():
    """Load `stages/deactivate.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.deactivate"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "deactivate.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _load_delete_existing():
    """Load `stages/delete_existing.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.delete_existing"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "stages", "delete_existing.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module

# `_common.FlockTimeoutError` is the exception both `state.save_state` and
# `state.load_state` can surface when the demo-state lock is contended. We
# catch it at the handler boundary so the user sees a structured `lock_timeout`
# envelope (matching the documented exit-code 5 in SCHEMA_METADATA) rather
# than a Python traceback. `_common` was already pulled in transitively via
# `state.py`; importing here keeps the symbol explicit at the catch site.
from _common import FlockTimeoutError  # noqa: E402

# ---------------------------------------------------------------------------
# Schema metadata (consumed by `alive schema` introspection)
# ---------------------------------------------------------------------------

SCHEMA_METADATA: Dict[str, Any] = {
    "description": (
        "Manage `/alive:demo` generated worlds: list partial + active "
        "generations, activate / deactivate a demo world, delete a "
        "promoted world from disk, or print the current demo-state.json "
        "status. All five user-facing subcommand bodies are implemented "
        "(fn-2-2zz.12); pipeline-internal subcommands (stage2..preset) "
        "drive the generation transaction."
    ),
    "stdout_shape": {
        "envelope": (
            "JSON object. On success, subcommand-specific shape carrying "
            "`rendered_block` (a pre-formatted bordered-block surface). "
            "On failure, "
            "{success: false, error: {code, message, hint?}}."
        ),
    },
    "exit_codes": {
        "0": "success",
        "1": "general failure",
        "2": "usage error",
        "3": "demo-state.json schema_version mismatch (run `/alive:demo reset`)",
        "5": "lock acquisition timed out on .demo-state.lock",
    },
    "examples": [
        {
            "input": "alive demo status",
            "output_excerpt": (
                '{"success": true, "active_world": null, '
                '"partial_generations": [], "schema_version": "0.1"}'
            ),
        },
        {
            "input": "alive demo list",
            "output_excerpt": (
                '{"success": true, "active_world": null, "partials": []}'
            ),
        },
    ],
}


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

def _emit(payload: Dict[str, Any]) -> int:
    """Print pure JSON to stdout. Return the exit code embedded in the payload.

    The ``_exit_code`` private field (when present) controls the exit code
    on failure; it is stripped BEFORE printing so the published JSON shape
    only carries documented surface keys (success / error / data).
    """
    exit_code = int(payload.pop("_exit_code", 1)) if not payload.get("success", False) else 0
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


class _EnvelopeExit(Exception):
    """Internal control-flow signal: an error envelope has been emitted.

    Carries the exit code the handler should propagate. Caught at the
    handler boundary; never escapes the CLI module.
    """

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


def _load_state_envelope() -> Dict[str, Any]:
    """Load demo-state with friendly schema-mismatch handling.

    Returns the loaded state on success. On schema-mismatch / corrupt-state
    failures, emits the canonical JSON envelope (with `_exit_code` already
    consumed by `_emit`) and raises `_EnvelopeExit` carrying the appropriate
    exit code. Handlers wrap their bodies in `try/except _EnvelopeExit` so
    the CLI's `main()` sees the exit code via `return`.
    """
    try:
        return demo_state.load_state()
    except FlockTimeoutError as exc:
        rc = _emit({
            "success": False,
            "error": {
                "code": "lock_timeout",
                "message": str(exc),
                "hint": (
                    "Another /alive:demo session is updating "
                    "demo-state.json. Wait a few seconds and retry."
                ),
            },
            "_exit_code": 5,
        })
        raise _EnvelopeExit(rc) from exc
    except demo_state.SchemaVersionMismatch as exc:
        rc = _emit({
            "success": False,
            "error": {
                "code": "schema_version_mismatch",
                "message": str(exc),
                "hint": (
                    "Run `/alive:demo reset` to rebuild demo-state.json "
                    "from the live world-root pointer."
                ),
                "found": exc.found,
                "expected": exc.expected,
            },
            "_exit_code": 3,
        })
        raise _EnvelopeExit(rc) from exc
    except demo_state.DemoStateError as exc:
        rc = _emit({
            "success": False,
            "error": {
                "code": "demo_state_corrupt",
                "message": str(exc),
                "hint": (
                    "demo-state.json is unreadable. Inspect "
                    "`~/.config/alive/demo-state.json` or run "
                    "`/alive:demo reset`."
                ),
            },
            "_exit_code": 1,
        })
        raise _EnvelopeExit(rc) from exc


def _status_handler(args: argparse.Namespace) -> int:
    """`alive demo status` -- print the loaded + self-healed state as JSON.

    Includes a rendered bordered-block ``rendered_block`` field carrying
    the 5-7 line surface from ``lib.format_status`` so the squirrel can
    print it verbatim. Active world is hydrated into a ``WorldRecord``
    when the active path is resolvable; otherwise we synthesise a record
    from the demo-state metadata.
    """
    try:
        state = _load_state_envelope()
    except _EnvelopeExit as exit_signal:
        return exit_signal.exit_code

    active = state.get("active_world")
    previous = state.get("previous_world_root")

    # Hydrate active_world into a WorldRecord (or None) for format_status.
    active_record = None
    if isinstance(active, dict):
        ulid = active.get("ulid", "")
        path = active.get("path", "")
        # Try to locate this record in list_demos so we get accurate
        # disk_size_bytes; fall back to a synthetic record.
        records = demo_lib.list_demos()
        match = next((r for r in records if r.ulid == ulid), None)
        if match is not None:
            active_record = match
        else:
            active_record = demo_lib.WorldRecord(
                ulid=ulid,
                label=active.get("label", ""),
                path=path,
                created_at=active.get("activated_at", ""),
                last_activated_at=active.get("activated_at", ""),
                disk_size_bytes=-1,
                status="active",
                persona_name=None,
            )

    body = demo_lib.format_status(active_record, previous)
    rendered = demo_lib.format_block("demo status", body)
    return _emit({
        "success": True,
        "schema_version": state.get("schema_version"),
        "active_world": active,
        "previous_world_root": previous,
        "partial_generations": state.get("partial_generations", []),
        "rendered_block": rendered,
    })


def _list_handler(args: argparse.Namespace) -> int:
    """`alive demo list` -- enumerate promoted demo worlds + partials.

    Renders the 6-column bordered-block table at ``rendered_block`` so the
    squirrel prints it verbatim. Each ``WorldRecord`` is also serialised
    into the ``records`` field for programmatic consumers.
    """
    try:
        state = _load_state_envelope()
    except _EnvelopeExit as exit_signal:
        return exit_signal.exit_code

    records = demo_lib.list_demos()
    active_world = state.get("active_world")
    active_ulid = (
        active_world.get("ulid")
        if isinstance(active_world, dict) and active_world.get("ulid")
        else None
    )

    table = demo_lib.format_list_table(records, active_ulid=active_ulid)
    if records:
        body = table
    else:
        body = (
            "No demo worlds found.\n"
            "\n"
            "Run /alive:demo to create one (preset or custom)."
        )
    rendered = demo_lib.format_block("demo list", body)

    return _emit({
        "success": True,
        "active_world": active_world,
        "active_ulid": active_ulid,
        "records": [r.to_dict() for r in records],
        "partials": list(state.get("partial_generations", [])),
        "rendered_block": rendered,
    })


def _resolve_ref_or_emit(ref: str):
    """Resolve a ref to a WorldRecord, or emit an error envelope and raise.

    Wraps ``lib.resolve_ref``; on ``LookupError`` (no match) emits a
    structured ``not_found`` envelope; on ``AmbiguousMatch`` emits a
    structured envelope carrying the candidate list + a rendered picker
    block so the squirrel can drive ``AskUserQuestion``.

    Returns the WorldRecord on success. Raises ``_EnvelopeExit`` on
    failure (the handler catches it and returns the exit code).
    """
    try:
        return demo_lib.resolve_ref(ref)
    except demo_lib.AmbiguousMatch as exc:
        candidates = exc.candidates
        rendered = demo_lib.format_block(
            f"multiple matches for {ref!r}",
            demo_lib.format_picker_body(ref, candidates),
        )
        rc = _emit({
            "success": False,
            "error": {
                "code": "ambiguous_ref",
                "message": str(exc),
                "hint": (
                    "Pick the intended world by ULID prefix or run "
                    "`alive demo list` to see all candidates."
                ),
            },
            "candidates": [r.to_dict() for r in candidates],
            "rendered_block": rendered,
            "_exit_code": 1,
        })
        raise _EnvelopeExit(rc) from exc
    except LookupError as exc:
        rc = _emit({
            "success": False,
            "error": {
                "code": "not_found",
                "message": str(exc),
                "hint": (
                    "Run `alive demo list` to see available demo worlds."
                ),
            },
            "_exit_code": 1,
        })
        raise _EnvelopeExit(rc) from exc


def _activate_handler(args: argparse.Namespace) -> int:
    """`alive demo activate <ref> [--confirm]` -- re-activate a demo world.

    Resolves the ref, runs the activation pre-check, and (with
    ``--confirm``) runs the tail of Stage 5 (steps 9-11) against the
    existing world.

    Catches the full vocabulary of recoverable errors (lock timeout,
    schema mismatch, demo-state corruption, ActivateExistingError) plus
    a final catch-all so the CLI never leaks a Python traceback past
    the JSON-only contract.
    """
    try:
        record = _resolve_ref_or_emit(args.ref)
    except _EnvelopeExit as exit_signal:
        return exit_signal.exit_code

    activate_mod = _load_activate_existing()
    try:
        result = activate_mod.run_activate(
            record,
            confirm=bool(getattr(args, "confirm", False)),
        )
    except activate_mod.ActivateExistingError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "activate_error",
                "message": str(exc),
                "hint": (
                    "Inspect ~/.config/alive/world-root and demo-state.json. "
                    "If the pointer is corrupt run /alive:demo reset."
                ),
            },
            "_exit_code": 1,
        })
    except FlockTimeoutError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "lock_timeout",
                "message": str(exc),
                "hint": (
                    "Another /alive:demo session is updating "
                    "demo-state.json. Wait a few seconds and retry."
                ),
            },
            "_exit_code": 5,
        })
    except demo_state.SchemaVersionMismatch as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "schema_version_mismatch",
                "message": str(exc),
                "hint": "Run /alive:demo reset to rebuild demo-state.json.",
                "found": exc.found,
                "expected": exc.expected,
            },
            "_exit_code": 3,
        })
    except demo_state.DemoStateError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "demo_state_corrupt",
                "message": str(exc),
                "hint": (
                    "demo-state.json is unreadable. Inspect "
                    "~/.config/alive/demo-state.json or run "
                    "/alive:demo reset."
                ),
            },
            "_exit_code": 1,
        })
    except (ValueError, OSError) as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "activate_error",
                "message": (
                    f"unexpected {type(exc).__name__} during activation: {exc}"
                ),
                "hint": "See ~/.config/alive/world-root for pointer state.",
            },
            "_exit_code": 1,
        })

    if result.get("status") == "already_active":
        rendered = demo_lib.format_block(
            "already active",
            (
                f"Demo world is already activated:\n"
                f"  {record.ulid}  -  {record.label or '(unknown)'}\n"
                f"  {record.path}\n"
                f"\n"
                f"No changes made; previous_world_root preserved."
            ),
        )
        return _emit({
            "success": True,
            "result": result,
            "world": record.to_dict(),
            "rendered_block": rendered,
        })

    if result.get("status") == "needs_confirmation":
        findings = result.get("findings", [])
        body_lines = [
            "The current live world has unsaved work the demo activation will replace:",
            "",
        ]
        for f in findings[:5]:
            body_lines.append(f"  - [predicate {f.get('predicate')}] {f.get('evidence')}")
        if len(findings) > 5:
            body_lines.append(f"  - (+{len(findings) - 5} more)")
        body_lines.append("")
        body_lines.append("Re-run with --confirm to proceed.")
        rendered = demo_lib.format_block(
            "activation -- uncommitted work on current world",
            "\n".join(body_lines),
        )
        return _emit({
            "success": False,
            "error": {
                "code": "needs_confirmation",
                "message": (
                    f"{len(findings)} unsaved-work finding(s) on the "
                    f"current live world; re-run with --confirm to proceed."
                ),
                "hint": f"alive demo activate {args.ref} --confirm",
            },
            "findings": findings,
            "world": record.to_dict(),
            "rendered_block": rendered,
            "_exit_code": 1,
        })

    body_lines = [
        "Demo world activated:",
        f"  {record.ulid}  -  {record.label or '(unknown)'}",
        f"  {record.path}",
        "",
        "The session-start hook injects WORLD_INDEX once per session. To pick",
        "up the new world index, restart Claude Code (Cmd+Q + relaunch).",
        "",
        "Or: run /alive:world to re-render against the new pointer.",
    ]
    # Surface any post-commit build-log warning (codex review round 6).
    # The world IS activated -- the warning is advisory ("audit trail
    # entry could not be written"), but it MUST be visible to the user
    # so they can repair the build log or re-run activate later.
    build_log_warning = result.get("build_log_warning")
    if build_log_warning:
        body_lines.append("")
        body_lines.append(f"WARNING: {build_log_warning}")
        body_lines.append(
            "The world is active at the pointer level. The audit-trail "
            "entry in _demo-build-log.md could not be refreshed; the "
            "frontmatter activated_at may be stale. Inspect the build "
            "log and edit the timestamp manually if it matters for "
            "downstream tooling."
        )
    rendered = demo_lib.format_block(
        "activated -- restart Claude Code",
        "\n".join(body_lines),
    )
    return _emit({
        "success": True,
        "result": result,
        "world": record.to_dict(),
        "build_log_warning": build_log_warning,
        "rendered_block": rendered,
    })


def _deactivate_handler(args: argparse.Namespace) -> int:
    """`alive demo deactivate` -- restore the previous world-root pointer."""
    deactivate_mod = _load_deactivate()
    try:
        result = deactivate_mod.run_deactivate()
    except deactivate_mod.DeactivateError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "deactivate_error",
                "message": str(exc),
            },
            "_exit_code": 1,
        })
    except FlockTimeoutError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "lock_timeout",
                "message": str(exc),
                "hint": (
                    "Another /alive:demo session is updating "
                    "demo-state.json. Wait a few seconds and retry."
                ),
            },
            "_exit_code": 5,
        })
    except demo_state.SchemaVersionMismatch as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "schema_version_mismatch",
                "message": str(exc),
                "hint": "Run /alive:demo reset to rebuild demo-state.json.",
                "found": exc.found,
                "expected": exc.expected,
            },
            "_exit_code": 3,
        })
    except demo_state.DemoStateError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "demo_state_corrupt",
                "message": str(exc),
                "hint": "Run /alive:demo reset to rebuild demo-state.json.",
            },
            "_exit_code": 1,
        })
    except (ValueError, OSError) as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "deactivate_error",
                "message": (
                    f"unexpected {type(exc).__name__} during deactivate: {exc}"
                ),
            },
            "_exit_code": 1,
        })

    status = result.get("status")
    if status == "no_demo_active":
        rendered = demo_lib.format_block(
            "deactivate -- no demo active",
            "No demo world is currently active. Nothing to do.",
        )
        return _emit({
            "success": True,
            "result": result,
            "rendered_block": rendered,
        })
    if status == "no_previous_world":
        active = result.get("active_world", {})
        rendered = demo_lib.format_block(
            "deactivate -- cold demo",
            (
                f"The active demo world has no cached previous world-root.\n"
                f"This happens when the demo was activated against an empty pointer.\n"
                f"\n"
                f"Active demo: {active.get('ulid', '?')} -- {active.get('label', '?')}\n"
                f"\n"
                f"Either run /alive:demo to create a new demo, or set the\n"
                f"world-root pointer manually to a real world."
            ),
        )
        return _emit({
            "success": False,
            "error": {
                "code": "no_previous_world",
                "message": (
                    "Active demo has no cached previous_world_root; cannot restore."
                ),
                "hint": (
                    "Activate another world via /alive:demo or set "
                    "~/.config/alive/world-root manually."
                ),
            },
            "result": result,
            "rendered_block": rendered,
            "_exit_code": 1,
        })

    deactivated = result.get("deactivated", {})
    rendered = demo_lib.format_block(
        "deactivated -- restart Claude Code",
        (
            f"Demo world deactivated:\n"
            f"  {deactivated.get('ulid', '?')}  -  {deactivated.get('label', '?')}\n"
            f"\n"
            f"World-root restored to:\n"
            f"  {result.get('restored_world_root', '?')}\n"
            f"\n"
            f"Restart Claude Code (Cmd+Q + relaunch) so the session picks up\n"
            f"the restored world index."
        ),
    )
    return _emit({
        "success": True,
        "result": result,
        "rendered_block": rendered,
    })


def _delete_handler(args: argparse.Namespace) -> int:
    """`alive demo delete <ref> [--confirm]` -- destroy a demo world."""
    try:
        record = _resolve_ref_or_emit(args.ref)
    except _EnvelopeExit as exit_signal:
        return exit_signal.exit_code

    delete_mod = _load_delete_existing()
    try:
        result = delete_mod.run_delete(
            record,
            confirm=bool(getattr(args, "confirm", False)),
        )
    except delete_mod.PointerReadError as exc:
        # Pointer corrupt / unreadable: refuse rather than guess.
        # PointerReadError is a DeleteError subclass; catch it FIRST
        # so the more-specific code surfaces.
        return _emit({
            "success": False,
            "error": {
                "code": "pointer_read_error",
                "message": str(exc),
                "hint": (
                    "Inspect ~/.config/alive/world-root or run "
                    "/alive:demo reset; refusing to delete under "
                    "indeterminate active-world state."
                ),
            },
            "_exit_code": 1,
        })
    except delete_mod.DeleteError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "delete_error",
                "message": str(exc),
            },
            "_exit_code": 1,
        })
    except FlockTimeoutError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "lock_timeout",
                "message": str(exc),
                "hint": (
                    "Another /alive:demo session is updating "
                    "demo-state.json. Wait a few seconds and retry."
                ),
            },
            "_exit_code": 5,
        })
    except demo_state.SchemaVersionMismatch as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "schema_version_mismatch",
                "message": str(exc),
                "hint": "Run /alive:demo reset to rebuild demo-state.json.",
                "found": exc.found,
                "expected": exc.expected,
            },
            "_exit_code": 3,
        })
    except demo_state.DemoStateError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "demo_state_corrupt",
                "message": str(exc),
                "hint": "Run /alive:demo reset to rebuild demo-state.json.",
            },
            "_exit_code": 1,
        })
    except (ValueError, OSError) as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "delete_error",
                "message": (
                    f"unexpected {type(exc).__name__} during delete: {exc}"
                ),
            },
            "_exit_code": 1,
        })

    status = result.get("status")
    if status == "refused_active":
        rendered = demo_lib.format_block(
            "delete refused -- active world",
            (
                f"Refusing to delete the currently-active demo world:\n"
                f"  {record.ulid}  -  {record.label or '(unknown)'}\n"
                f"\n"
                f"Run /alive:demo deactivate first, then re-run delete."
            ),
        )
        return _emit({
            "success": False,
            "error": {
                "code": "refused_active",
                "message": (
                    "Refusing to delete the currently-active demo world."
                ),
                "hint": "alive demo deactivate; alive demo delete <ref> --confirm",
            },
            "result": result,
            "rendered_block": rendered,
            "_exit_code": 1,
        })

    if status == "needs_confirmation":
        size = demo_lib.bytes_human(record.disk_size_bytes)
        rendered = demo_lib.format_block(
            "delete -- irreversible",
            (
                f"About to PERMANENTLY DELETE this demo world:\n"
                f"  {record.ulid}  -  {record.label or '(unknown)'}\n"
                f"  {record.path}\n"
                f"  size: {size}, created: {record.created_at or '(unknown)'}\n"
                f"\n"
                f"This cannot be undone. The world directory will be removed\n"
                f"with shutil.rmtree.\n"
                f"\n"
                f"Re-run with --confirm to proceed."
            ),
        )
        return _emit({
            "success": False,
            "error": {
                "code": "needs_confirmation",
                "message": (
                    "Deletion is irreversible; re-run with --confirm to proceed."
                ),
                "hint": f"alive demo delete {args.ref} --confirm",
            },
            "result": result,
            "rendered_block": rendered,
            "_exit_code": 1,
        })

    deleted = result.get("deleted", {})
    rendered = demo_lib.format_block(
        "deleted",
        (
            f"Demo world removed:\n"
            f"  {deleted.get('ulid', '?')}  -  {deleted.get('label', '?')}\n"
            f"  {deleted.get('path', '?')}"
        ),
    )
    return _emit({
        "success": True,
        "result": result,
        "rendered_block": rendered,
    })


def _resolve_world_root(args: argparse.Namespace) -> str:
    """Resolve the world-root path for brief substitution.

    Stage 2's prompt template uses `{WORLD_ROOT}` for the subagent brief.
    For partials in flight, the partial dir itself is the most useful
    anchor (no live world is yet promoted). Callers can override via
    `--world-root` for tests / explicit dispatch.
    """
    explicit = getattr(args, "world_root", None)
    if explicit:
        return os.path.abspath(explicit)
    return os.path.abspath(args.partial)


def _stage2_prepare_handler(args: argparse.Namespace) -> int:
    """`alive demo stage2 prepare` — emit dispatch descriptors as JSON."""
    stage2 = _load_stage2()
    try:
        descriptors = stage2.prepare_dispatches(
            args.partial,
            world_root=_resolve_world_root(args),
        )
    except stage2.Stage2NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage2_not_ready",
                "message": str(exc),
                "hint": (
                    "Run Stage 0 + Stage 1 first; freeze the anchor envelope "
                    "before dispatching Stage 2."
                ),
            },
            "_exit_code": 1,
        })
    except stage2.Stage2Error as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage2_error",
                "message": str(exc),
            },
            "_exit_code": 1,
        })
    batches = stage2.batch_dispatches(descriptors)
    return _emit({
        "success": True,
        "stage": "2",
        "partial_dir": os.path.abspath(args.partial),
        "dispatches": descriptors,
        "batches": [[d["slug"] for d in batch] for batch in batches],
        "batch_size": stage2.DEFAULT_BATCH_SIZE,
    })


def _stage2_collect_validate_handler(args: argparse.Namespace) -> int:
    """`alive demo stage2 collect-validate` — file presence + structural validation.

    Builds the same dispatch descriptors `prepare` would build (from the
    spine + frozen anchor envelope) and passes them to both
    `collect_outputs` and `validate_entity_outputs`. Without descriptors,
    the helpers fall back to disk inference, which silently reports
    `success` on a fresh frozen partial that has produced no entity
    files at all. Driving from the expected slug set guarantees missing
    per-slug directories surface as `status="missing"` and at least one
    `directory_missing` / `missing_file` finding.
    """
    stage2 = _load_stage2()
    try:
        descriptors = stage2.prepare_dispatches(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        coverage = stage2.collect_outputs(args.partial, dispatches=descriptors)
        findings = stage2.validate_entity_outputs(args.partial, dispatches=descriptors)
    except stage2.Stage2NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage2_not_ready",
                "message": str(exc),
                "hint": (
                    "Run Stage 0 + Stage 1 first; freeze the anchor envelope "
                    "before validating Stage 2 outputs."
                ),
            },
            "_exit_code": 1,
        })
    except stage2.Stage2Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage2_error", "message": str(exc)},
            "_exit_code": 1,
        })
    errors = [f for f in findings if f.get("severity") == "error"]
    # Run the unified validate.py facade so the same envelope carries
    # both the per-stage findings (for retry_dispatch) AND the
    # ok/retryable/fatal verdict the squirrel uses for the three-option
    # surface. validate.py's classification is the source of truth for
    # "should we retry, escalate, or proceed?"; the per-stage findings
    # remain the source of truth for WHAT to fix.
    validate = _load_validate()
    coherence = validate.validate_stage("2", args.partial)
    return _emit({
        "success": True,
        "stage": "2",
        "partial_dir": os.path.abspath(args.partial),
        "coverage": coverage,
        "findings": findings,
        "error_count": len(errors),
        "coherence": coherence.to_json(),
    })


def _stage2_retry_dispatch_handler(args: argparse.Namespace) -> int:
    """`alive demo stage2 retry-dispatch` — build retry descriptors."""
    stage2 = _load_stage2()
    try:
        descriptors = stage2.prepare_dispatches(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        findings = stage2.validate_entity_outputs(args.partial, dispatches=descriptors)
        failed_slugs = {
            f["slug"] for f in findings if f.get("severity") == "error"
        }
        failed = [d for d in descriptors if d["slug"] in failed_slugs]
        retries = stage2.retry_dispatches(failed, findings)
    except stage2.Stage2Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage2_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "2",
        "partial_dir": os.path.abspath(args.partial),
        "retries": retries,
        "retry_slugs": [r["slug"] for r in retries],
    })


def _stage2_freeze_handler(args: argparse.Namespace) -> int:
    """`alive demo stage2 freeze` — write stage2_done.json marker."""
    stage2 = _load_stage2()
    try:
        descriptors = stage2.prepare_dispatches(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        marker = stage2.freeze_stage(args.partial, dispatches=descriptors)
    except stage2.Stage2Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage2_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "2",
        "partial_dir": os.path.abspath(args.partial),
        "marker": marker,
    })


def _stage3_prepare_handler(args: argparse.Namespace) -> int:
    """`alive demo stage3 prepare`: emit the single dispatch descriptor."""
    stage3 = _load_stage3()
    try:
        descriptor = stage3.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
    except stage3.Stage3NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage3_not_ready",
                "message": str(exc),
                "hint": (
                    "Run Stages 0-2 first; freeze the spine, anchor "
                    "envelope, and entity scaffolds before dispatching "
                    "Stage 3."
                ),
            },
            "_exit_code": 1,
        })
    except stage3.Stage3Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage3_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "3",
        "partial_dir": os.path.abspath(args.partial),
        "dispatch": descriptor,
    })


def _stage3_collect_validate_handler(args: argparse.Namespace) -> int:
    """`alive demo stage3 collect-validate`: file presence + structural validation."""
    stage3 = _load_stage3()
    try:
        descriptor = stage3.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        coverage = stage3.collect_outputs(
            args.partial,
            expected_people=descriptor["expected_people"],
            expected_walnuts=descriptor["expected_walnuts"],
        )
        findings = stage3.validate_timeline(
            args.partial,
            expected_people=descriptor["expected_people"],
            expected_walnuts=descriptor["expected_walnuts"],
        )
    except stage3.Stage3NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage3_not_ready",
                "message": str(exc),
                "hint": (
                    "Run Stages 0-2 first before validating Stage 3 outputs."
                ),
            },
            "_exit_code": 1,
        })
    except stage3.Stage3Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage3_error", "message": str(exc)},
            "_exit_code": 1,
        })
    errors = [f for f in findings if f.get("severity") == "error"]
    validate = _load_validate()
    coherence = validate.validate_stage("3", args.partial)
    return _emit({
        "success": True,
        "stage": "3",
        "partial_dir": os.path.abspath(args.partial),
        "coverage": coverage,
        "findings": findings,
        "error_count": len(errors),
        "coherence": coherence.to_json(),
    })


def _stage3_retry_dispatch_handler(args: argparse.Namespace) -> int:
    """`alive demo stage3 retry-dispatch`: build the one-shot retry descriptor."""
    stage3 = _load_stage3()
    try:
        descriptor = stage3.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        findings = stage3.validate_timeline(
            args.partial,
            expected_people=descriptor["expected_people"],
            expected_walnuts=descriptor["expected_walnuts"],
        )
        retry = stage3.retry_dispatch(descriptor, findings)
    except stage3.Stage3Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage3_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "3",
        "partial_dir": os.path.abspath(args.partial),
        "retry": retry,
    })


def _stage3_freeze_handler(args: argparse.Namespace) -> int:
    """`alive demo stage3 freeze`: write stage3_done.json marker."""
    stage3 = _load_stage3()
    try:
        descriptor = stage3.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        marker = stage3.freeze_stage(
            args.partial,
            expected_people=descriptor["expected_people"],
            expected_walnuts=descriptor["expected_walnuts"],
        )
    except stage3.Stage3Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage3_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "3",
        "partial_dir": os.path.abspath(args.partial),
        "marker": marker,
    })


def _stage4_prepare_handler(args: argparse.Namespace) -> int:
    """`alive demo stage4 prepare`: emit the single dispatch descriptor."""
    stage4 = _load_stage4()
    try:
        descriptor = stage4.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
    except stage4.Stage4NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage4_not_ready",
                "message": str(exc),
                "hint": (
                    "Run Stages 0-3 first; freeze the spine, anchor "
                    "envelope, entity scaffolds, and timeline before "
                    "dispatching Stage 4."
                ),
            },
            "_exit_code": 1,
        })
    except stage4.Stage4Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage4_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "4",
        "partial_dir": os.path.abspath(args.partial),
        "dispatch": descriptor,
    })


def _stage4_collect_validate_handler(args: argparse.Namespace) -> int:
    """`alive demo stage4 collect-validate`: file presence + structural validation."""
    stage4 = _load_stage4()
    try:
        descriptor = stage4.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        coverage = stage4.collect_outputs(
            args.partial,
            expected_walnuts=descriptor["expected_walnuts"],
        )
        findings = stage4.validate_insights(
            args.partial,
            expected_walnuts=descriptor["expected_walnuts"],
        )
    except stage4.Stage4NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage4_not_ready",
                "message": str(exc),
                "hint": (
                    "Run Stages 0-3 first before validating Stage 4 outputs."
                ),
            },
            "_exit_code": 1,
        })
    except stage4.Stage4Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage4_error", "message": str(exc)},
            "_exit_code": 1,
        })
    errors = [f for f in findings if f.get("severity") == "error"]
    validate = _load_validate()
    coherence = validate.validate_stage("4", args.partial)
    return _emit({
        "success": True,
        "stage": "4",
        "partial_dir": os.path.abspath(args.partial),
        "coverage": coverage,
        "findings": findings,
        "error_count": len(errors),
        "coherence": coherence.to_json(),
    })


def _stage4_retry_dispatch_handler(args: argparse.Namespace) -> int:
    """`alive demo stage4 retry-dispatch`: build the one-shot retry descriptor."""
    stage4 = _load_stage4()
    try:
        descriptor = stage4.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        findings = stage4.validate_insights(
            args.partial,
            expected_walnuts=descriptor["expected_walnuts"],
        )
        retry = stage4.retry_dispatch(descriptor, findings)
    except stage4.Stage4Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage4_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "4",
        "partial_dir": os.path.abspath(args.partial),
        "retry": retry,
    })


def _stage4_freeze_handler(args: argparse.Namespace) -> int:
    """`alive demo stage4 freeze`: write stage4_done.json marker."""
    stage4 = _load_stage4()
    try:
        descriptor = stage4.prepare_dispatch(
            args.partial,
            world_root=_resolve_world_root(args),
        )
        marker = stage4.freeze_stage(
            args.partial,
            expected_walnuts=descriptor["expected_walnuts"],
        )
    except stage4.Stage4Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage4_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "4",
        "partial_dir": os.path.abspath(args.partial),
        "marker": marker,
    })


# ---------------------------------------------------------------------------
# Stage 5 handlers
# ---------------------------------------------------------------------------

def _stage5_prepare_handler(args: argparse.Namespace) -> int:
    """`alive demo stage5 prepare` -- dry-run plan + pre-check findings."""
    stage5 = _load_stage5()
    scaffold_mod = stage5._scaffold()
    try:
        plan = stage5.prepare_activation(args.partial)
    except scaffold_mod.Stage5NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage5_not_ready",
                "message": str(exc),
                "hint": (
                    "Stages 0-4 must all be frozen (stage{N}_done.json "
                    "with frozen=true) before activation can run."
                ),
            },
            "_exit_code": 1,
        })
    except scaffold_mod.Stage5Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage5_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "5",
        "plan": plan,
    })


def _stage5_run_handler(args: argparse.Namespace) -> int:
    """`alive demo stage5 run` -- execute the 11-step activation transaction."""
    stage5 = _load_stage5()
    scaffold_mod = stage5._scaffold()
    try:
        result = stage5.run_activation(
            args.partial,
            confirm=bool(getattr(args, "confirm", False)),
            plugin_root=getattr(args, "plugin_root", None),
        )
    except scaffold_mod.Stage5NotReady as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "stage5_not_ready",
                "message": str(exc),
                "hint": (
                    "Stages 0-4 must all be frozen before activation."
                ),
            },
            "_exit_code": 1,
        })
    except scaffold_mod.Stage5Error as exc:
        return _emit({
            "success": False,
            "error": {"code": "stage5_error", "message": str(exc)},
            "_exit_code": 1,
        })
    if result.get("status") == "needs_confirmation":
        return _emit({
            "success": False,
            "error": {
                "code": "needs_confirmation",
                "message": (
                    f"{len(result.get('findings', []))} unsaved-work "
                    f"finding(s) on the current live world; re-run with "
                    f"--confirm to proceed."
                ),
                "hint": "alive demo stage5 run --partial <path> --confirm",
            },
            "findings": result.get("findings", []),
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "5",
        "result": result,
    })


def _stage5_verify_handler(args: argparse.Namespace) -> int:
    """`alive demo stage5 verify` -- post-step-10 verification."""
    stage5 = _load_stage5()
    result = stage5.verify_activation(args.world)
    return _emit({
        "success": True,
        "stage": "5",
        "verification": result,
    })


# ---------------------------------------------------------------------------
# Custom-path orchestrator: `alive demo create prepare` (fn-2-2zz.16)
# ---------------------------------------------------------------------------

#: Allowed values for the ``--size`` enum on ``alive demo create prepare``.
#: Each maps to a Stage 0 anchor-count + walnut-roster size hint (see
#: ``templates/demo/stage_prompts/stage_0_spine.v1.md``); the orchestrator
#: stores the choice on the partial-generation entry so a resume can
#: re-render the spine prompt with the same size.
_CREATE_VALID_SIZES = ("small", "medium", "large")


def _create_prepare_handler(args: argparse.Namespace) -> int:
    """``alive demo create prepare`` -- mint a partial dir + stage demo-state.

    Atomic surface that the custom-path orchestrator (``create.md``)
    invokes BEFORE any LLM dispatch. Steps, in order:

      1. Validate ``--description-file`` exists and is readable; reject
         an empty body.
      2. Validate ``--size`` against ``_CREATE_VALID_SIZES``.
      3. ``lib.mint_partial_dir()`` mints a fresh ``wld_<ulid>.partial/``
         directory atomically. ``FileExistsError`` surfaces as
         ``partial_dir_exists``.
      4. Copy the description verbatim to
         ``<partial>/_input/persona-description.md`` via
         ``_common.atomic_write_text`` so the prose orchestrator and
         Stage 0's ``persist_description`` see the same canonical body.
      5. Atomically stage a ``partial_generations[*]`` entry in
         demo-state.json under the demo-state flock with
         ``stage = "0_spine"``, ``status = "in_progress"``, the new
         optional ``size`` / ``description_path`` / ``partial_dir``
         fields, and matching ``started_at`` / ``last_updated``
         timestamps.
      6. Emit the standard CLI envelope (``success``,
         ``rendered_block``, ``partial_dir``, ``partial_ulid``,
         ``next_step``).

    The actual Stage 0 dispatch is OWNED by the orchestrating squirrel
    (``create.md``); this handler only wires up the durable filesystem
    + state-file handles. The split is load-bearing: demo-state staging
    must happen under flock, and the partial-dir ULID is the only
    durable handle for resume/retry, so it has to land on disk before
    any LLM dispatch.
    """
    # --- 1. description-file ------------------------------------------------
    description_path = getattr(args, "description_file", None)
    if not description_path:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": "--description-file is required",
                "hint": (
                    "alive demo create prepare --description-file "
                    "<persona.md> [--size small|medium|large]"
                ),
            },
            "_exit_code": 2,
        })
    abs_description = os.path.abspath(description_path)
    if not os.path.isfile(abs_description):
        return _emit({
            "success": False,
            "error": {
                "code": "description_not_found",
                "message": (
                    f"persona description file not found: {abs_description}"
                ),
                "hint": (
                    "Pass an existing path via --description-file. "
                    "create.md persists the human's persona text to a "
                    "temp path under ~/.config/alive/ before invoking "
                    "this CLI."
                ),
            },
            "_exit_code": 1,
        })
    try:
        with open(abs_description, "r", encoding="utf-8") as f:
            description_text = f.read()
    except OSError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "description_unreadable",
                "message": (
                    f"persona description file at {abs_description} "
                    f"could not be read: {type(exc).__name__}: {exc}"
                ),
            },
            "_exit_code": 1,
        })
    if not description_text.strip():
        return _emit({
            "success": False,
            "error": {
                "code": "description_empty",
                "message": (
                    f"persona description file at {abs_description} "
                    "is empty after stripping whitespace"
                ),
                "hint": (
                    "Stage 0's spine generator needs at least one "
                    "non-blank token to ground the world spine."
                ),
            },
            "_exit_code": 1,
        })

    # --- 2. size enum -------------------------------------------------------
    size = getattr(args, "size", None)
    if size is not None and size not in _CREATE_VALID_SIZES:
        return _emit({
            "success": False,
            "error": {
                "code": "invalid_size",
                "message": (
                    f"--size {size!r} is not one of "
                    f"{_CREATE_VALID_SIZES}"
                ),
                "hint": (
                    "Pass --size small | medium | large, or omit "
                    "--size to let Stage 0 choose."
                ),
            },
            "_exit_code": 2,
        })

    # --- 3. validate demo-state BEFORE creating durable on-disk artifacts ---
    # Pre-check that demo-state.json is loadable + at the right
    # schema_version + the lock can be acquired. This is a dry-run read;
    # we hold no lock yet -- any of the failure modes we care about
    # (corrupt file, schema mismatch, lock contention) are read-time
    # symptoms that show up here without needing to take the write lock.
    # Catching them here means we never mint a partial directory that
    # later gets orphaned because the staging step failed.
    try:
        demo_state.load_state()
    except FlockTimeoutError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "lock_timeout",
                "message": str(exc),
                "hint": (
                    "Another /alive:demo session is updating "
                    "demo-state.json. Wait a few seconds and retry."
                ),
            },
            "_exit_code": 5,
        })
    except demo_state.SchemaVersionMismatch as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "schema_version_mismatch",
                "message": str(exc),
                "hint": "Run `/alive:demo reset` to rebuild demo-state.json.",
                "found": exc.found,
                "expected": exc.expected,
            },
            "_exit_code": 3,
        })
    except demo_state.DemoStateError as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "demo_state_corrupt",
                "message": str(exc),
                "hint": (
                    "demo-state.json is unreadable. Inspect "
                    "`~/.config/alive/demo-state.json` or run "
                    "`/alive:demo reset`."
                ),
            },
            "_exit_code": 1,
        })

    # --- 4. mint partial dir + persist description + stage demo-state.
    #
    # Wrapped in an ``unwind`` block so that ANY failure after the
    # partial directory exists on disk reverts the on-disk write before
    # returning the error envelope. This preserves the documented
    # atomicity contract: either ``alive demo create prepare`` produces
    # both a durable partial dir AND a registered partial_generations
    # row, or it produces neither.
    import shutil  # noqa: PLC0415
    from _common import atomic_write_text, iso_now  # noqa: PLC0415

    partial_dir: Optional[str] = None
    partial_ulid: Optional[str] = None
    try:
        try:
            partial_dir, partial_ulid = demo_lib.mint_partial_dir()
        except FileExistsError as exc:
            return _emit({
                "success": False,
                "error": {
                    "code": "partial_dir_exists",
                    "message": str(exc),
                    "hint": (
                        "A previous run minted the same partial path. "
                        "Inspect $ALIVE_DEMO_BASE_DIR (default "
                        "~/.alive-demos/) and remove the stale "
                        "wld_<ulid>.partial/ directory if it is no "
                        "longer needed; otherwise resume via "
                        "`alive demo resume <ulid>`."
                    ),
                },
                "_exit_code": 1,
            })
        except OSError as exc:
            return _emit({
                "success": False,
                "error": {
                    "code": "partial_dir_unwritable",
                    "message": (
                        f"could not create partial directory: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "hint": (
                        "Check $ALIVE_DEMO_BASE_DIR (default "
                        "~/.alive-demos/) for permissions / disk-full "
                        "issues."
                    ),
                },
                "_exit_code": 1,
            })

        persona_path = os.path.join(
            partial_dir, "_input", "persona-description.md"
        )
        try:
            atomic_write_text(persona_path, description_text)
        except OSError as exc:
            # Unwind: drop the just-minted partial dir so we leave no
            # orphan on disk.
            shutil.rmtree(partial_dir, ignore_errors=True)
            return _emit({
                "success": False,
                "error": {
                    "code": "persona_write_failed",
                    "message": (
                        f"could not persist persona description to "
                        f"{persona_path}: {type(exc).__name__}: {exc}"
                    ),
                },
                "_exit_code": 1,
            })

        started_at = iso_now()
        new_entry = {
            "ulid": partial_ulid,
            "label": demo_lib.derive_label(description_text) or partial_ulid,
            "stage": "0_spine",
            "started_at": started_at,
            "last_updated": started_at,
            "status": "in_progress",
            "size": size,
            "description_path": persona_path,
            "partial_dir": partial_dir,
        }
        try:
            with demo_state.with_locked_state() as state:
                partials = state.get("partial_generations") or []
                demo_state.upsert_partial(partials, new_entry)
                state["partial_generations"] = partials
        except FlockTimeoutError as exc:
            shutil.rmtree(partial_dir, ignore_errors=True)
            return _emit({
                "success": False,
                "error": {
                    "code": "lock_timeout",
                    "message": str(exc),
                    "hint": (
                        "Another /alive:demo session is updating "
                        "demo-state.json. Wait a few seconds and retry."
                    ),
                },
                "_exit_code": 5,
            })
        except demo_state.SchemaVersionMismatch as exc:
            shutil.rmtree(partial_dir, ignore_errors=True)
            return _emit({
                "success": False,
                "error": {
                    "code": "schema_version_mismatch",
                    "message": str(exc),
                    "hint": (
                        "Run `/alive:demo reset` to rebuild "
                        "demo-state.json."
                    ),
                    "found": exc.found,
                    "expected": exc.expected,
                },
                "_exit_code": 3,
            })
        except demo_state.DemoStateError as exc:
            shutil.rmtree(partial_dir, ignore_errors=True)
            return _emit({
                "success": False,
                "error": {
                    "code": "demo_state_corrupt",
                    "message": str(exc),
                    "hint": (
                        "demo-state.json is unreadable. Inspect "
                        "`~/.config/alive/demo-state.json` or run "
                        "`/alive:demo reset`."
                    ),
                },
                "_exit_code": 1,
            })
    except BaseException:
        # Last-resort cleanup on any unexpected exception (KeyboardInterrupt,
        # SystemExit, etc). Without this, an interrupt mid-stage would
        # leave the partial dir orphaned.
        if partial_dir is not None and os.path.isdir(partial_dir):
            shutil.rmtree(partial_dir, ignore_errors=True)
        raise

    # --- 6. envelope --------------------------------------------------------
    body_lines = [
        f"partial_ulid: {partial_ulid}",
        f"partial_dir:  {partial_dir}",
    ]
    if size is not None:
        body_lines.append(f"size:         {size}")
    body_lines.append("")
    body_lines.append("Stage 0 spine dispatch is the next step;")
    body_lines.append("the orchestrating squirrel owns the Agent tool call.")
    rendered_block = demo_lib.format_block(
        "demo create prepared", "\n".join(body_lines)
    )

    return _emit({
        "success": True,
        "stage": "0",
        "partial_dir": partial_dir,
        "partial_ulid": partial_ulid,
        "description_path": persona_path,
        "size": size,
        "label": new_entry["label"],
        "started_at": started_at,
        "next_step": "stage 0 spine dispatch (orchestrated by create.md)",
        "rendered_block": rendered_block,
    })


# ---------------------------------------------------------------------------
# Preset (sandbox-testing) handlers (fn-2-2zz.11)
# ---------------------------------------------------------------------------

def _preset_prepare_handler(args: argparse.Namespace) -> int:
    """`alive demo preset prepare` -- validate preset + emit activation plan.

    No filesystem writes; surfaces ``preset_not_found`` when the preset
    directory or its manifest is missing so the skill can route the
    user to the custom path.
    """
    preset = _load_preset()
    try:
        plan = preset.prepare_preset(args.preset)
    except preset.PresetNotFound as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "preset_not_found",
                "message": str(exc),
                "hint": (
                    "Use /alive:demo create then choose Custom "
                    "(persona-driven), or restore the preset content "
                    "under plugins/alive/skills/demo/preset/."
                ),
            },
            "_exit_code": 1,
        })
    except preset.PresetError as exc:
        return _emit({
            "success": False,
            "error": {"code": "preset_error", "message": str(exc)},
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "preset",
        "plan": plan,
    })


def _preset_run_handler(args: argparse.Namespace) -> int:
    """`alive demo preset run` -- execute the preset activation transaction."""
    preset = _load_preset()
    try:
        result = preset.run_preset(
            args.preset,
            confirm=bool(getattr(args, "confirm", False)),
            plugin_root=getattr(args, "plugin_root", None),
        )
    except preset.PresetNotFound as exc:
        return _emit({
            "success": False,
            "error": {
                "code": "preset_not_found",
                "message": str(exc),
                "hint": "alive demo preset prepare --preset <name>",
            },
            "_exit_code": 1,
        })
    except preset.PresetError as exc:
        return _emit({
            "success": False,
            "error": {"code": "preset_error", "message": str(exc)},
            "_exit_code": 1,
        })
    if result.get("status") == "needs_confirmation":
        return _emit({
            "success": False,
            "error": {
                "code": "needs_confirmation",
                "message": (
                    f"{len(result.get('findings', []))} unsaved-work "
                    f"finding(s) on the current live world; re-run with "
                    f"--confirm to proceed."
                ),
                "hint": "alive demo preset run --preset <name> --confirm",
            },
            "findings": result.get("findings", []),
            "_exit_code": 1,
        })
    return _emit({
        "success": True,
        "stage": "preset",
        "result": result,
    })


def _preset_verify_handler(args: argparse.Namespace) -> int:
    """`alive demo preset verify` -- Read-Before-Speaking + activation checks."""
    preset = _load_preset()
    result = preset.verify_preset(args.world)
    return _emit({
        "success": True,
        "stage": "preset",
        "verification": result,
    })


def _load_validate():
    """Load `validate.py` under a namespaced sys.modules key."""
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.validate"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, "validate.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _validate_handler(args: argparse.Namespace) -> int:
    """`alive demo validate <stage_id> --partial <path>`.

    Emits the unified ValidationResult JSON for the parent squirrel.
    Stage IDs: 0, 2, 3, 4. Stage 1 is UX-only and rejected as a usage
    error. The handler does NOT manage retries; the squirrel does that
    by reading `format_retry_feedback` and feeding it into the
    per-stage `retry_dispatch` helper.
    """
    validate = _load_validate()
    stage_id = args.stage
    partial = args.partial
    if stage_id not in validate.SUPPORTED_STAGES:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": (
                    f"stage_id must be one of {validate.SUPPORTED_STAGES}; "
                    f"got {stage_id!r}. Stage 1 is UX-only with no validator."
                ),
                "hint": "alive demo validate 0 --partial <path>",
            },
            "_exit_code": 2,
        })
    try:
        result = validate.validate_stage(stage_id, partial)
    except (ValueError, TypeError) as exc:
        return _emit({
            "success": False,
            "error": {"code": "usage", "message": str(exc)},
            "_exit_code": 2,
        })
    return _emit({
        "success": True,
        "stage": stage_id,
        "partial_dir": os.path.abspath(partial),
        "result": result.to_json(),
    })


def _resume_handler(args: argparse.Namespace) -> int:
    """`alive demo resume [partial_id]` -- offer retry-from-failed-stage.

    Failure-recovery surface (fn-2-2zz.13). Reads ``demo-state.json``,
    finds partials whose ``status`` is ``in_progress`` AND that carry a
    ``failed_at_stage`` marker (set by the failure-mode handlers in
    ``lib.py``). Three response shapes:

      * No resumable partials: success envelope with a friendly bordered
        block telling the user there's nothing to resume.
      * Exactly one resumable: returns ``{partial_id, failed_at_stage,
        failed_reason, suggested_action, rendered_block}`` so the
        squirrel prose drives the actual retry (the CLI does not retry
        autonomously; the per-stage retry primitives need a dispatch
        callable that only the runtime supplies).
      * Multiple resumable AND no ``partial_id`` arg: ambiguous-list
        envelope with a picker-style block. The user re-invokes
        ``alive demo resume <partial_id>`` after picking.

    A specific ``partial_id`` is resolved by exact match against the
    state's partial-generation ulids; an unknown id returns a
    ``not_found`` envelope.
    """
    try:
        state = _load_state_envelope()
    except _EnvelopeExit as exit_signal:
        return exit_signal.exit_code

    resumable = [
        entry
        for entry in state.get("partial_generations", [])
        if entry.get("status") == "in_progress"
        and entry.get("failed_at_stage")
    ]
    resumable.sort(key=lambda e: e.get("last_updated") or "", reverse=True)

    target_id = getattr(args, "partial_id", None)

    if not resumable:
        rendered = demo_lib.format_block(
            "nothing to resume",
            (
                "No partial generations are flagged for resume.\n"
                "\n"
                "Run `alive demo list` to see all partial + active worlds.\n"
                "Run /alive:demo to start a fresh generation."
            ),
        )
        return _emit({
            "success": True,
            "resumable": [],
            "rendered_block": rendered,
        })

    if target_id is None and len(resumable) > 1:
        body_lines = [
            f"{len(resumable)} resumable partial generation(s):",
            "",
        ]
        for i, entry in enumerate(resumable, 1):
            ulid = entry.get("ulid", "?")
            label = entry.get("label", "(unknown)")
            stage = entry.get("failed_at_stage", "?")
            reason = entry.get("failed_reason", "?")
            body_lines.append(
                f"  {i}. {label}  -  {ulid}  (failed at {stage}, reason: {reason})"
            )
        body_lines.append("")
        body_lines.append("Pick one and re-run:")
        body_lines.append("  alive demo resume <ulid>")
        rendered = demo_lib.format_block(
            "multiple resumable partials",
            "\n".join(body_lines),
        )
        return _emit({
            "success": True,
            "ambiguous": True,
            "resumable": list(resumable),
            "rendered_block": rendered,
        })

    if target_id is not None:
        match = next(
            (e for e in resumable if e.get("ulid") == target_id),
            None,
        )
        if match is None:
            return _emit({
                "success": False,
                "error": {
                    "code": "not_found",
                    "message": f"no resumable partial with ulid {target_id!r}",
                    "hint": (
                        "Run `alive demo resume` (no arg) to list resumable "
                        "partials, or `alive demo list` to see all partials."
                    ),
                },
                "_exit_code": 1,
            })
        entry = match
    else:
        entry = resumable[0]

    failed_stage = entry.get("failed_at_stage", "?")
    failed_reason = entry.get("failed_reason", "?")
    label = entry.get("label", "(unknown)")
    ulid = entry.get("ulid", "?")

    suggested = _suggest_retry_action(failed_stage, failed_reason)

    body_lines = [
        f"Resumable partial: {label}  -  {ulid}",
        f"  failed at: {failed_stage}",
        f"  reason:    {failed_reason}",
        f"  failed at: {entry.get('failed_at', '(unknown)')}",
        "",
        "Suggested next action:",
        f"  {suggested}",
        "",
        "Once you've retried successfully, the failure markers clear",
        "automatically. To abandon this partial without retrying, run",
        "`alive demo delete <ulid>`.",
    ]
    rendered = demo_lib.format_block(
        f"resume {label}",
        "\n".join(body_lines),
    )
    return _emit({
        "success": True,
        "partial_id": ulid,
        "label": label,
        "failed_at_stage": failed_stage,
        "failed_reason": failed_reason,
        "failed_at": entry.get("failed_at"),
        "suggested_action": suggested,
        "rendered_block": rendered,
    })


def _suggest_retry_action(failed_stage: str, failed_reason: str) -> str:
    """Map (stage, reason) to a human-readable retry instruction.

    The CLI cannot autonomously retry an LLM stage (the stage retries
    require a dispatch callable that only the runtime owns). What it can
    do is point the squirrel at the right entry point so the prose
    response drives the next subagent invocation.
    """
    if failed_reason == "atomic_write_failure":
        return (
            "Re-run the failed activation step once disk / permission "
            "issue is resolved. demo-state.json is intact."
        )
    if failed_stage == "5_promote":
        return (
            "Re-run `alive demo stage5 run --partial <partial-dir>` "
            "(or `alive demo activate <ref>` for an existing world)."
        )
    if failed_stage == "0_spine":
        return (
            "Re-dispatch Stage 0 via `/alive:demo` (the skill router "
            "calls run_stage0 with the persona description on disk)."
        )
    if failed_stage == "2_entities":
        return (
            "Re-run `alive demo stage2 retry-dispatch --partial <partial-dir>` "
            "to rebuild dispatch descriptors; the squirrel re-fires the "
            "subagents and re-runs collect-validate."
        )
    if failed_stage == "3_timeline":
        return (
            "Re-run `alive demo stage3 retry-dispatch --partial <partial-dir>` "
            "to rebuild the dispatch descriptor; the squirrel re-fires "
            "the subagent and re-runs collect-validate."
        )
    if failed_stage == "4_insights":
        return (
            "Re-run `alive demo stage4 retry-dispatch --partial <partial-dir>` "
            "to rebuild the dispatch descriptor; the squirrel re-fires "
            "the subagent and re-runs collect-validate."
        )
    return (
        "Re-run the appropriate stage entry point. See "
        "`plugins/alive/skills/demo/SKILL.md` `## Failure recovery`."
    )


def _not_implemented_handler(name: str):
    """Factory for stub handlers that emit a structured `not_implemented` envelope."""
    def _handler(_args: argparse.Namespace) -> int:
        return _emit({
            "success": False,
            "error": {
                "code": "not_implemented",
                "message": (
                    f"`alive demo {name}` is registered but its body lands "
                    f"in fn-2-2zz.12. fn-2-2zz.3 ships the scaffold only."
                ),
                "hint": (
                    "Use `alive demo status` to inspect demo-state.json "
                    "in this release."
                ),
            },
            "_exit_code": 1,
        })
    return _handler


# ---------------------------------------------------------------------------
# Argparse error handler — surface JSON instead of stderr text
# ---------------------------------------------------------------------------

def _json_error_handler(parser: argparse.ArgumentParser):
    """Replace `parser.error` with a JSON-emitting equivalent (matches log.py / promote.py)."""
    def _err(message: str) -> None:
        _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": message,
                "hint": parser.format_usage().strip(),
            },
            "_exit_code": 2,
        })
        sys.exit(2)
    return _err


# ---------------------------------------------------------------------------
# Group-level missing-subcommand fallback
# ---------------------------------------------------------------------------

def _demo_group_missing_subcommand(_args: argparse.Namespace) -> int:
    """Handler for bare `alive demo` invocation."""
    return _emit({
        "success": False,
        "error": {
            "code": "usage",
            "message": (
                "`alive demo` requires a subcommand. User-facing: list, "
                "activate, deactivate, delete, status, resume. Pipeline: "
                "create, stage2, stage3, stage4, stage5, preset, "
                "validate."
            ),
            "hint": (
                "alive demo status (current state) | "
                "alive demo resume (failure recovery) | "
                "alive demo preset prepare (sandbox-testing world) | "
                "alive demo create prepare --description-file <path>"
            ),
        },
        "_exit_code": 2,
    })


# ---------------------------------------------------------------------------
# Registration entry point
# ---------------------------------------------------------------------------

def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Append the `demo` subcommand group to a top-level subparsers object.

    Mirrors the registration pattern used by `log.py` / `promote.py`:

      * `alive demo` lands on `_demo_group_missing_subcommand` (JSON envelope).
      * Each leaf subcommand sets `_handler` via `set_defaults` so
        `cli.py:main` can dispatch generically.
      * `parser.error` is wired to emit JSON, never stderr text.
      * `SCHEMA_METADATA` is stashed under `_schema_metadata` for `alive schema`.

    Subcommand bodies (`list`, `activate`, `deactivate`, `delete`,
    `status`) are SHIPPED in fn-2-2zz.12 -- this register call wires the
    argparse surface for both the user-facing subcommands AND the
    pipeline-internal ones (stage2 / stage3 / stage4 / stage5 / preset /
    validate).
    """
    parser = subparsers.add_parser(
        "demo",
        help="Manage /alive:demo generated worlds.",
        description=(
            "Manage demo worlds scaffolded by the /alive:demo skill: list, "
            "activate, deactivate, delete, status. Subcommand bodies land "
            "in fn-2-2zz.12; this group registers the dispatch surface."
        ),
    )
    parser.error = _json_error_handler(parser)  # type: ignore[assignment]

    demo_subparsers = parser.add_subparsers(dest="demo_command")
    demo_subparsers.required = False

    # Lazy import of the schema-metadata key sentinel; matches the
    # log.py / promote.py pattern (avoids circular import at file load).
    from schema import SCHEMA_METADATA_DEFAULT_KEY  # noqa: PLC0415, E402

    # ---- alive demo status -------------------------------------------------
    status = demo_subparsers.add_parser(
        "status",
        help="Print loaded + self-healed demo-state.json.",
    )
    status.error = _json_error_handler(status)  # type: ignore[assignment]
    status.set_defaults(
        _handler=_status_handler,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )

    # ---- alive demo list ---------------------------------------------------
    listp = demo_subparsers.add_parser(
        "list",
        help="List partial + active demo worlds (body: fn-2-2zz.12).",
    )
    listp.error = _json_error_handler(listp)  # type: ignore[assignment]
    listp.set_defaults(
        _handler=_list_handler,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )

    # ---- alive demo activate <ref> [--confirm] ----------------------------
    activate = demo_subparsers.add_parser(
        "activate",
        help="Re-activate a previously-promoted demo world by ref.",
        description=(
            "Re-activate a demo world. ref resolves via 3-step fallback: "
            "exact label match, then ULID prefix (>=3 chars), then "
            "ambiguous-match envelope (squirrel drives picker). Pass "
            "--confirm to acknowledge activation pre-check findings on a "
            "live world with uncommitted work."
        ),
    )
    activate.add_argument(
        "ref",
        help="Demo world label or ULID prefix (>=3 chars).",
    )
    activate.add_argument(
        "--confirm",
        action="store_true",
        help="Acknowledge pre-check findings on a live world with uncommitted work.",
    )
    activate.error = _json_error_handler(activate)  # type: ignore[assignment]
    activate.set_defaults(
        _handler=_activate_handler,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )

    # ---- alive demo deactivate ---------------------------------------------
    deactivate = demo_subparsers.add_parser(
        "deactivate",
        help="Restore the previous world-root pointer.",
        description=(
            "Restore ~/.config/alive/world-root to the value cached as "
            "previous_world_root in demo-state.json, then clear "
            "active_world. Pointer flip is the single commit point."
        ),
    )
    deactivate.error = _json_error_handler(deactivate)  # type: ignore[assignment]
    deactivate.set_defaults(
        _handler=_deactivate_handler,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )

    # ---- alive demo delete <ref> [--confirm] ------------------------------
    delete = demo_subparsers.add_parser(
        "delete",
        help="Delete a demo world from disk (irreversible).",
        description=(
            "Resolve ref (label or ULID prefix), refuse if active, "
            "then shutil.rmtree the world directory. Without --confirm, "
            "returns a dry-run summary; with --confirm, proceeds. "
            "Active worlds must be deactivated first."
        ),
    )
    delete.add_argument(
        "ref",
        help="Demo world label or ULID prefix (>=3 chars).",
    )
    delete.add_argument(
        "--confirm",
        action="store_true",
        help="Acknowledge irreversibility and proceed with deletion.",
    )
    delete.error = _json_error_handler(delete)  # type: ignore[assignment]
    delete.set_defaults(
        _handler=_delete_handler,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )

    # ---- alive demo resume [partial_id] ----------------------------------
    resume = demo_subparsers.add_parser(
        "resume",
        help="List / pick a resumable partial generation (failure recovery).",
        description=(
            "Failure recovery (fn-2-2zz.13). Reads demo-state.json for "
            "partials whose status is in_progress AND that carry a "
            "failed_at_stage marker. With no arg, lists; with one arg, "
            "returns the retry plan for that ulid."
        ),
    )
    resume.add_argument(
        "partial_id",
        nargs="?",
        default=None,
        help="Optional ulid (wld_<...>) of a specific resumable partial.",
    )
    resume.error = _json_error_handler(resume)  # type: ignore[assignment]
    resume.set_defaults(
        _handler=_resume_handler,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )

    # ---- alive demo create -------------------------------------------------
    create = demo_subparsers.add_parser(
        "create",
        help="Custom-path orchestrator entry (fn-2-2zz.16).",
        description=(
            "Mint the partial directory, persist the persona "
            "description, and atomically stage a partial_generations "
            "entry in demo-state.json BEFORE any LLM dispatch. The "
            "orchestrating squirrel (skills/demo/create.md) drives "
            "Stage 0 dispatch, Stage 1 anchor confirmation, and the "
            "Stage 2-5 driver chain after this CLI returns."
        ),
    )
    create.error = _json_error_handler(create)  # type: ignore[assignment]
    create_subparsers = create.add_subparsers(dest="create_command")
    create_subparsers.required = False

    cp_prepare = create_subparsers.add_parser(
        "prepare",
        help="Mint partial dir + stage demo-state for the custom path.",
        description=(
            "Atomic surface invoked by create.md before any subagent "
            "dispatch. Validates --description-file + --size, mints "
            "<base>/wld_<ulid>.partial/, persists the persona text "
            "verbatim at _input/persona-description.md, and stages a "
            "partial_generations[*] entry under the demo-state flock."
        ),
    )
    cp_prepare.add_argument(
        "--description-file",
        required=True,
        help=(
            "Absolute path to a file carrying the human's persona "
            "description (the orchestrator persists the text to a "
            "temp path under ~/.config/alive/ before invoking)."
        ),
    )
    cp_prepare.add_argument(
        "--size",
        choices=list(_CREATE_VALID_SIZES),
        default=None,
        help=(
            "Optional spine size hint. Maps to Stage 0's anchor count "
            "+ walnut roster size guidance. Omit to let Stage 0 "
            "choose."
        ),
    )
    cp_prepare.error = _json_error_handler(cp_prepare)  # type: ignore[assignment]
    cp_prepare.set_defaults(_handler=_create_prepare_handler)

    def _create_missing_subcommand(_args: argparse.Namespace) -> int:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": (
                    "`alive demo create` requires a subcommand: prepare."
                ),
                "hint": (
                    "alive demo create prepare --description-file "
                    "<persona.md> [--size small|medium|large]"
                ),
            },
            "_exit_code": 2,
        })
    create.set_defaults(_handler=_create_missing_subcommand)

    # ---- alive demo stage2 ------------------------------------------------
    stage2 = demo_subparsers.add_parser(
        "stage2",
        help="Stage 2 entity prose subagents (parallel dispatch).",
        description=(
            "Stage 2 of the /alive:demo generation pipeline. Subcommands: "
            "prepare (emit dispatch descriptors), collect-validate "
            "(coverage + findings), retry-dispatch (build retry "
            "descriptors), freeze (write stage2_done.json)."
        ),
    )
    stage2.error = _json_error_handler(stage2)  # type: ignore[assignment]
    stage2_subparsers = stage2.add_subparsers(dest="stage2_command")
    stage2_subparsers.required = False

    s2_prepare = stage2_subparsers.add_parser(
        "prepare",
        help="Build per-entity dispatch descriptors from the frozen spine + anchors.",
    )
    s2_prepare.add_argument("--partial", required=True, help="Partial directory path.")
    s2_prepare.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s2_prepare.error = _json_error_handler(s2_prepare)  # type: ignore[assignment]
    s2_prepare.set_defaults(_handler=_stage2_prepare_handler)

    s2_collect = stage2_subparsers.add_parser(
        "collect-validate",
        help="Walk entities/<slug>/ and run hand-rolled stdlib validation.",
    )
    s2_collect.add_argument("--partial", required=True, help="Partial directory path.")
    s2_collect.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s2_collect.error = _json_error_handler(s2_collect)  # type: ignore[assignment]
    s2_collect.set_defaults(_handler=_stage2_collect_validate_handler)

    s2_retry = stage2_subparsers.add_parser(
        "retry-dispatch",
        help="Build retry descriptors for slugs whose validation failed.",
    )
    s2_retry.add_argument("--partial", required=True, help="Partial directory path.")
    s2_retry.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s2_retry.error = _json_error_handler(s2_retry)  # type: ignore[assignment]
    s2_retry.set_defaults(_handler=_stage2_retry_dispatch_handler)

    s2_freeze = stage2_subparsers.add_parser(
        "freeze",
        help="Write _stage_outputs/stage2_done.json after validation succeeds.",
    )
    s2_freeze.add_argument("--partial", required=True, help="Partial directory path.")
    s2_freeze.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s2_freeze.error = _json_error_handler(s2_freeze)  # type: ignore[assignment]
    s2_freeze.set_defaults(_handler=_stage2_freeze_handler)

    def _stage2_missing_subcommand(_args: argparse.Namespace) -> int:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": (
                    "`alive demo stage2` requires a subcommand: prepare, "
                    "collect-validate, retry-dispatch, freeze."
                ),
                "hint": "alive demo stage2 prepare --partial <path>",
            },
            "_exit_code": 2,
        })
    stage2.set_defaults(_handler=_stage2_missing_subcommand)

    # ---- alive demo stage3 ------------------------------------------------
    stage3 = demo_subparsers.add_parser(
        "stage3",
        help="Stage 3 timeline materialisation (single subagent).",
        description=(
            "Stage 3 of the /alive:demo generation pipeline. Subcommands: "
            "prepare (emit single dispatch descriptor), collect-validate "
            "(coverage + findings), retry-dispatch (build retry descriptor), "
            "freeze (write stage3_done.json)."
        ),
    )
    stage3.error = _json_error_handler(stage3)  # type: ignore[assignment]
    stage3_subparsers = stage3.add_subparsers(dest="stage3_command")
    stage3_subparsers.required = False

    s3_prepare = stage3_subparsers.add_parser(
        "prepare",
        help="Build the single Stage 3 dispatch descriptor.",
    )
    s3_prepare.add_argument("--partial", required=True, help="Partial directory path.")
    s3_prepare.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s3_prepare.error = _json_error_handler(s3_prepare)  # type: ignore[assignment]
    s3_prepare.set_defaults(_handler=_stage3_prepare_handler)

    s3_collect = stage3_subparsers.add_parser(
        "collect-validate",
        help="Walk Stage 3 outputs and run hand-rolled stdlib validation.",
    )
    s3_collect.add_argument("--partial", required=True, help="Partial directory path.")
    s3_collect.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s3_collect.error = _json_error_handler(s3_collect)  # type: ignore[assignment]
    s3_collect.set_defaults(_handler=_stage3_collect_validate_handler)

    s3_retry = stage3_subparsers.add_parser(
        "retry-dispatch",
        help="Build a one-shot retry descriptor with feedback appended.",
    )
    s3_retry.add_argument("--partial", required=True, help="Partial directory path.")
    s3_retry.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s3_retry.error = _json_error_handler(s3_retry)  # type: ignore[assignment]
    s3_retry.set_defaults(_handler=_stage3_retry_dispatch_handler)

    s3_freeze = stage3_subparsers.add_parser(
        "freeze",
        help="Write _stage_outputs/stage3_done.json after validation succeeds.",
    )
    s3_freeze.add_argument("--partial", required=True, help="Partial directory path.")
    s3_freeze.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s3_freeze.error = _json_error_handler(s3_freeze)  # type: ignore[assignment]
    s3_freeze.set_defaults(_handler=_stage3_freeze_handler)

    def _stage3_missing_subcommand(_args: argparse.Namespace) -> int:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": (
                    "`alive demo stage3` requires a subcommand: prepare, "
                    "collect-validate, retry-dispatch, freeze."
                ),
                "hint": "alive demo stage3 prepare --partial <path>",
            },
            "_exit_code": 2,
        })
    stage3.set_defaults(_handler=_stage3_missing_subcommand)

    # ---- alive demo stage4 ------------------------------------------------
    stage4 = demo_subparsers.add_parser(
        "stage4",
        help="Stage 4 insights synthesis (single subagent).",
        description=(
            "Stage 4 of the /alive:demo generation pipeline. Subcommands: "
            "prepare (emit single dispatch descriptor), collect-validate "
            "(coverage + findings), retry-dispatch (build retry descriptor), "
            "freeze (write stage4_done.json)."
        ),
    )
    stage4.error = _json_error_handler(stage4)  # type: ignore[assignment]
    stage4_subparsers = stage4.add_subparsers(dest="stage4_command")
    stage4_subparsers.required = False

    s4_prepare = stage4_subparsers.add_parser(
        "prepare",
        help="Build the single Stage 4 dispatch descriptor.",
    )
    s4_prepare.add_argument("--partial", required=True, help="Partial directory path.")
    s4_prepare.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s4_prepare.error = _json_error_handler(s4_prepare)  # type: ignore[assignment]
    s4_prepare.set_defaults(_handler=_stage4_prepare_handler)

    s4_collect = stage4_subparsers.add_parser(
        "collect-validate",
        help="Walk Stage 4 outputs and run hand-rolled stdlib validation.",
    )
    s4_collect.add_argument("--partial", required=True, help="Partial directory path.")
    s4_collect.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s4_collect.error = _json_error_handler(s4_collect)  # type: ignore[assignment]
    s4_collect.set_defaults(_handler=_stage4_collect_validate_handler)

    s4_retry = stage4_subparsers.add_parser(
        "retry-dispatch",
        help="Build a one-shot retry descriptor with feedback appended.",
    )
    s4_retry.add_argument("--partial", required=True, help="Partial directory path.")
    s4_retry.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s4_retry.error = _json_error_handler(s4_retry)  # type: ignore[assignment]
    s4_retry.set_defaults(_handler=_stage4_retry_dispatch_handler)

    s4_freeze = stage4_subparsers.add_parser(
        "freeze",
        help="Write _stage_outputs/stage4_done.json after validation succeeds.",
    )
    s4_freeze.add_argument("--partial", required=True, help="Partial directory path.")
    s4_freeze.add_argument(
        "--world-root", default=None,
        help="World root for subagent brief substitution (defaults to --partial).",
    )
    s4_freeze.error = _json_error_handler(s4_freeze)  # type: ignore[assignment]
    s4_freeze.set_defaults(_handler=_stage4_freeze_handler)

    def _stage4_missing_subcommand(_args: argparse.Namespace) -> int:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": (
                    "`alive demo stage4` requires a subcommand: prepare, "
                    "collect-validate, retry-dispatch, freeze."
                ),
                "hint": "alive demo stage4 prepare --partial <path>",
            },
            "_exit_code": 2,
        })
    stage4.set_defaults(_handler=_stage4_missing_subcommand)

    # ---- alive demo stage5 ------------------------------------------------
    stage5 = demo_subparsers.add_parser(
        "stage5",
        help="Stage 5 deterministic activation transaction (11 steps).",
        description=(
            "Stage 5 of the /alive:demo generation pipeline. Subcommands: "
            "prepare (dry-run validator + pre-check), run (execute the "
            "11-step activation transaction with single commit point at "
            "step 11), verify (post-step-11 verification)."
        ),
    )
    stage5.error = _json_error_handler(stage5)  # type: ignore[assignment]
    stage5_subparsers = stage5.add_subparsers(dest="stage5_command")
    stage5_subparsers.required = False

    s5_prepare = stage5_subparsers.add_parser(
        "prepare",
        help="Dry-run validator: stage{0..4}_done markers + activation_pre_check.",
        description=(
            "Dry-run Stage 5: validate that every stage{0..4}_done.json "
            "marker is frozen and emit the activation pre-check findings. "
            "No filesystem writes."
        ),
    )
    s5_prepare.add_argument("--partial", required=True, help="Partial directory path.")
    s5_prepare.error = _json_error_handler(s5_prepare)  # type: ignore[assignment]
    s5_prepare.set_defaults(_handler=_stage5_prepare_handler)

    s5_run = stage5_subparsers.add_parser(
        "run",
        help="Execute the 11-step activation transaction.",
        description=(
            "Execute the Stage 5 11-step activation transaction. Single "
            "commit point at step 11 (atomic write of "
            "~/.config/alive/world-root). Pass --confirm to acknowledge "
            "pre-check findings on a live world with uncommitted work."
        ),
    )
    s5_run.add_argument("--partial", required=True, help="Partial directory path.")
    s5_run.add_argument(
        "--confirm",
        action="store_true",
        help="Acknowledge pre-check findings on a live world with uncommitted work.",
    )
    s5_run.error = _json_error_handler(s5_run)  # type: ignore[assignment]
    s5_run.set_defaults(_handler=_stage5_run_handler)

    s5_verify = stage5_subparsers.add_parser(
        "verify",
        help="Post-step-11 verification of an activated demo world.",
        description=(
            "Post-step-11 verification: confirm the world-root pointer "
            "matches the demo-state active world, walnut now.json "
            "projections present, and .alive/_index.{yaml,json} built."
        ),
    )
    s5_verify.add_argument("--world", required=True, help="Activated demo world path.")
    s5_verify.error = _json_error_handler(s5_verify)  # type: ignore[assignment]
    s5_verify.set_defaults(_handler=_stage5_verify_handler)

    def _stage5_missing_subcommand(_args: argparse.Namespace) -> int:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": (
                    "`alive demo stage5` requires a subcommand: prepare, "
                    "run, verify."
                ),
                "hint": "alive demo stage5 prepare --partial <path>",
            },
            "_exit_code": 2,
        })
    stage5.set_defaults(_handler=_stage5_missing_subcommand)

    # ---- alive demo preset ------------------------------------------------
    preset = demo_subparsers.add_parser(
        "preset",
        help="Sandbox-testing preset path (deterministic, no LLM).",
        description=(
            "Sandbox-testing preset path: scaffold a fully-baked Nova "
            "Station demo world without firing the LLM pipeline. "
            "Subcommands: prepare (dry-run validator + pre-check), run "
            "(execute the activation transaction), verify (post-step-11 "
            "Read-Before-Speaking + pointer/state check)."
        ),
    )
    preset.error = _json_error_handler(preset)  # type: ignore[assignment]
    preset_subparsers = preset.add_subparsers(dest="preset_command")
    preset_subparsers.required = False

    pp_prepare = preset_subparsers.add_parser(
        "prepare",
        help="Validate the preset directory + emit an activation plan (no writes).",
    )
    pp_prepare.add_argument(
        "--preset",
        default="realistic-seeded",
        help="Preset name (filesystem stem under preset/). Default: realistic-seeded.",
    )
    pp_prepare.error = _json_error_handler(pp_prepare)  # type: ignore[assignment]
    pp_prepare.set_defaults(_handler=_preset_prepare_handler)

    pp_run = preset_subparsers.add_parser(
        "run",
        help="Execute the preset activation transaction (11 steps, single commit).",
        description=(
            "Execute the preset activation transaction. Steps 3, 7, 8, 10, 11 "
            "share the Stage 5 helpers; steps 4 and 9 are preset-specific; "
            "steps 5 and 6 are skipped (preset ships pre-baked "
            "completed.json + a fully-canonical walnut tree). Pass --confirm "
            "to acknowledge pre-check findings on a live world with "
            "uncommitted work."
        ),
    )
    pp_run.add_argument(
        "--preset",
        default="realistic-seeded",
        help="Preset name. Default: realistic-seeded.",
    )
    pp_run.add_argument(
        "--confirm",
        action="store_true",
        help="Acknowledge pre-check findings on a live world with uncommitted work.",
    )
    pp_run.error = _json_error_handler(pp_run)  # type: ignore[assignment]
    pp_run.set_defaults(_handler=_preset_run_handler)

    pp_verify = preset_subparsers.add_parser(
        "verify",
        help="Post-activation verification (Read-Before-Speaking contract).",
        description=(
            "Post-activation verification of a preset-activated world: "
            "world-root pointer matches, demo-state matches, every walnut "
            "has all five Read-Before-Speaking kernel files present + "
            "readable, index files built."
        ),
    )
    pp_verify.add_argument("--world", required=True, help="Activated demo world path.")
    pp_verify.error = _json_error_handler(pp_verify)  # type: ignore[assignment]
    pp_verify.set_defaults(_handler=_preset_verify_handler)

    def _preset_missing_subcommand(_args: argparse.Namespace) -> int:
        return _emit({
            "success": False,
            "error": {
                "code": "usage",
                "message": (
                    "`alive demo preset` requires a subcommand: prepare, "
                    "run, verify."
                ),
                "hint": "alive demo preset prepare --preset realistic-seeded",
            },
            "_exit_code": 2,
        })
    preset.set_defaults(_handler=_preset_missing_subcommand)

    # ---- alive demo validate <stage> --partial <path> ----------------------
    validate = demo_subparsers.add_parser(
        "validate",
        help="Run stage-local coherence validation (validate.py facade).",
        description=(
            "Run validate.py:validate_stage on the given stage's outputs. "
            "Emits a unified ValidationResult JSON. Stage IDs: 0, 2, 3, 4. "
            "Stage 1 is UX-only. The squirrel uses the result to drive "
            "the one-retry-and-feedback contract documented in SKILL.md."
        ),
    )
    validate.add_argument(
        "stage",
        choices=["0", "2", "3", "4"],
        help="Stage ID to validate. Stage 1 (UX-only) has no validator.",
    )
    validate.add_argument(
        "--partial",
        required=True,
        help="Partial directory path (e.g. <base>/wld_<ulid>.partial/).",
    )
    validate.error = _json_error_handler(validate)  # type: ignore[assignment]
    validate.set_defaults(_handler=_validate_handler)

    parser.set_defaults(_handler=_demo_group_missing_subcommand)
    return parser


__all__ = (
    "register",
    "SCHEMA_METADATA",
)
