"""Activate-by-ref entry point (fn-2-2zz.12).

Re-activates a previously-promoted demo world by re-running the tail
of the Stage 5 transaction (steps 9 / 10 / 11) against an existing
``<base>/wld_<ULID>/`` directory.

Why steps 9-11 only? The full 11-step transaction promotes a partial
directory (steps 1-8 build the world out of stage outputs); we already
have a fully-baked world on disk here. preferences.yaml, _squirrels/,
completed.json, projections, and indexes are already in place from the
original activation. We need to:

* refresh the build log with a "re-activated at <ts>" entry (step 9),
* stage demo-state metadata so ``previous_world_root`` is captured for
  the eventual deactivate call (step 10),
* atomically flip ``~/.config/alive/world-root`` to this world (step 11).

The activation pre-check still fires before steps 9/10/11 -- a live
world with uncommitted work needs the same explicit confirmation
whether we are activating a fresh or pre-existing world.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import os
import sys
from typing import Any, Dict, Optional

# Mirror the path-bootstrap pattern used elsewhere in the demo skill so
# importers don't have to do it themselves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _common import atomic_write_text, iso_now  # noqa: E402
from _world_root_io import read_world_root_file  # noqa: E402


def _load_sibling(module_name: str, filename: str):
    """Load a sibling .py under a namespaced sys.modules key."""
    full_name = f"alive_demo.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_DEMO_DIR, filename)
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_lib():
    return _load_sibling("lib", "lib.py")


def _load_state():
    return _load_sibling("state", "state.py")


def _load_scaffold():
    return _load_sibling("scaffold", "scaffold.py")


class ActivateExistingError(RuntimeError):
    """Base error for the activate-existing flow."""


def prepare_activate(record) -> Dict[str, Any]:
    """Dry-run plan for re-activating ``record``.

    Returns a planning dict with the resolved target world path and
    activation pre-check findings; performs no writes. The caller
    surfaces a confirmation block if ``needs_confirmation`` is true.

    Pointer-read errors (corrupt ``~/.config/alive/world-root``,
    ``ValueError`` from ``_world_root_io.read_world_root_file``) are
    wrapped in ``ActivateExistingError`` so the CLI handler always
    surfaces a structured JSON envelope.
    """
    if record is None:
        raise ActivateExistingError("record is None; cannot activate")
    if not isinstance(record.path, str) or not os.path.isdir(record.path):
        raise ActivateExistingError(
            f"record.path {record.path!r} is not a directory"
        )

    lib = _load_lib()
    try:
        current_root = read_world_root_file()
    except ValueError as exc:
        raise ActivateExistingError(
            f"world-root pointer is corrupt: {exc}. "
            f"Inspect ~/.config/alive/world-root or run /alive:demo reset."
        ) from exc
    except OSError as exc:
        raise ActivateExistingError(
            f"could not read world-root pointer: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    current_str = str(current_root) if current_root is not None else None
    findings = lib.activation_pre_check(current_str)

    return {
        "ulid": record.ulid,
        "label": record.label,
        "world_path": record.path,
        "current_world_root": current_str,
        "findings": findings,
        "needs_confirmation": bool(findings),
    }


_FRONTMATTER_ACTIVATED_AT_RE = None  # populated lazily


def _append_reactivation_entry(world_path: str, *, activated_at: str) -> None:
    """Refresh the build-log frontmatter `activated_at` and append a re-activation bullet.

    The build log is markdown with a YAML frontmatter. ``state.py``'s
    self-heal reads ``activated_at`` straight off the frontmatter, so
    a re-activate that only appended a bullet would let a reset/self-heal
    report a stale activation timestamp. We:

      1. Rewrite the frontmatter ``activated_at`` line to the new
         timestamp (preserving every other key + value verbatim).
      2. Append a ``## Re-activations`` bullet at end-of-file so the
         human history of re-activations stays readable.

    Both writes go via a single ``atomic_write_text`` against the
    final body so a crash mid-rewrite cannot leave a half-baked file.
    """
    import re as _re  # noqa: PLC0415

    target = os.path.join(world_path, ".alive", "_demo-build-log.md")
    try:
        with open(target, "r", encoding="utf-8") as f:
            existing = f.read()
    except OSError as exc:
        raise ActivateExistingError(
            f"_demo-build-log.md unreadable at {target}: {exc}"
        ) from exc

    # Update frontmatter `activated_at:` line, keeping the rest intact.
    # Frontmatter is the leading `---\n...\n---\n` block; we rewrite
    # only inside it. If no frontmatter is present we still write the
    # bullet so the human-visible audit trail isn't lost.
    fm_re = _re.compile(r"\A(---\s*\n)(.*?)(\n---\s*\n)", _re.DOTALL)
    m = fm_re.match(existing)
    if m is not None:
        head, body, tail = m.group(1), m.group(2), m.group(3)
        new_lines = []
        replaced = False
        for line in body.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("activated_at:"):
                indent = line[: len(line) - len(stripped)]
                new_lines.append(f"{indent}activated_at: {activated_at}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            # No `activated_at:` line was present; insert one at the
            # start of the frontmatter so self-heal sees it.
            new_lines.insert(0, f"activated_at: {activated_at}")
        new_fm = head + "\n".join(new_lines) + tail
        rest = existing[m.end():]
        rebuilt = new_fm + rest
    else:
        rebuilt = existing

    note_heading = "## Re-activations\n"
    bullet = f"- {activated_at}\n"
    if note_heading in rebuilt:
        new_text = rebuilt.rstrip("\n") + "\n" + bullet
    else:
        new_text = rebuilt.rstrip("\n") + "\n\n" + note_heading + bullet
    atomic_write_text(target, new_text)


def run_activate(record, *, confirm: bool = False) -> Dict[str, Any]:
    """Re-activate a previously-promoted demo world.

    Steps run:
      1. Pre-check (fail-fast on findings unless ``confirm=True``).
      2. Append a re-activation note to ``_demo-build-log.md`` (Stage 5
         step 9 analogue; the existing frontmatter stays intact).
      3. Stage demo-state metadata via Stage 5 step 10 helper -- caches
         the previous world-root pointer into ``previous_world_root``,
         points ``active_world`` at this record.
      4. Atomically flip the world-root pointer via Stage 5 step 11
         helper. THE single commit point.

    Crash-consistency: the same invariants that govern Stage 5 apply
    here. Failures before step 4 leave the world-root pointer
    unchanged; the next ``state.load_state`` call's self-heal converges
    demo-state.json back to the pointer.
    """
    plan = prepare_activate(record)

    # Short-circuit: re-activating an already-active demo. Per codex
    # review round 3: if the live world-root pointer ALREADY names this
    # record's path, running step 10 would overwrite
    # `demo-state.json[previous_world_root]` with the demo's own path,
    # destroying the cached path back to the original live world. A
    # subsequent `deactivate` would then re-flip the pointer to the
    # same demo (no-op) and clear the cache, leaving the user with no
    # way to restore the real previous world.
    #
    # Treat this as a no-op success: emit `already_active` so the
    # squirrel can render a friendly "already activated" message, and
    # leave demo-state untouched.
    if (
        plan["current_world_root"] is not None
        and os.path.normpath(os.path.abspath(plan["current_world_root"]))
        == os.path.normpath(os.path.abspath(record.path))
    ):
        return {
            "status": "already_active",
            "ulid": record.ulid,
            "label": record.label,
            "world_path": record.path,
            "current_world_root": plan["current_world_root"],
        }

    if plan["needs_confirmation"] and not confirm:
        return {
            "status": "needs_confirmation",
            "ulid": record.ulid,
            "label": record.label,
            "world_path": record.path,
            "findings": plan["findings"],
            "current_world_root": plan["current_world_root"],
        }

    scaffold = _load_scaffold()
    activated_at = iso_now()

    # Single-commit-point ordering. Per codex review round 2:
    # the build log rewrite is USER-VISIBLE METADATA (state.py self-heal
    # and list_demos read `activated_at` straight off it). Rewriting it
    # before step 10/11 can leave a stale-success appearance if step 10
    # fails (SchemaVersionMismatch, lock contention, etc.).
    #
    # Order:
    #   1. Step 10 (stage demo-state metadata; caches previous_world_root).
    #      State-layer exceptions (SchemaVersionMismatch, FlockTimeoutError,
    #      DemoStateError) propagate UNCHANGED so the CLI surfaces the
    #      correct error code.
    #   2. Step 11 (atomic world-root flip; THE single commit point).
    #   3. Build-log rewrite (Stage 5 step 9 analogue). Runs only AFTER
    #      step 11 succeeds. A failure here is post-commit and is
    #      surfaced as a partial-success envelope; the world is
    #      genuinely activated at the pointer level even if the audit
    #      trail update lags.
    state_layer_excs = _state_layer_exceptions()
    pass_through_excs = (ActivateExistingError,) + state_layer_excs

    # Snapshot demo-state.json BEFORE step 10 so a step-11 failure can
    # roll the staged metadata back to its pre-activation values. Per
    # codex review round 4: without rollback, a step-11 crash leaves
    # `previous_world_root` pointing at the wrong cache target -- the
    # original live world (R) is overwritten with the pre-flip pointer
    # value (A), so a subsequent deactivate restores to A instead of R
    # and clears the cache, permanently losing the path back to R.
    state_mod = _load_state()
    try:
        with state_mod.with_locked_state() as _snapshot_state:
            pre_step9_active = _snapshot_state.get("active_world")
            pre_step9_previous = _snapshot_state.get("previous_world_root")
    except pass_through_excs:
        raise
    except Exception as exc:
        raise ActivateExistingError(
            f"could not snapshot demo-state pre-activation: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        s9 = scaffold.step_10_stage_demo_state(
            ulid=record.ulid,
            label=record.label,
            world_path=record.path,
            activated_at=activated_at,
        )
    except pass_through_excs:
        # State-layer + ActivateExistingError pass through unchanged
        # so the CLI can surface lock_timeout / schema_version_mismatch /
        # demo_state_corrupt with their documented codes.
        raise
    except Exception as exc:
        raise ActivateExistingError(
            f"step 10 (demo-state metadata) failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        s10 = scaffold.step_11_commit_world_root(record.path)
    except Exception as exc:
        # Step 11 failed: the world-root pointer is unchanged, but
        # step 10 has already mutated demo-state.json. Roll back so
        # `previous_world_root` is not corrupted with the just-flipped
        # pointer value (which would let a subsequent deactivate
        # restore to the wrong target).
        #
        # CAS rollback (codex review round 5): a concurrent
        # /alive:demo session could legitimately update demo-state
        # between this activation's snapshot and step 10. An
        # unconditional rollback would clobber that newer state with
        # stale values. Instead, we re-acquire the lock and only
        # restore IF the current state still matches what step 10
        # staged for THIS activation:
        #
        #   active_world.ulid == this record's ulid
        #   active_world.path == this record's path
        #   previous_world_root == the value step 10 cached
        #
        # When the match holds, we are the most recent writer and
        # rolling back is safe. When it does not, another writer has
        # superseded us; we leave the state alone and surface a
        # conflict-style hint so the user knows the failed activation
        # did NOT overwrite recent demo-state changes.
        rollback_error: Optional[str] = None
        rollback_skipped_due_to_concurrent_mutation = False
        try:
            # We hand-roll the locked read because `with_locked_state`
            # runs `self_heal` on entry, which would rewrite
            # `active_world` to whatever the pointer names (A) before
            # our CAS check sees the raw step-10 mutation (B). The CAS
            # check needs the RAW post-step-10 state to detect
            # concurrent writers correctly.
            from _common import flock_file as _flock_file  # noqa: PLC0415, F401
            with state_mod.flock_file(state_mod.lock_path()):
                raw = state_mod._read_state_file(state_mod.state_path())
                cur_active = raw.get("active_world") or {}
                cur_previous = raw.get("previous_world_root")
                staged_previous = s9.get("previous_world_root")
                staged_match = (
                    isinstance(cur_active, dict)
                    and cur_active.get("ulid") == record.ulid
                    and cur_active.get("path") == record.path
                    and cur_previous == staged_previous
                )
                if staged_match:
                    raw["active_world"] = pre_step9_active
                    raw["previous_world_root"] = pre_step9_previous
                    # partial_generations is left as step 10 wrote it.
                    # The 'promoted' marker on the failing entry is
                    # still useful provenance for cleanup; the schema
                    # accepts a promoted entry without an active_world
                    # reference.
                    state_mod.save_state(raw, path=state_mod.state_path())
                else:
                    # Another writer has superseded our staging; do
                    # not touch demo-state. Caller is informed via
                    # the error message hint.
                    rollback_skipped_due_to_concurrent_mutation = True
        except Exception as rb_exc:
            rollback_error = (
                f"{type(rb_exc).__name__}: {rb_exc}"
            )

        msg = (
            f"step 11 (world-root commit) failed: "
            f"{type(exc).__name__}: {exc}"
        )
        if rollback_skipped_due_to_concurrent_mutation:
            msg += (
                "; demo-state rollback skipped (a concurrent /alive:demo "
                "session has already superseded the staged metadata)"
            )
        if rollback_error is not None:
            msg += f"; demo-state rollback also failed: {rollback_error}"
        raise ActivateExistingError(msg) from exc

    # Step 9 analogue: refresh the build log AFTER the commit succeeds.
    # A failure here is a post-commit warning, not a hard failure: the
    # demo world IS activated at the pointer level. We capture the
    # build-log error as `build_log_warning` in the result envelope so
    # the squirrel can surface it without rolling back a successful
    # activation.
    build_log_warning: Optional[str] = None
    try:
        _append_reactivation_entry(record.path, activated_at=activated_at)
    except ActivateExistingError as exc:
        build_log_warning = str(exc)
    except Exception as exc:
        build_log_warning = (
            f"build-log refresh failed: {type(exc).__name__}: {exc}"
        )

    return {
        "status": "ok",
        "ulid": record.ulid,
        "label": record.label,
        "world_path": record.path,
        "activated_at": activated_at,
        "previous_world_root": s9.get("previous_world_root"),
        "step9": s9,
        "step10": s10,
        "build_log_warning": build_log_warning,
    }


def _state_layer_exceptions():
    """Return a tuple of state-layer exception types that should pass through.

    These are the exceptions the CLI handler catches with their own
    structured envelope codes (lock_timeout, schema_version_mismatch,
    demo_state_corrupt). Wrapping them in ActivateExistingError would
    erase the documented error vocabulary.
    """
    state_mod = _load_state()
    # FlockTimeoutError lives on _common; pull it via state_mod's namespace
    # if exported, else import directly.
    import sys as _sys  # noqa: PLC0415
    common = _sys.modules.get("_common")
    if common is None:
        import _common as common  # noqa: F401, PLC0415
        common = _sys.modules["_common"]
    return (
        getattr(state_mod, "SchemaVersionMismatch", Exception),
        getattr(state_mod, "DemoStateError", Exception),
        getattr(common, "FlockTimeoutError", Exception),
    )


__all__ = (
    "ActivateExistingError",
    "prepare_activate",
    "run_activate",
)
