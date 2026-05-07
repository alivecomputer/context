"""Delete entry point (fn-2-2zz.12).

Removes a previously-promoted demo world from disk. Refuses on the
currently-active world (the user must deactivate first); requires
explicit ``confirm=True`` on every other call so an accidental
``alive demo delete <ref>`` cannot silently destroy data.

Updates demo-state.json to mark any matching ``partial_generations``
entry as ``failed`` (re-purposing the existing enum value -- the
schema's status vocabulary is ``in_progress | promoted | failed``,
and ``failed`` is the closest match for "no longer on disk"). The
ULID stays in the partial-generations history so a future audit can
see what was deleted.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _common import iso_now  # noqa: E402
from _world_root_io import read_world_root_file  # noqa: E402


def _load_state():
    full_name = "alive_demo.state"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_DEMO_DIR, "state.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


class DeleteError(RuntimeError):
    """Base error for delete flow."""


class DeleteRefusedActive(DeleteError):
    """Raised when the caller asks to delete the currently-active world."""


class PointerReadError(DeleteError):
    """Pointer file existed but could not be parsed safely.

    For an irreversible action, we MUST refuse rather than guess. The
    delete handler surfaces a hint asking the user to inspect /
    repair the pointer (or run ``/alive:demo reset``).
    """


def _is_record_active(record) -> bool:
    """Return True iff ``record`` matches the live world-root pointer.

    Raises ``PointerReadError`` when the pointer file is corrupt /
    unreadable -- a destructive operation cannot proceed under that
    ambiguity (per codex review).
    """
    if record is None:
        return False
    try:
        live = read_world_root_file()
    except ValueError as exc:
        raise PointerReadError(
            f"world-root pointer is corrupt: {exc}. "
            f"Refusing to delete under indeterminate active-world state. "
            f"Inspect ~/.config/alive/world-root or run /alive:demo reset."
        ) from exc
    except OSError as exc:
        raise PointerReadError(
            f"could not read world-root pointer: {type(exc).__name__}: {exc}. "
            f"Refusing to delete under indeterminate active-world state."
        ) from exc
    if live is None:
        return False
    return os.path.normpath(os.path.abspath(str(live))) == os.path.normpath(
        os.path.abspath(record.path)
    )


def prepare_delete(record) -> Dict[str, Any]:
    """Dry-run: emit a summary block + active-world refusal check.

    Returns ``{"status": "needs_confirmation" | "refused_active",
    "summary": {...}, "active": bool}``. Performs no writes.

    Raises ``PointerReadError`` (DeleteError subclass) when the
    world-root pointer is corrupt / unreadable so the CLI handler can
    surface a "fix the pointer first" envelope rather than guessing.
    """
    if record is None:
        raise DeleteError("record is None; cannot delete")
    is_active = _is_record_active(record)  # may raise PointerReadError
    if is_active:
        return {
            "status": "refused_active",
            "summary": {
                "ulid": record.ulid,
                "label": record.label,
                "path": record.path,
                "disk_size_bytes": record.disk_size_bytes,
            },
            "active": True,
            "hint": (
                "Refusing to delete the currently-active demo world. "
                "Run /alive:demo deactivate first, then re-run delete."
            ),
        }
    return {
        "status": "needs_confirmation",
        "summary": {
            "ulid": record.ulid,
            "label": record.label,
            "path": record.path,
            "created_at": record.created_at,
            "disk_size_bytes": record.disk_size_bytes,
            "persona_name": record.persona_name,
        },
        "active": False,
        "hint": (
            "Deletion is irreversible. Re-run `alive demo delete <ref> "
            "--confirm` to proceed."
        ),
    }


def run_delete(record, *, confirm: bool = False) -> Dict[str, Any]:
    """Delete ``record`` from disk. Refuses if active or unconfirmed.

    Steps:
      1. Refuse if the record matches the live world-root pointer.
      2. Without ``confirm=True``, return ``needs_confirmation`` so the
         skill can render the irreversibility surface + AskUserQuestion.
      3. ``shutil.rmtree(record.path)``.
      4. Update demo-state.json: any ``partial_generations`` entry
         whose ULID matches gets ``status="failed"`` and a fresh
         ``last_updated`` so audit trails stay coherent.

    Returns:
        On ``ok``: ``{"status": "ok", "deleted": {...}}``.
        On ``refused_active``: ``{"status": "refused_active", ...}``.
        On ``needs_confirmation``: ``{"status": "needs_confirmation", ...}``.
    """
    if record is None:
        raise DeleteError("record is None; cannot delete")

    plan = prepare_delete(record)
    if plan["status"] == "refused_active":
        return plan
    if not confirm:
        return plan

    # Validate-state-then-destroy ordering. Per codex review round 2:
    # rmtree before validating demo-state means a SchemaVersionMismatch
    # / FlockTimeoutError discovered post-rmtree leaves the world gone
    # AND leaves the user with a confusing `delete_error` envelope.
    # The fix: take the demo-state lock + run all validation FIRST.
    # Only then do we shutil.rmtree, and rewrite demo-state atomically
    # under the same lock.
    #
    # State-layer exceptions (SchemaVersionMismatch, FlockTimeoutError,
    # DemoStateError) propagate UNCHANGED so the CLI surfaces the
    # documented error codes.
    state_mod = _load_state()
    now = iso_now()

    # The destruction itself runs INSIDE the locked-state context so
    # a state error (schema mismatch, contention) blocks rmtree.
    with state_mod.with_locked_state() as state:
        # Re-check the active-world predicate inside the lock. A racing
        # session could have activated this world between the prepare
        # and run calls; refusing under the lock is the safe behaviour.
        active = state.get("active_world")
        if isinstance(active, dict) and active.get("path"):
            if os.path.normpath(os.path.abspath(active["path"])) == os.path.normpath(
                os.path.abspath(record.path)
            ):
                # Active inside the lock: refuse. The world is intact.
                return {
                    "status": "refused_active",
                    "summary": {
                        "ulid": record.ulid,
                        "label": record.label,
                        "path": record.path,
                        "disk_size_bytes": record.disk_size_bytes,
                    },
                    "active": True,
                    "hint": (
                        "Refusing to delete the currently-active demo world. "
                        "Run /alive:demo deactivate first, then re-run delete."
                    ),
                }

        # Now safe to destroy.
        if not os.path.isdir(record.path):
            already_gone = True
        else:
            try:
                shutil.rmtree(record.path)
            except OSError as exc:
                raise DeleteError(
                    f"shutil.rmtree({record.path!r}) failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            already_gone = False

        # Update demo-state inside the same lock so destruction +
        # audit-trail update commit together.
        partials = list(state.get("partial_generations", []))
        for entry in partials:
            if entry.get("ulid") == record.ulid:
                entry["status"] = "failed"
                entry["last_updated"] = now
        state["partial_generations"] = partials

    return {
        "status": "ok",
        "deleted": {
            "ulid": record.ulid,
            "label": record.label,
            "path": record.path,
            "already_gone": already_gone,
        },
    }


__all__ = (
    "DeleteError",
    "DeleteRefusedActive",
    "prepare_delete",
    "run_delete",
)
