"""Deactivate entry point (fn-2-2zz.12).

Restores the world-root pointer to the value cached as
``previous_world_root`` in demo-state.json, then clears
``active_world`` and ``previous_world_root`` so a follow-up activate
starts clean.

Three states the input can be in:

* **No active demo** -- demo-state.json's ``active_world`` is None. We
  return a no-op envelope. The skill prose surfaces a friendly
  "no demo active" block.
* **Cold demo (no previous_world_root)** -- active_world is set but
  ``previous_world_root`` is None. This happens when a demo was
  activated against an empty / missing world-root pointer (no
  pre-existing live world). Restoring "nothing" would leave the system
  pointing at the demo world; instead we surface a friendly error and
  ask the user to activate a real world or run /alive:demo create.
* **Standard case** -- ``previous_world_root`` carries an absolute path;
  we atomically flip the pointer back to it.

The world-root flip is the single commit point. Failures before it
leave demo-state.json unchanged so a retry resumes from the same
state.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _world_root_io import write_world_root_file  # noqa: E402


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


class DeactivateError(RuntimeError):
    """Base error for deactivate flow."""


def run_deactivate() -> Dict[str, Any]:
    """Restore the previous world-root and clear demo-state pointers.

    Returns:
        ``{"status": "ok" | "no_demo_active" | "no_previous_world",
        "previous_world_root": ..., "deactivated": {...}}``.

        * ``no_demo_active`` -- nothing to do; ``previous_world_root``
          may still echo whatever demo-state had cached.
        * ``no_previous_world`` -- the demo was activated cold (no
          live world existed at activation time). Caller surfaces an
          error block asking the user to /alive:demo create or activate
          another world manually.
        * ``ok`` -- pointer restored; ``deactivated`` carries the
          previous-active world's identity for the squirrel's
          confirmation block.
    """
    state_mod = _load_state()
    deactivated: Optional[Dict[str, Any]] = None
    previous_for_envelope: Optional[str] = None
    restored_target: Optional[str] = None

    # State-layer exceptions (SchemaVersionMismatch, DemoStateError,
    # FlockTimeoutError) MUST propagate unchanged so the CLI can surface
    # the documented `schema_version_mismatch` / `demo_state_corrupt` /
    # `lock_timeout` envelopes. Per codex review round 2: wrapping them
    # in DeactivateError erases the error vocabulary.
    with state_mod.with_locked_state() as state:
        active = state.get("active_world")
        previous = state.get("previous_world_root")
        previous_for_envelope = previous

        if active is None:
            return {
                "status": "no_demo_active",
                "previous_world_root": previous,
            }

        if not previous or not isinstance(previous, str):
            # Cold demo: no cached previous_world_root. We surface
            # a structured envelope and ask the squirrel to drive
            # the user via AskUserQuestion (a future flow). Doing
            # NOTHING here is the safe default: the user can run
            # /alive:demo create or activate another world manually.
            return {
                "status": "no_previous_world",
                "active_world": dict(active),
                "previous_world_root": previous,
            }

        # Atomic commit: flip the pointer back. If it raises, demo-state
        # stays untouched (with_locked_state does not save on exception)
        # and the user can retry. ValueError from write_world_root_file
        # is wrapped because the CLI's documented vocabulary uses
        # DeactivateError -> deactivate_error for content-shape issues
        # on the previous-world value (corrupt cache).
        try:
            write_world_root_file(previous)
        except ValueError as exc:
            raise DeactivateError(
                f"cached previous_world_root is invalid: {previous!r}: "
                f"{exc}"
            ) from exc
        except OSError as exc:
            raise DeactivateError(
                f"could not write world-root pointer: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        deactivated = dict(active)
        restored_target = previous
        # Clear demo-state (the read-modify-write idiom rewrites
        # on clean exit).
        state["active_world"] = None
        state["previous_world_root"] = None

    return {
        "status": "ok",
        "deactivated": deactivated,
        "previous_world_root": previous_for_envelope,
        "restored_world_root": restored_target,
    }


__all__ = ("DeactivateError", "run_deactivate")
