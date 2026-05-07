"""Sandbox-testing preset path (fn-2-2zz.11).

Non-LLM, fully deterministic activation path for the ``/alive:demo`` skill.
Lean reimpl of the older ``sandbox-environment v0.1`` bundle: copy the
hand-authored ``preset/realistic-seeded/`` tree to a fresh
``<base>/wld_<ULID>/`` and run the Stage 5 transactional sequence
(steps 3..11) against it.

Differences from the custom (LLM-driven) path:

* **No Stages 0..4.** The preset is pre-validated at author time; there
  is no spine generation, no anchor-confirmation UX, no entity prose
  pass, no timeline materialisation, no insights synthesis.
* **Step 1 (pre-check) is preserved.** The activation pre-check runs
  even for the preset path; the preset is *for devs* but the live
  world the activation overwrites may not be.
* **Step 2 (rename) is replaced by a copy.** The preset tree lives at
  ``<plugin>/skills/demo/preset/realistic-seeded/``; ``shutil.copytree``
  delivers the world to ``<base>/wld_<ULID>/`` directly. (The custom
  path's atomic same-FS rename does not apply here because the preset
  source must remain in place under version control.)
* **Step 5 (completed.json) is skipped.** The preset ships a pre-baked
  ``completed.json`` per walnut so the 80/20 split synthesis is not
  needed. We do not re-synthesize on top of the preset's own values.
* **Step 4 (squirrel YAMLs) reads ``_world_meta.json``.** The custom
  path harvests sessions from the Stage 3 log timeline; the preset
  declares its sessions in a small JSON manifest at the preset root.

Steps 3, 7, 8, 10, 11 use the SAME helpers as the custom path
(``scaffold.step_3_preferences``, ``scaffold.step_7_project``,
``scaffold.step_8_generate_index``, ``scaffold.step_10_stage_demo_state``,
``scaffold.step_11_commit_world_root``); step 6 (install entities) is a
no-op for the preset (pre-baked tree); only the Step 4 / 5 / 9 bodies
are preset-specific. The shared helpers carry the locked atomic-write
+ flock semantics that the activation transaction requires.

Stdlib-only.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _load_demo_sibling(name: str):
    """Load ``<demo>/<name>.py`` under a namespaced sys.modules key.

    Mirrors the convention in ``stage5.py``; keeps the sibling import
    paths from colliding with any future top-level ``state`` / ``lib``.
    """
    full_name = f"alive_demo.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_DEMO_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(full_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _scaffold():
    """Return the Stage 5 scaffold module (shared step helpers)."""
    return _load_demo_sibling("scaffold")


def _state():
    """Return the demo-state module."""
    return _load_demo_sibling("state")


def _lib():
    """Return the demo lib module (ULID + format helpers)."""
    return _load_demo_sibling("lib")


from _common import atomic_write_text, iso_now, resolve_plugin_root  # noqa: E402
from _world_root_io import read_world_root_file  # noqa: E402


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PresetError(RuntimeError):
    """Base error for preset-path failures."""


class PresetNotFound(PresetError):
    """Raised when the preset source directory or its manifest is missing."""


# ---------------------------------------------------------------------------
# Paths + manifest
# ---------------------------------------------------------------------------

#: Default preset name. Today the preset library only ships
#: ``realistic-seeded`` (Nova Station). Future presets live as siblings
#: under ``preset/`` and are selected by name.
DEFAULT_PRESET_NAME = "realistic-seeded"

#: Allowed identifier shape for preset names (filesystem safety guard).
_PRESET_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Squirrel-id format -- 16 lowercase hex chars (matches stage3 contract).
_SQUIRREL_SID_RE = re.compile(r"^[0-9a-f]{16}$")

#: Base directory env override (shared with the custom path).
_DEMO_BASE_OVERRIDE = "ALIVE_DEMO_BASE_DIR"
_DEFAULT_BASE_RELHOME = ".alive-demos"


def _abspath(p) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(p)))


def _demo_base_dir() -> str:
    """Resolve the demo base dir; same rule as ``scaffold._demo_base_dir``."""
    override = os.environ.get(_DEMO_BASE_OVERRIDE)
    if override:
        return _abspath(override)
    return _abspath(os.path.expanduser("~/" + _DEFAULT_BASE_RELHOME))


def _preset_root(preset_name: str) -> str:
    """Resolve the hand-authored preset source directory.

    Lives at ``<plugin>/skills/demo/preset/<preset_name>/``. Validated
    by ``_validate_preset_name`` before joining; the stem is therefore
    guaranteed safe against path-escape (no separators, no leading dot).
    """
    return os.path.join(_DEMO_DIR, "preset", preset_name)


def _validate_preset_name(preset_name: str) -> None:
    if not isinstance(preset_name, str) or not preset_name:
        raise PresetError(
            f"preset_name must be a non-empty str; got {preset_name!r}"
        )
    if not _PRESET_NAME_RE.match(preset_name):
        raise PresetError(
            f"preset_name {preset_name!r} must match "
            f"^[a-z0-9]+(-[a-z0-9]+)*$ (filesystem safety guard)"
        )


def _read_world_meta(preset_root: str) -> Dict[str, Any]:
    """Read ``_world_meta.json`` from the preset root.

    The manifest declares the walnuts the preset projects, the bundles
    it carries, and the synthetic sessions to mint squirrel YAMLs for.
    Schema is documented in
    ``plugins/alive/skills/demo/preset/realistic-seeded/README.md``.
    """
    meta_path = os.path.join(preset_root, "_world_meta.json")
    if not os.path.isfile(meta_path):
        raise PresetNotFound(
            f"preset manifest missing at {meta_path}; the preset directory "
            f"is incomplete. Use the custom path (/alive:demo create -> "
            f"Custom) or restore the preset content."
        )
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise PresetError(
            f"preset manifest at {meta_path} is unreadable / malformed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PresetError(
            f"preset manifest at {meta_path} must be a JSON object"
        )
    # Fail-loud schema check before returning so callers can trust
    # `walnuts` / `sessions` are well-formed without re-validating.
    _validate_meta(data, meta_path=meta_path)
    return data


def _validate_meta(meta: Dict[str, Any], *, meta_path: str) -> None:
    """Fail-loud schema check for ``_world_meta.json``.

    A permissive parser silently dropped malformed entries on the
    floor and produced incomplete worlds while still flipping the
    pointer at step 11; the codex review flagged this as a regression
    risk. We now type-check every required key here so a broken
    preset fails BEFORE any filesystem mutation.
    """
    if meta.get("schema_version") != "0.1":
        raise PresetError(
            f"{meta_path}: schema_version must be '0.1' "
            f"(found {meta.get('schema_version')!r})"
        )
    walnuts = meta.get("walnuts")
    if not isinstance(walnuts, list) or not walnuts:
        raise PresetError(
            f"{meta_path}: 'walnuts' must be a non-empty list"
        )
    for i, w in enumerate(walnuts):
        if not isinstance(w, dict):
            raise PresetError(
                f"{meta_path}: walnuts[{i}] must be an object, got "
                f"{type(w).__name__}"
            )
        for key in ("slug", "domain_dir"):
            if not isinstance(w.get(key), str) or not w[key]:
                raise PresetError(
                    f"{meta_path}: walnuts[{i}].{key} must be a non-empty "
                    f"string (got {w.get(key)!r})"
                )
    sessions = meta.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise PresetError(
            f"{meta_path}: 'sessions' must be a non-empty list"
        )
    for i, s in enumerate(sessions):
        if not isinstance(s, dict):
            raise PresetError(
                f"{meta_path}: sessions[{i}] must be an object, got "
                f"{type(s).__name__}"
            )
        sid = s.get("sid")
        if not isinstance(sid, str) or not _SQUIRREL_SID_RE.match(sid):
            raise PresetError(
                f"{meta_path}: sessions[{i}].sid must be 16 lowercase "
                f"hex chars (got {sid!r})"
            )
        date = s.get("date")
        if not isinstance(date, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise PresetError(
                f"{meta_path}: sessions[{i}].date must be 'YYYY-MM-DD' "
                f"(got {date!r})"
            )


def _meta_to_spine(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Convert ``_world_meta.json`` into a Stage 5 ``spine``-shaped dict.

    The shared helpers (``scaffold.step_3_preferences``,
    ``step_7_project``) consume a spine with ``persona`` + ``walnut_roster``
    keys. We synthesize a minimal spine here so the helpers run unchanged
    against the preset world.

    Walnuts in the manifest correspond 1:1 to spine roster entries. Each
    entry carries the ``slug`` (basename) and ``domain_dir`` so
    ``scaffold._walnut_kernel_dirs`` can locate the kernel under
    ``<world>/<domain_dir>/<slug>/``. The manifest is validated upstream
    (``_validate_meta``) so this conversion can trust its inputs.
    """
    walnuts = meta["walnuts"]
    roster: List[Dict[str, Any]] = []
    for w in walnuts:
        slug = w["slug"]
        domain = w["domain_dir"]
        roster.append({
            "slug": slug,
            "name": slug,
            "type": w.get("type") or "venture",
            "domain_dir": domain,
            "summary": f"{slug} (preset)",
            "status": "active",
        })

    persona_first = meta.get("persona_first_name") or "sandbox"
    persona_full = meta.get("persona_full_name") or "sandbox squirrel"
    label = meta.get("label") or "preset-sandbox"

    return {
        "schema_version": "0.1",
        "persona": {
            "name": persona_full,
            "first_name": persona_first,
            "label": label,
            "summary": "Preset sandbox persona for skill testing.",
            "tone_hints": "concise",
        },
        "walnut_roster": roster,
        "people_roster": [],
        "bundle_distribution": [],
        "time_span": {"start": "2026-01-12", "end": "2026-04-22"},
        "session_cadence": {"pattern": "weekly", "sessions_per_week": 1},
        "anchor_moments": [],
    }


# ---------------------------------------------------------------------------
# Preset-specific step bodies
# ---------------------------------------------------------------------------


def _date_to_iso_z(date: str, *, hour: int = 9) -> str:
    """``YYYY-MM-DD`` -> ``YYYY-MM-DDTHH:00:00Z``. Matches scaffold helper."""
    try:
        dt = _dt.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return date
    dt = dt.replace(hour=hour, minute=0, second=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _step_4_squirrel_yamls_from_meta(
    world_path: str,
    *,
    sessions: List[Dict[str, Any]],
    squirrel_name: str,
) -> Dict[str, Any]:
    """Mint ``<world>/.alive/_squirrels/<sid>.yaml`` per declared session.

    The custom path's ``scaffold.step_4_squirrel_yamls`` walks the Stage
    3 world log to enumerate sessions; the preset path uses the
    pre-baked session list from ``_world_meta.json`` (no log walk
    required, the preset content was authored from the same list).

    Each session YAML carries ``saves: 1`` so the activation pre-check
    cannot fire on the preset world (matches the custom path's
    contract).
    """
    out_dir = os.path.join(world_path, ".alive", "_squirrels")
    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    walnut_assignments: List[Dict[str, Any]] = []

    # Manifest is pre-validated by ``_validate_meta`` so every entry
    # here is guaranteed well-formed. Silent skips at this layer
    # would re-introduce the regression risk codex flagged: a broken
    # preset that ships fewer squirrel YAMLs than declared while
    # still flipping the world-root pointer at step 11.
    for entry in sessions:
        sid = entry["sid"]
        date = entry["date"]
        walnut = entry.get("walnut")
        started_iso = _date_to_iso_z(date, hour=9)
        last_saved_iso = _date_to_iso_z(date, hour=18)
        walnut_value = walnut if isinstance(walnut, str) and walnut else "null"
        body = (
            f"session_id: {sid}\n"
            f"runtime_id: squirrel.core@1.0\n"
            f"squirrel_name: \"{squirrel_name}\"\n"
            f"engine: claude-demo\n"
            f"walnut: {walnut_value}\n"
            f"started: {started_iso}\n"
            f"ended: {last_saved_iso}\n"
            f"saves: 1\n"
            f"last_saved: {last_saved_iso}\n"
            f"transcript: null\n"
            f"cwd: {world_path}\n"
            f"rules_loaded: []\n"
            f"recovery_state: null\n"
            f"tags: []\n"
            f"stash: []\n"
            f"actions: []\n"
            f"working: []\n"
        )
        target = os.path.join(out_dir, f"{sid}.yaml")
        atomic_write_text(target, body)
        written.append(target)
        walnut_assignments.append({"sid": sid, "walnut": walnut})

    return {
        "step": 4,
        "status": "ok",
        "session_count": len(written),
        "yamls": written,
        "walnut_assignments": walnut_assignments,
    }


def _step_9_preset_build_log(
    world_path: str,
    *,
    ulid: str,
    label: str,
    activated_at: str,
    preset_name: str,
) -> Dict[str, Any]:
    """Write ``<world>/.alive/_demo-build-log.md`` for the preset path.

    Same frontmatter shape as the custom path so ``state.py``'s
    self-heal can recover ``active_world`` from the build log.
    Body notes that the world was scaffolded from the preset, not
    LLM-generated.
    """
    body = (
        "---\n"
        f"ulid: {ulid}\n"
        f"label: {label}\n"
        f"activated_at: {activated_at}\n"
        "---\n\n"
        "# Demo world build log (preset path)\n\n"
        f"World ulid {ulid} (label {label}) was scaffolded from preset "
        f"{preset_name} on {activated_at}.\n\n"
        "This world was NOT generated by the LLM pipeline (Stages 0..4); "
        "it is a deterministic copy of the hand-authored preset content "
        f"at plugins/alive/skills/demo/preset/{preset_name}/. The shared "
        "Stage 5 steps (3, 7, 8, 10, 11) were applied verbatim. The "
        "preset-specific steps were:\n\n"
        "- step 4 minted squirrel YAMLs from _world_meta.json (no log walk).\n"
        "- step 5 was skipped (preset ships pre-baked completed.json).\n"
        "- step 6 was skipped (preset copytree ships the canonical walnut "
        "tree wholesale; no Stage 0-4 outputs to install).\n"
        "- step 9 (this file) was preset-specific.\n\n"
        "Regenerate by editing the preset directory and re-running "
        "/alive:demo (preset path). See preset README.md for the regen "
        "workflow.\n"
    )
    target = os.path.join(world_path, ".alive", "_demo-build-log.md")
    atomic_write_text(target, body)
    return {
        "step": 9,
        "status": "ok",
        "path": target,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prepare_preset(
    preset_name: str = DEFAULT_PRESET_NAME,
    *,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate the preset directory + emit an activation plan.

    Returns a planning dict with the resolved target world path and
    the pre-check findings. No filesystem writes occur.

    Raises ``PresetNotFound`` when the preset directory or its
    ``_world_meta.json`` is missing; the calling skill surfaces a
    bordered-block error pointing the user to the custom path.

    The ULID is generated fresh on every call (the preset path is
    inherently re-runnable; idempotence is provided by the pre-baked
    preset content rather than by a sidecar marker).
    """
    _validate_preset_name(preset_name)
    preset_root = _preset_root(preset_name)
    if not os.path.isdir(preset_root):
        raise PresetNotFound(
            f"preset directory missing at {preset_root}; "
            f"use /alive:demo create then choose Custom (persona-driven) "
            f"or restore the preset content under "
            f"plugins/alive/skills/demo/preset/."
        )
    meta = _read_world_meta(preset_root)

    base = _abspath(base_dir) if base_dir else _demo_base_dir()
    ulid = _lib().new_world_ulid()
    target = os.path.join(base, ulid)
    label = meta.get("label") or preset_name

    current_root = read_world_root_file()
    findings = _lib().activation_pre_check(
        str(current_root) if current_root is not None else None
    )

    return {
        "preset_name": preset_name,
        "preset_root": preset_root,
        "target_world_path": target,
        "ulid": ulid,
        "label": label,
        "current_world_root": str(current_root) if current_root is not None else None,
        "findings": findings,
        "needs_confirmation": bool(findings),
        "walnuts_to_project": [
            w for w in (meta.get("walnuts") or []) if isinstance(w, dict)
        ],
    }


def _maybe_crash(fail_after_step: Optional[int], current: int) -> None:
    """Test seam (mirror of ``scaffold._maybe_crash``).

    Production callers pass ``fail_after_step=None``; the
    crash-consistency test suite passes an integer to abort AFTER the
    given step number.
    """
    if fail_after_step is None:
        return
    if current == fail_after_step:
        raise RuntimeError(
            f"preset crash-consistency test seam: aborting AFTER step "
            f"{current} (step {current + 1} did not run)"
        )


def run_preset(
    preset_name: str = DEFAULT_PRESET_NAME,
    *,
    confirm: bool = False,
    base_dir: Optional[str] = None,
    plugin_root: Optional[str] = None,
    fail_after_step: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute the preset activation transaction.

    Step sequence (ordering matches the custom path so a future audit
    that walks both paths sees the same logical step numbers):

      1. ``activation_pre_check`` (returns ``needs_confirmation`` if
         findings are non-empty and ``confirm`` is False).
      2. Copy preset tree to ``<base>/wld_<ULID>/``. Same role as the
         custom path's atomic rename: produces the final-shape directory.
      3. Write ``<world>/.alive/preferences.yaml`` (shared helper).
      4. Mint squirrel YAMLs from ``_world_meta.json`` (preset-specific).
      5. SKIP -- preset ships pre-baked completed.json per walnut.
      6. Shell out ``project.py --walnut`` per walnut (shared helper).
      7. Shell out ``generate-index.py <world>`` (shared helper).
      8. Write preset-specific ``_demo-build-log.md`` (preset-specific).
      9. Atomically update ``demo-state.json`` (shared helper).
      10. ``_world_root_io.write_world_root_file(<world>)`` -- THE
          single commit point (shared helper).
    """
    plan = prepare_preset(preset_name, base_dir=base_dir)
    target = plan["target_world_path"]
    ulid = plan["ulid"]
    label = plan["label"]
    preset_root = plan["preset_root"]
    current_world_root = plan["current_world_root"]

    meta = _read_world_meta(preset_root)
    spine = _meta_to_spine(meta)
    sessions = meta.get("sessions") or []
    squirrel_name = meta.get("persona_full_name") or "sandbox squirrel"

    activated_at = iso_now()
    scaffold = _scaffold()
    state_mod = _state()
    steps: List[Dict[str, Any]] = []

    # Step 1 -- pre-check.
    #
    # IMPORTANT (codex review round 2): the partial_generations row is
    # staged AFTER the pre-check confirmation gate. If we staged
    # before, a no-confirm run that returns ``needs_confirmation``
    # would write an ``in_progress`` row for a ULID that the follow-up
    # ``--confirm`` invocation will not reuse (the preset path mints a
    # fresh ULID on every prepare; unlike the custom path it has no
    # sidecar activation marker to pin identity across retries). Each
    # follow-up call would then orphan a phantom row in demo-state.
    # Deferring staging until AFTER confirmation removes that class of
    # leak: a ``needs_confirmation`` return path writes nothing.
    s1 = scaffold.step_1_pre_check(current_world_root=current_world_root)
    steps.append(s1)
    if s1.get("status") == "needs_confirmation" and not confirm:
        return {
            "status": "needs_confirmation",
            "steps": steps,
            "ulid": ulid,
            "label": label,
            "world_path": target,
            "preset_name": preset_name,
            "findings": s1.get("findings", []),
        }
    _maybe_crash(fail_after_step, 1)

    # Stage the partial_generations row now that we are committed to
    # running the transaction end-to-end (or until a downstream crash
    # boundary). The row converges to ``promoted`` at step 9.
    with state_mod.with_locked_state() as state:
        partials = state.get("partial_generations") or []
        existing = next(
            (p for p in partials if p.get("ulid") == ulid),
            None,
        )
        if existing is None:
            partials.append({
                "ulid": ulid,
                "label": label,
                "stage": "5_promote",
                "started_at": activated_at,
                "last_updated": activated_at,
                "status": "in_progress",
            })
        state["partial_generations"] = partials

    # Step 2 -- copy preset tree to the target world path. Analogous to
    # the custom path's atomic rename: produces the final-shape
    # directory directly. ``copytree`` rejects an existing target so
    # collisions on the same ULID surface loud.
    if os.path.exists(target):
        raise PresetError(
            f"preset target world path {target} already exists; refusing "
            f"to clobber. Choose a different ULID or remove the directory."
        )
    parent = os.path.dirname(target) or "/"
    os.makedirs(parent, exist_ok=True)
    shutil.copytree(preset_root, target, symlinks=False, dirs_exist_ok=False)
    # Drop the preset-internal ``_world_meta.json`` and ``README.md``
    # from the activated world tree so the world looks like a real
    # ALIVE world, not a preset container. Failure here is hard:
    # leaving these files in the activated world would silently
    # produce a tree that other ALIVE readers do not recognize. The
    # transaction has not yet committed (step 11 has not fired) so
    # raising leaves the world-root pointer untouched.
    for stem in ("_world_meta.json", "README.md"):
        leaked = os.path.join(target, stem)
        if os.path.isfile(leaked):
            try:
                os.unlink(leaked)
            except OSError as exc:
                raise PresetError(
                    f"step 2: failed to strip preset-internal "
                    f"{leaked} from activated world "
                    f"({type(exc).__name__}: {exc}); refusing to "
                    f"proceed with a preset-container in the world tree"
                ) from exc
    # The custom path also makes ``<world>/.alive/`` here implicitly
    # (Stage 5 writes preferences.yaml under it); for the preset we
    # create the dir up-front so steps 3/4/8 can write into it.
    os.makedirs(os.path.join(target, ".alive"), exist_ok=True)
    steps.append({
        "step": 2,
        "status": "ok",
        "from": preset_root,
        "to": target,
        "kind": "copytree",
    })
    _maybe_crash(fail_after_step, 2)

    # Step 3 -- preferences.yaml (shared helper).
    s3 = scaffold.step_3_preferences(target, spine=spine)
    steps.append(s3)
    _maybe_crash(fail_after_step, 3)

    # Step 4 -- preset-specific squirrel YAMLs from manifest.
    s4 = _step_4_squirrel_yamls_from_meta(
        target, sessions=sessions, squirrel_name=squirrel_name,
    )
    steps.append(s4)
    _maybe_crash(fail_after_step, 4)

    # Step 5 -- SKIPPED. Preset ships pre-baked completed.json.
    steps.append({
        "step": 5,
        "status": "skipped_preset",
        "reason": (
            "preset ships pre-baked completed.json per walnut; the "
            "80/20 synthesis path is reserved for the custom flow."
        ),
    })
    _maybe_crash(fail_after_step, 5)

    # Step 6 -- SKIPPED. step_6_install_entities translates Stage 0-4
    # orphan outputs from <world>/_stage_outputs/ into the canonical
    # walnut layout; the preset ships a pre-baked, fully-canonical tree
    # via copytree at step 2, so there is nothing to install. Emit the
    # skipped entry so the step ledger stays parallel to the custom
    # path and `len(steps) == 11` is preserved.
    steps.append({
        "step": 6,
        "status": "skipped_preset",
        "reason": (
            "preset copytree (step 2) ships the canonical walnut tree "
            "wholesale; there are no Stage 0-4 outputs at "
            "_stage_outputs/ to install."
        ),
    })
    _maybe_crash(fail_after_step, 6)

    # Step 7 -- project.py per walnut (shared helper).
    s7 = scaffold.step_7_project(target, spine=spine, plugin_root=plugin_root)
    steps.append(s7)
    _maybe_crash(fail_after_step, 7)

    # Step 8 -- generate-index.py (shared helper).
    s8 = scaffold.step_8_generate_index(target, plugin_root=plugin_root)
    steps.append(s8)
    _maybe_crash(fail_after_step, 8)

    # Step 9 -- preset-specific build log.
    s9 = _step_9_preset_build_log(
        target,
        ulid=ulid,
        label=label,
        activated_at=activated_at,
        preset_name=preset_name,
    )
    steps.append(s9)
    _maybe_crash(fail_after_step, 9)

    # Step 10 -- demo-state staging (shared helper).
    s10 = scaffold.step_10_stage_demo_state(
        ulid=ulid, label=label, world_path=target, activated_at=activated_at,
    )
    steps.append(s10)
    _maybe_crash(fail_after_step, 10)

    # Step 11 -- THE commit point (shared helper).
    s11 = scaffold.step_11_commit_world_root(target)
    steps.append(s11)
    _maybe_crash(fail_after_step, 11)

    return {
        "status": "ok",
        "steps": steps,
        "ulid": ulid,
        "label": label,
        "world_path": target,
        "preset_name": preset_name,
        "activated_at": activated_at,
    }


def verify_preset(world_path) -> Dict[str, Any]:
    """Post-activation verification of a preset-activated world.

    Checks (each contributes a finding on failure):

      * ``~/.config/alive/world-root`` reads back as ``world_path``.
      * ``demo-state.json[active_world][path]`` matches ``world_path``.
      * Every walnut directory has the four Read-Before-Speaking kernel
        files (``key.md``, ``now.json``, ``insights.md``, ``log.md``)
        present and readable. ``tasks.json`` is treated as soft (the
        live ALIVE convention is "absent == empty"); the file is read
        only if it exists and any I/O failure surfaces a finding.
      * ``now.json`` is accepted at either the canonical
        ``_kernel/now.json`` location or the v2 fallback
        ``_kernel/_generated/now.json`` (the resolver mirrors what
        ``project.py`` and ``state.py`` accept downstream).
      * ``<world>/.alive/_index.{yaml,json}`` exist.
      * ``<world>/.alive/_demo-build-log.md`` exists.

    Returns ``{"status": "ok" | "failed", "findings": [...], "world_path": <abs>}``.
    """
    world_path = _abspath(world_path)
    findings: List[Dict[str, Any]] = []

    state_mod = _state()
    state = state_mod.load_state()

    pointer = state_mod.read_world_root_file()
    pointer_str = str(pointer) if pointer is not None else None
    if pointer_str != world_path:
        findings.append({
            "issue": "world_root_mismatch",
            "evidence": (
                f"world-root pointer = {pointer_str!r}; "
                f"expected {world_path!r}"
            ),
        })

    active = state.get("active_world") or {}
    if active.get("path") != world_path:
        findings.append({
            "issue": "demo_state_active_world_mismatch",
            "evidence": (
                f"demo-state.json active_world.path = {active.get('path')!r}; "
                f"expected {world_path!r}"
            ),
        })

    for stem, label in (
        (".alive/_demo-build-log.md", "build_log"),
        (".alive/_index.yaml", "index_yaml"),
        (".alive/_index.json", "index_json"),
    ):
        target = os.path.join(world_path, stem)
        if not os.path.isfile(target):
            findings.append({
                "issue": f"{label}_missing",
                "evidence": f"missing {target}",
            })

    # Read-Before-Speaking contract: every walnut has all kernel files
    # present + readable. tasks.json may be absent on a fresh walnut
    # (the live ALIVE convention is "absent == empty"); we tolerate
    # that here too.
    domain_dirs = ("01_Archive", "02_Life", "04_Ventures", "05_Experiments")
    skip_dirs = frozenset({
        ".git", ".next", ".venv",
        "__pycache__", "build", "dist", "node_modules", "raw",
        "target", "venv",
    })
    # ``now.json`` is special: project.py writes it at
    # ``_kernel/now.json`` today, but the v2 fallback path
    # ``_kernel/_generated/now.json`` is also accepted by readers
    # downstream. Resolve to whichever exists so a future relocation
    # does not cause spurious verification failures.
    def _resolve_now_json(kernel_dir: str) -> Optional[str]:
        canonical = os.path.join(kernel_dir, "now.json")
        if os.path.isfile(canonical):
            return canonical
        fallback = os.path.join(kernel_dir, "_generated", "now.json")
        if os.path.isfile(fallback):
            return fallback
        return None

    required_static_files = ("key.md", "insights.md", "log.md")
    for domain in domain_dirs:
        dpath = os.path.join(world_path, domain)
        if not os.path.isdir(dpath):
            continue
        for walk_root, dirs, _files in os.walk(dpath):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in skip_dirs
            ]
            kernel = os.path.join(walk_root, "_kernel")
            key_path = os.path.join(kernel, "key.md")
            if not os.path.isfile(key_path):
                continue
            dirs[:] = []
            # Required kernel files (canonical location).
            for fname in required_static_files:
                fpath = os.path.join(kernel, fname)
                if not os.path.isfile(fpath):
                    findings.append({
                        "issue": "kernel_file_missing",
                        "evidence": (
                            f"walnut {walk_root}: missing {fpath}"
                        ),
                    })
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        f.read(1)
                except OSError as exc:
                    findings.append({
                        "issue": "kernel_file_unreadable",
                        "evidence": (
                            f"walnut {walk_root}: {fpath} unreadable "
                            f"({type(exc).__name__}: {exc})"
                        ),
                    })
            # now.json with v2 fallback resolution.
            now_path = _resolve_now_json(kernel)
            if now_path is None:
                findings.append({
                    "issue": "kernel_file_missing",
                    "evidence": (
                        f"walnut {walk_root}: missing now.json (checked "
                        f"_kernel/now.json and _kernel/_generated/now.json)"
                    ),
                })
            else:
                try:
                    with open(now_path, "r", encoding="utf-8") as f:
                        f.read(1)
                except OSError as exc:
                    findings.append({
                        "issue": "kernel_file_unreadable",
                        "evidence": (
                            f"walnut {walk_root}: {now_path} unreadable "
                            f"({type(exc).__name__}: {exc})"
                        ),
                    })
            # tasks.json soft check: absent is OK (live convention is
            # "absent == empty"); present-but-unreadable is a finding.
            tasks_path = os.path.join(kernel, "tasks.json")
            if os.path.isfile(tasks_path):
                try:
                    with open(tasks_path, "r", encoding="utf-8") as f:
                        f.read(1)
                except OSError as exc:
                    findings.append({
                        "issue": "kernel_file_unreadable",
                        "evidence": (
                            f"walnut {walk_root}: {tasks_path} unreadable "
                            f"({type(exc).__name__}: {exc})"
                        ),
                    })

    return {
        "status": "ok" if not findings else "failed",
        "findings": findings,
        "world_path": world_path,
    }


__all__ = (
    "DEFAULT_PRESET_NAME",
    "PresetError",
    "PresetNotFound",
    "prepare_preset",
    "run_preset",
    "verify_preset",
)
