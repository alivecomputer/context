"""Stage 5 deterministic activation transaction (fn-2-2zz.9).

The 11-step sequence that turns a fully-baked Stage 0-4 partial directory
into a live, projected ALIVE world. Steps 1-10 are reversible / replayable;
step 11 is THE single commit point (the atomic write of
``~/.config/alive/world-root`` via ``_world_root_io.write_world_root_file``).

The activation transaction MUST satisfy these crash-consistency invariants:

  * Failure at any step 1-10 leaves the canonical world-root pointer
    UNCHANGED. The previous live world remains active.
  * After step 10 succeeds, ``demo-state.json[active_world]`` names the new
    world but the pointer still names the previous one. ``state.py``'s
    self-heal detects the mismatch on the next load and re-syncs
    demo-state to the pointer (the pointer is the source of truth).
  * Step 11 is the atomic commit. After it succeeds, both the pointer and
    ``demo-state.json`` reflect the new world.

The 11 steps:

  1. ``activation_pre_check`` -- if findings non-empty, the worker returns
     ``status: needs_confirmation`` so the calling skill can surface the
     bordered-block warning + ``AskUserQuestion``. On reinvocation with
     ``confirm=True``, proceed.
  2. ``os.rename(<partial>, <base>/wld_<ULID>/)`` -- atomic on the same
     filesystem.
  3. Generate ``<world>/.alive/preferences.yaml`` (named squirrel +
     full-default action_logging + discovery_hints).
  4. Generate ``<world>/.alive/_squirrels/*.yaml`` per session referenced
     in the world log; full ``templates/squirrel/entry.yaml`` schema with
     ``saves: 1`` and a backdated ``last_saved``.
  5. Synthesize ``<walnut>/_kernel/completed.json`` per walnut: 80 % of
     the walnut's tasks pre-marked done with backdated ``completed`` dates
     (direct file write via ``_common.atomic_write_json`` -- NOT via
     ``tasks.py``).
  6. ``step_6_install_entities`` -- move Stage 0-4 outputs from
     ``<world>/_stage_outputs/`` into the canonical walnut layout: per-walnut
     ``_kernel/{key,log,insights}.md``, per-person ``_kernel/...`` (key/log/
     insights + bootstrap empty tasks/completed), per-bundle
     ``<walnut>/<bundle>/{context.manifest.yaml,tasks.json}``, and world-level
     ``.alive/{log,insights}.md``. ``os.replace`` per-file + final ``rmtree``
     of the entities dir; idempotent re-run via SHA-256 equality checks.
  7. For each walnut, shell out
     ``python3 plugins/alive/scripts/project.py --walnut <abs-path>`` to
     generate ``_kernel/now.json``.
  8. Shell out
     ``python3 plugins/alive/scripts/generate-index.py <world-root>`` to
     generate ``.alive/_index.{yaml,json}``.
  9. Write ``<world>/.alive/_demo-build-log.md`` (build provenance --
     stage timestamps, prompt versions, ulid + label / activated_at
     frontmatter for ``state.py``'s self-heal).
  10. Atomically update ``~/.config/alive/demo-state.json`` (flock +
     atomic-write): cache previous world-root pointer into
     ``previous_world_root``, mark the partial as ``promoted``, populate
     ``active_world``. **Pre-commit metadata staging.**
  11. ``_world_root_io.write_world_root_file(<world>)`` -- THE single
     commit point.

Stdlib-only. No yaml / jsonschema. The squirrel-entry YAMLs are emitted
by string templating against the literal ``templates/squirrel/entry.yaml``
shape (every key documented in that file is present in the output).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors stage4.py / state.py)
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_HERE, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _common import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    flock_file,
    iso_now,
    resolve_plugin_root,
)
from _world_root_io import (  # noqa: E402
    read_world_root_file,
    write_world_root_file,
)


def _load_sibling(module_name: str, filename: str):
    """Load a sibling .py under a namespaced sys.modules key.

    Mirrors the convention in cli_register / state.py.
    """
    full_name = f"alive_demo.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_HERE, filename)
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_demo_lib():
    return _load_sibling("lib", "lib.py")


def _load_demo_state():
    return _load_sibling("state", "state.py")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version stamped into stage5_done.json (the post-step-10 marker
#: written into the world's `.alive/`). Bump on breaking changes.
SCHEMA_VERSION = "0.1"

#: Default base directory for promoted demo worlds. Honors
#: ``$ALIVE_DEMO_BASE_DIR`` for partials + worlds (per epic locked
#: decisions); the world-root pointer + demo-state.json stay canonical
#: at ``~/.config/alive/``.
_DEFAULT_BASE_RELHOME = ".alive-demos"

#: Fraction of generated tasks pre-marked done in completed.json. Per
#: epic acceptance criteria: "80 % of generated tasks pre-marked done".
COMPLETED_TASK_FRACTION = 0.80

#: Squirrel-id format documented in stage3 (``[0-9a-f]{16}``).
_SQUIRREL_SID_RE = re.compile(r"^[0-9a-f]{16}$")

#: Re-export the world log entry-header pattern from stage3 so we can
#: walk the timeline to mint squirrel YAMLs without depending on stage3.
_ENTRY_HEADER_RE = re.compile(
    r"^##\s+(?P<date>\S+)\s+--\s+squirrel:(?P<sid>[0-9a-f]{16})\s*$",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class Stage5Error(RuntimeError):
    """Base error for Stage 5 activation failures."""


class Stage5NotReady(Stage5Error):
    """Raised when the partial dir is missing required stage{0..4}_done markers."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _abspath(p) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(p)))


def _demo_base_dir() -> str:
    """Resolve `<base>` for promoted demo worlds.

    Honors ``$ALIVE_DEMO_BASE_DIR`` (single env override per epic spec);
    otherwise defaults to ``~/.alive-demos/``. The directory is created
    lazily on first write.
    """
    override = os.environ.get("ALIVE_DEMO_BASE_DIR")
    if override:
        return _abspath(override)
    return _abspath(os.path.expanduser("~/" + _DEFAULT_BASE_RELHOME))


def _stage_outputs_dir(partial_dir: str) -> str:
    return os.path.join(partial_dir, "_stage_outputs")


def _stage_done_path(partial_dir: str, n: int) -> str:
    return os.path.join(_stage_outputs_dir(partial_dir), f"stage{n}_done.json")


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_json(path: str, label: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise Stage5NotReady(f"{label} not found at {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage5Error(
            f"{label} at {path} is unreadable / not JSON: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Predicates / parsing
# ---------------------------------------------------------------------------

def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block. Returns body only."""
    m = re.match(r"\A---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    if m:
        return text[m.end():]
    return text


def _date_only(date_iso: str) -> str:
    """Take the date-prefix of an entry header. Accepts ``YYYY-MM-DD`` or
    ``YYYY-MM-DDTHH:MM:SS`` (matches stage3 ``_ENTRY_HEADER_RE`` group).
    """
    return date_iso.split("T", 1)[0]


def _parse_world_log_sessions(world_log_path: str) -> List[Dict[str, str]]:
    """Walk the Stage 3 world log; emit one descriptor per ``## <date> -- squirrel:<sid>``.

    Each descriptor::

        {"date": "YYYY-MM-DD", "sid": "<16-hex>"}

    Returned in the order they appear in the log. De-duplicated on
    ``sid`` so a session that surfaces in multiple per-walnut logs
    doesn't yield two squirrel YAMLs.
    """
    if not os.path.isfile(world_log_path):
        return []
    body = _strip_frontmatter(_read_text(world_log_path))
    seen: set = set()
    out: List[Dict[str, str]] = []
    for match in _ENTRY_HEADER_RE.finditer(body):
        sid = match.group("sid")
        if sid in seen:
            continue
        seen.add(sid)
        out.append({
            "date": _date_only(match.group("date")),
            "sid": sid,
        })
    return out


def _build_session_walnut_map(walnut_logs_dir: str) -> Dict[str, str]:
    """Walk ``<world>/_stage_outputs/walnut-logs/<slug>.md`` to map sid -> slug.

    Each per-walnut log has entries ``## YYYY-MM-DD -- squirrel:<sid>``.
    The session-to-walnut mapping lets Stage 5 step 4 emit squirrel
    YAMLs whose ``walnut:`` field matches the actual walnut basename
    (so ``project.py``'s exact-match filter at
    ``scripts/project.py:409-416`` includes the session in the per-walnut
    ``recent_sessions`` projection).

    A session that appears in multiple per-walnut logs is mapped to the
    FIRST walnut whose log mentioned it (sorted by slug for
    determinism); the alternative -- emitting one YAML per walnut --
    would mint duplicate session_ids, which `_squirrels/<id>.yaml`'s
    on-disk shape forbids by construction.

    Sessions present in the world log but absent from every per-walnut
    log are unmapped (returned dict has no entry for that sid). The
    caller (step 4) falls back to the persona label in that case to
    keep the squirrel YAML walnut field non-null.
    """
    out: Dict[str, str] = {}
    if not os.path.isdir(walnut_logs_dir):
        return out
    try:
        names = sorted(os.listdir(walnut_logs_dir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".md"):
            continue
        slug = name[:-len(".md")]
        log_path = os.path.join(walnut_logs_dir, name)
        try:
            body = _strip_frontmatter(_read_text(log_path))
        except OSError:
            continue
        for match in _ENTRY_HEADER_RE.finditer(body):
            sid = match.group("sid")
            if sid not in out:
                out[sid] = slug
    return out


# ---------------------------------------------------------------------------
# Step 1: pre-check
# ---------------------------------------------------------------------------

def step_1_pre_check(*, current_world_root: Optional[str]) -> Dict[str, Any]:
    """Step 1 of the activation transaction.

    Returns ``{"step": 1, "status": "ok" | "needs_confirmation",
    "findings": [...], ...}``.

    The caller (skill prose) drives the ``AskUserQuestion`` confirmation;
    this function stops at returning the findings list because workers
    cannot fire ``AskUserQuestion`` directly.
    """
    lib = _load_demo_lib()
    findings = lib.activation_pre_check(current_world_root)
    if findings:
        return {
            "step": 1,
            "status": "needs_confirmation",
            "findings": findings,
            "current_world_root": current_world_root,
            "message": (
                f"{len(findings)} unsaved-work finding(s) on the "
                f"current live world; surface bordered-block warning "
                f"+ AskUserQuestion before re-running with confirm=True."
            ),
        }
    return {
        "step": 1,
        "status": "ok",
        "findings": [],
    }


# ---------------------------------------------------------------------------
# Step 2: rename partial -> world (atomic same-FS)
# ---------------------------------------------------------------------------

def step_2_promote_partial(
    partial_dir: str,
    *,
    new_world_path: str,
) -> Dict[str, Any]:
    """Atomically rename ``<partial>`` to ``<new_world_path>``.

    Verifies both paths are on the same filesystem (``st_dev`` match
    on the parents) before invoking ``os.rename``. A cross-filesystem
    rename is non-atomic and silently masquerades as a copy under
    Python's ``os.rename``; we fail loud if detected so the transaction
    can be aborted before the world-root pointer flips.
    """
    if not os.path.isdir(partial_dir):
        raise Stage5Error(
            f"step 2: partial dir {partial_dir} is not a directory"
        )
    if os.path.exists(new_world_path):
        raise Stage5Error(
            f"step 2: target world path {new_world_path} already exists; "
            f"refusing to clobber"
        )

    # Same-FS check on parents (or, if target parent doesn't exist
    # yet, on its grandparent that we'll create).
    src_parent = os.path.dirname(_abspath(partial_dir)) or "/"
    dst_parent = os.path.dirname(_abspath(new_world_path)) or "/"
    os.makedirs(dst_parent, exist_ok=True)
    src_dev = os.stat(src_parent).st_dev
    dst_dev = os.stat(dst_parent).st_dev
    if src_dev != dst_dev:
        raise Stage5Error(
            f"step 2: partial {partial_dir} and target {new_world_path} "
            f"are on different filesystems (st_dev mismatch); refusing to "
            f"perform a non-atomic rename"
        )

    os.rename(partial_dir, new_world_path)
    return {
        "step": 2,
        "status": "ok",
        "from": partial_dir,
        "to": new_world_path,
    }


# ---------------------------------------------------------------------------
# Step 3: preferences.yaml
# ---------------------------------------------------------------------------

def _render_preferences_yaml(*, persona_first_name: str) -> str:
    """Render ``<world>/.alive/preferences.yaml`` as a YAML string.

    The named squirrel is ``<persona_first_name>'s squirrel``; full
    defaults: ``action_logging: true`` and ``discovery_hints: true``
    (per the epic spec).
    """
    safe_name = (persona_first_name or "").strip() or "demo"
    squirrel_name = f"{safe_name}'s squirrel"
    return (
        "# Auto-generated by /alive:demo Stage 5\n"
        "# This file customizes the squirrel's voice + behaviour.\n"
        f"squirrel_name: \"{squirrel_name}\"\n"
        "action_logging: true\n"
        "discovery_hints: true\n"
    )


def step_3_preferences(
    world_path: str,
    *,
    spine: Dict[str, Any],
) -> Dict[str, Any]:
    persona = spine.get("persona") or {}
    first_name = persona.get("first_name") or persona.get("name") or "demo"
    if isinstance(first_name, str):
        # Take just the first whitespace-separated token if the
        # spine accidentally smuggled a full name into first_name.
        first_name = first_name.split()[0] if first_name.split() else "demo"
    body = _render_preferences_yaml(persona_first_name=str(first_name))
    target = os.path.join(world_path, ".alive", "preferences.yaml")
    atomic_write_text(target, body)
    return {
        "step": 3,
        "status": "ok",
        "path": target,
        "squirrel_name": f"{first_name}'s squirrel",
    }


# ---------------------------------------------------------------------------
# Step 4: squirrel YAMLs per session
# ---------------------------------------------------------------------------

def _render_squirrel_yaml(
    *,
    sid: str,
    date: str,
    squirrel_name: str,
    walnut: Optional[str],
    started_iso: str,
    last_saved_iso: str,
    transcript: Optional[str],
    cwd: str,
) -> str:
    """Render a single ``_squirrels/<id>.yaml`` body.

    Matches every key in ``templates/squirrel/entry.yaml`` exactly.
    ``saves: 1`` and a backdated ``last_saved`` reflect a saved
    session (so the activation pre-check's ``saves: 0`` predicates
    won't ever flag these synthetic sessions).

    ``walnut`` is the walnut slug owning this session, or ``None`` for
    cross-walnut / world-only sessions. ``project.py:409-416`` skips
    entries whose ``walnut:`` field is ``null`` / empty when populating
    a per-walnut ``recent_sessions`` projection, so writing ``null``
    keeps cross-walnut sessions visible at the world level without
    inventing per-walnut ownership.
    """
    transcript_value = "null" if not transcript else transcript
    walnut_value = "null" if walnut is None else walnut
    return (
        f"session_id: {sid}\n"
        f"runtime_id: squirrel.core@1.0\n"
        f"squirrel_name: \"{squirrel_name}\"\n"
        f"engine: claude-demo\n"
        f"walnut: {walnut_value}\n"
        f"started: {started_iso}\n"
        f"ended: {last_saved_iso}\n"
        f"saves: 1\n"
        f"last_saved: {last_saved_iso}\n"
        f"transcript: {transcript_value}\n"
        f"cwd: {cwd}\n"
        f"rules_loaded: []\n"
        f"recovery_state: null\n"
        f"tags: []\n"
        f"stash: []\n"
        f"actions: []\n"
        f"working: []\n"
    )


def _date_to_iso_z(date: str, *, hour: int = 9) -> str:
    """Format ``YYYY-MM-DD`` -> ``YYYY-MM-DDTHH:00:00Z``.

    Backdates by composing a fixed-time-of-day timestamp; the demo
    world only needs realistic ordering, not minute-level fidelity.
    """
    try:
        dt = _dt.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        # Non-strict date (e.g. already includes time); pass through.
        return date
    dt = dt.replace(hour=hour, minute=0, second=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def step_4_squirrel_yamls(
    world_path: str,
    *,
    spine: Dict[str, Any],
    world_log_path: str,
) -> Dict[str, Any]:
    """Mint ``<world>/.alive/_squirrels/*.yaml`` per session in the timeline.

    Each session YAML carries the walnut SLUG (basename) of the walnut
    its session_id appears under in the per-walnut logs. ``project.py``
    filters squirrel YAMLs by exact-match against ``os.path.basename(walnut)``
    (see ``scripts/project.py:409-416``); writing the persona label
    here produces empty ``recent_sessions`` projections in step 6. The
    sid->walnut map is built from
    ``<world>/_stage_outputs/walnut-logs/<slug>.md``; sessions absent
    from every per-walnut log fall back to the first walnut roster
    slug (so step 6's projection associates them with a real walnut
    rather than a non-existent one keyed off the persona label).
    """
    persona = spine.get("persona") or {}
    first_name = persona.get("first_name") or "demo"
    if isinstance(first_name, str):
        first_name = first_name.split()[0] if first_name.split() else "demo"
    squirrel_name = f"{first_name}'s squirrel"

    sessions = _parse_world_log_sessions(world_log_path)
    walnut_logs_dir = os.path.join(
        world_path, "_stage_outputs", "walnut-logs",
    )
    sid_to_walnut = _build_session_walnut_map(walnut_logs_dir)

    out_dir = os.path.join(world_path, ".alive", "_squirrels")
    os.makedirs(out_dir, exist_ok=True)

    written: List[str] = []
    walnut_assignments: List[Tuple[str, Optional[str]]] = []
    for entry in sessions:
        sid = entry["sid"]
        date = entry["date"]
        if not _SQUIRREL_SID_RE.match(sid):
            continue
        last_saved_iso = _date_to_iso_z(date, hour=18)
        started_iso = _date_to_iso_z(date, hour=9)
        # Per codex review: write null when Stage 3 doesn't give an
        # unambiguous walnut mapping for this session. Inventing a
        # fallback walnut (the first roster slug) would mis-attribute
        # cross-walnut / world-log-only sessions; null lets downstream
        # readers (project.py:409-416 filters by exact-match and skips
        # entries whose walnut field is "null" / empty) intentionally
        # ignore the session at the per-walnut level. Cross-walnut
        # sessions remain visible in the world-level _squirrels/
        # listing for the agent's session-history surface.
        walnut_for_sid = sid_to_walnut.get(sid)
        body = _render_squirrel_yaml(
            sid=sid,
            date=date,
            squirrel_name=squirrel_name,
            walnut=walnut_for_sid if walnut_for_sid else None,
            started_iso=started_iso,
            last_saved_iso=last_saved_iso,
            transcript=None,  # synthetic; no transcript file.
            cwd=world_path,
        )
        target = os.path.join(out_dir, f"{sid}.yaml")
        atomic_write_text(target, body)
        written.append(target)
        walnut_assignments.append((sid, walnut_for_sid))

    return {
        "step": 4,
        "status": "ok",
        "session_count": len(written),
        "yamls": written,
        "walnut_assignments": walnut_assignments,
    }


# ---------------------------------------------------------------------------
# Step 5: completed.json per walnut
# ---------------------------------------------------------------------------

def _walnut_kernel_dirs(world_path: str, spine: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return ``[(walnut_slug, walnut_abs_path), ...]`` for every walnut in spine.

    The walnut directory layout is ``<world>/<domain_dir>/<slug>/``;
    domain_dir is read off the spine roster entry.
    """
    out: List[Tuple[str, str]] = []
    for entry in spine.get("walnut_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        domain = entry.get("domain_dir")
        if not isinstance(slug, str) or not isinstance(domain, str):
            continue
        walnut_path = os.path.join(world_path, domain, slug)
        out.append((slug, walnut_path))
    return out


def _backdated_completion_dates(
    *,
    n: int,
    spine: Dict[str, Any],
) -> List[str]:
    """Generate ``n`` ``YYYY-MM-DD`` strings spread across the spine's time_span.

    Falls back to the persona's last-30-days window when the spine is
    missing time_span. Returned in chronological order.
    """
    ts = spine.get("time_span") or {}
    start_str = ts.get("start")
    end_str = ts.get("end")
    try:
        start = _dt.datetime.strptime(str(start_str), "%Y-%m-%d").date()
        end = _dt.datetime.strptime(str(end_str), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        end = _dt.date.today()
        start = end - _dt.timedelta(days=30)
    if end < start:
        start, end = end, start
    span_days = max((end - start).days, 1)
    if n <= 0:
        return []
    if n == 1:
        return [start.isoformat()]
    out: List[str] = []
    for i in range(n):
        offset = int(round(span_days * i / max(n - 1, 1)))
        d = start + _dt.timedelta(days=offset)
        out.append(d.isoformat())
    return out


def step_5_completed_json(
    world_path: str,
    *,
    spine: Dict[str, Any],
    persona_full_name: str,
) -> Dict[str, Any]:
    """Synthesize ``<walnut>/_kernel/completed.json`` per walnut.

    80 % of each walnut's tasks are pre-marked done with backdated
    ``completed`` dates. Direct ``_common.atomic_write_json`` -- never
    via ``tasks.py`` (which only stamps now). Envelope shape
    ``{"completed": [task, ...]}`` is unchanged so existing readers
    work without modification.

    Tasks are read from the bundle ``tasks.json`` files at
    ``<walnut>/<bundle>/tasks.json`` (Stage 2 produces empty bundles
    with ``{"tasks": []}``). For Stage 5 the bundle scaffolds are
    re-emitted with synthetic tasks; 80 % of those become the
    completed-json payload, 20 % remain in ``tasks.json``.

    NOTE: at this point ``<walnut>/_kernel/tasks.json`` is the
    authoritative source for the walnut's open-task list. Stage 2
    bundle scaffolds carry empty ``tasks.json`` files; the per-walnut
    kernel ``tasks.json`` is created here when missing so the 80/20
    split has something to operate on.

    Idempotent on already-installed worlds (R6): if ``_kernel/insights.md``
    AND ``_kernel/.insights-source`` already exist for a walnut, step 6
    has already promoted that walnut. Re-running the 80/20 split on the
    post-install ``tasks.json`` (which only holds the 20 % remainder)
    would corrupt ``completed.json`` -- so we short-circuit per walnut,
    leaving both files untouched and returning the existing counts in
    the result envelope. This is what makes a full ``activate(...)``
    replay (resume-after-crash, defensive idempotency) a true no-op.
    """
    walnut_dirs = _walnut_kernel_dirs(world_path, spine)
    written: List[Dict[str, Any]] = []

    for slug, walnut_path in walnut_dirs:
        kernel_dir = os.path.join(walnut_path, "_kernel")
        os.makedirs(kernel_dir, exist_ok=True)

        # Idempotency on already-installed worlds (R6 full-activation
        # replay): if step 6 has already run for THIS walnut, both
        # ``_kernel/insights.md`` and the ``.insights-source`` sidecar
        # exist (they're written in immediate succession by
        # ``_install_walnut_prose``). In that case the 80/20 split has
        # already happened on the FIRST run -- ``tasks.json`` now holds
        # only the 20 % remainder and ``completed.json`` holds the 80 %.
        # Re-running the split here would treat the remainder as the
        # full task list and clobber the prior history. Detect the
        # post-install signature and short-circuit, returning the
        # existing metadata so the activate() / step_9_build_log call
        # sites keep working.
        installed_insights = os.path.join(kernel_dir, "insights.md")
        installed_sidecar = os.path.join(kernel_dir, ".insights-source")
        if (
            os.path.isfile(installed_insights)
            and os.path.isfile(installed_sidecar)
        ):
            tasks_path = os.path.join(kernel_dir, "tasks.json")
            completed_path = os.path.join(kernel_dir, "completed.json")
            try:
                with open(tasks_path, "r", encoding="utf-8") as f:
                    remaining = list((json.load(f) or {}).get("tasks") or [])
            except (OSError, json.JSONDecodeError):
                remaining = []
            try:
                with open(completed_path, "r", encoding="utf-8") as f:
                    completed = list(
                        (json.load(f) or {}).get("completed") or []
                    )
            except (OSError, json.JSONDecodeError):
                completed = []
            written.append({
                "walnut": slug,
                "completed_path": completed_path,
                "completed_count": len(completed),
                "remaining_count": len(remaining),
                "skipped_already_installed": True,
            })
            continue

        # Bootstrap a tasks.json if Stage 2 didn't put one at the
        # kernel level. The synthetic tasks here are minimal -- the
        # demo worlds care more about completed.json (the proof point
        # for "world has lived-in history") than about open tasks.
        tasks_path = os.path.join(kernel_dir, "tasks.json")
        if not os.path.isfile(tasks_path):
            atomic_write_json(tasks_path, {"tasks": []})

        try:
            with open(tasks_path, "r", encoding="utf-8") as f:
                task_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            task_data = {"tasks": []}

        all_tasks = list(task_data.get("tasks") or [])
        # If there are no tasks at all, synthesize a small set so the
        # 80/20 split produces a non-empty completed.json (the demo
        # world's value rests on visible past completions).
        if not all_tasks:
            for i in range(5):
                all_tasks.append({
                    "id": f"t{i+1:03d}",
                    "title": f"{slug} milestone {i+1}",
                    "status": "open",
                })

        n_completed = max(int(round(len(all_tasks) * COMPLETED_TASK_FRACTION)), 1)
        n_completed = min(n_completed, len(all_tasks))
        completed_tasks = all_tasks[:n_completed]
        remaining_tasks = all_tasks[n_completed:]

        dates = _backdated_completion_dates(
            n=n_completed,
            spine=spine,
        )
        for task, date in zip(completed_tasks, dates):
            task["status"] = "done"
            task["completed"] = date
            task["completed_by"] = persona_full_name or "demo"

        completed_path = os.path.join(kernel_dir, "completed.json")
        atomic_write_json(completed_path, {"completed": completed_tasks})

        # Rewrite tasks.json with the remaining 20 % so project.py's
        # subsequent --walnut invocation sees the correct open queue.
        atomic_write_json(tasks_path, {"tasks": remaining_tasks})

        written.append({
            "walnut": slug,
            "completed_path": completed_path,
            "completed_count": len(completed_tasks),
            "remaining_count": len(remaining_tasks),
        })

    return {
        "step": 5,
        "status": "ok",
        "walnuts": written,
    }


# ---------------------------------------------------------------------------
# Step 6: install Stage 0-4 outputs into canonical walnut layout
# ---------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    """Return the hex-digest SHA-256 of `path`. Caller ensures path exists."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_install_file(src: str, dst: str, *, kind: str, slug: str) -> str:
    """Move `src` to `dst` via ``os.replace`` with idempotency check.

    Returns one of ``"installed"``, ``"skipped_idempotent"``. Raises
    ``Stage5Error`` with the locked message format when src is missing
    AND dst is missing (the spine declared this slug but no source +
    no prior install exists).

    Idempotency: if dst exists AND (src absent OR sha256(src) == sha256(dst)),
    treat as installed and skip. Both src and dst are inside ``<world>/...``
    by construction, so ``os.replace`` is atomic (no EXDEV possible).
    """
    src_exists = os.path.isfile(src)
    dst_exists = os.path.isfile(dst)
    if dst_exists and (not src_exists or _sha256_file(src) == _sha256_file(dst)):
        # Already installed (resume case); leave stale src in place if it
        # somehow survived -- the cleanup step removes the entities tree
        # wholesale, so a same-content src is harmless until then.
        return "skipped_idempotent"
    if not src_exists:
        raise Stage5Error(
            f"step 6: {kind} source not found for {slug} at {src}"
        )
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    return "installed"


def _install_walnut_prose(
    world_path: str,
    walnut_entry: Dict[str, Any],
    stage_outputs_dir: str,
) -> Dict[str, str]:
    """Install one walnut's key/log/insights into its `_kernel/`.

    Returns ``{"slug": ..., "insights_source": "stage4" | "stage2_placeholder"}``.
    Raises ``Stage5Error`` when Stage 2 entity dir or Stage 3 walnut-log are
    absent (mandatory inputs). Stage 4 walnut-insights is optional; on
    absence we fall through to the Stage 2 placeholder
    (``_stage_outputs/entities/<slug>/insights.md``).
    """
    slug = walnut_entry["slug"]
    domain = walnut_entry["domain_dir"]
    walnut_root = os.path.join(world_path, domain, slug)
    kernel_dir = os.path.join(walnut_root, "_kernel")

    src_key = os.path.join(stage_outputs_dir, "entities", slug, "key.md")
    src_log = os.path.join(stage_outputs_dir, "walnut-logs", f"{slug}.md")
    src_insights_stage4 = os.path.join(
        stage_outputs_dir, "walnut-insights", f"{slug}.md",
    )
    src_insights_stage2 = os.path.join(
        stage_outputs_dir, "entities", slug, "insights.md",
    )

    dst_key = os.path.join(kernel_dir, "key.md")
    dst_log = os.path.join(kernel_dir, "log.md")
    dst_insights = os.path.join(kernel_dir, "insights.md")

    _atomic_install_file(src_key, dst_key, kind="walnut key", slug=slug)
    _atomic_install_file(src_log, dst_log, kind="walnut log", slug=slug)

    # Stage 4 walnut-insights is conditional -- not every walnut earns
    # one. When absent, fall through to the Stage 2 placeholder.
    #
    # Provenance sidecar: we persist the original classification
    # (``stage4`` / ``stage2_placeholder``) into a per-walnut sidecar
    # ``<kernel>/.insights-source`` immediately after install so an
    # idempotent re-run (where both stage4 + stage2 sources have been
    # rmtree'd by ``_cleanup_entities_dir``) can recover the original
    # provenance instead of returning ``"already_installed"``. Spec
    # contract: ``insights_sources[slug]`` is one of
    # ``{"stage4", "stage2_placeholder"}``.
    #
    # Sidecar-first idempotency: if the sidecar exists, the dst was
    # written by a prior run -- trust it as the authoritative provenance
    # record and short-circuit BEFORE re-picking a source. This protects
    # against partial-failure reruns where step 6 crashed AFTER moving
    # the Stage 4 source into place (so ``src_insights_stage4`` is gone)
    # but BEFORE ``_cleanup_entities_dir`` rmtree'd Stage 2 (so
    # ``src_insights_stage2`` is still present). Without this guard the
    # fall-through below would silently downgrade a Stage 4 install to
    # the Stage 2 placeholder on resume.
    sidecar_path = os.path.join(kernel_dir, ".insights-source")
    if os.path.isfile(sidecar_path) and os.path.isfile(dst_insights):
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                recorded = f.read().strip()
        except OSError as exc:
            raise Stage5Error(
                f"step 6: failed to read insights-source sidecar for "
                f"{slug} at {sidecar_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if recorded not in ("stage4", "stage2_placeholder"):
            raise Stage5Error(
                f"step 6: malformed insights-source sidecar for {slug} "
                f"at {sidecar_path}: {recorded!r} (expected "
                f"'stage4' or 'stage2_placeholder')"
            )
        return {"slug": slug, "insights_source": recorded}

    # No sidecar (genuine first install) -- pick a source by priority.
    # We validate BOTH source decisions (path existence) before
    # touching the dst so that a partial pair (e.g. dst written but
    # sidecar missing) cannot be created mid-flight: insights.md and
    # the sidecar always land in immediate succession with both inputs
    # validated upfront.
    if os.path.isfile(src_insights_stage4):
        chosen_src = src_insights_stage4
        insights_source = "stage4"
    elif os.path.isfile(src_insights_stage2):
        chosen_src = src_insights_stage2
        insights_source = "stage2_placeholder"
    elif os.path.isfile(dst_insights):
        # Dst exists but sidecar is missing -- legacy world (pre-
        # sidecar) or manual deletion. We have no way to recover the
        # original classification; raise rather than silently weakening
        # provenance.
        raise Stage5Error(
            f"step 6: insights-source sidecar missing for {slug} at "
            f"{sidecar_path}; cannot recover original provenance "
            f"after both Stage 4 + Stage 2 sources were cleaned up"
        )
    else:
        # No dst, no stage4 src, no stage2 src: spine declared this
        # walnut but neither input was produced. Fail-fast with the
        # locked message format pointing at the stage2 placeholder
        # (the lower-priority source) since that's the one Stage 2 is
        # mandated to produce for every walnut.
        raise Stage5Error(
            f"step 6: walnut insights source not found for {slug} at "
            f"{src_insights_stage2}"
        )

    # Both inputs validated -- install insights.md and the sidecar in
    # immediate succession. A crash between the two leaves the sidecar
    # absent, which the next run treats as a genuine first install (no
    # provenance lie possible).
    _atomic_install_file(
        chosen_src, dst_insights,
        kind="walnut insights", slug=slug,
    )
    atomic_write_text(sidecar_path, f"{insights_source}\n")

    return {"slug": slug, "insights_source": insights_source}


def _install_person_walnut(
    world_path: str,
    person_entry: Dict[str, Any],
    stage_outputs_dir: str,
) -> str:
    """Install one person's key/log/insights into ``02_Life/people/<slug>/_kernel/``.

    People roster has no ``domain_dir`` -- the path is hardcoded
    (``02_Life/people/<slug>/_kernel/``). Bootstraps empty
    ``tasks.json`` / ``completed.json`` via ``atomic_write_json``.
    Returns the installed slug.
    """
    slug = person_entry["slug"]
    kernel_dir = os.path.join(world_path, "02_Life", "people", slug, "_kernel")

    src_key = os.path.join(stage_outputs_dir, "entities", slug, "key.md")
    src_log = os.path.join(stage_outputs_dir, "people-logs", f"{slug}.md")
    # Stage 4 doesn't emit per-person insights; the Stage 2 placeholder is
    # the only source.
    src_insights = os.path.join(
        stage_outputs_dir, "entities", slug, "insights.md",
    )

    dst_key = os.path.join(kernel_dir, "key.md")
    dst_log = os.path.join(kernel_dir, "log.md")
    dst_insights = os.path.join(kernel_dir, "insights.md")

    _atomic_install_file(src_key, dst_key, kind="person key", slug=slug)
    _atomic_install_file(src_log, dst_log, kind="person log", slug=slug)
    _atomic_install_file(
        src_insights, dst_insights, kind="person insights", slug=slug,
    )

    # Bootstrap empty tasks / completed only if absent (idempotent).
    tasks_path = os.path.join(kernel_dir, "tasks.json")
    if not os.path.isfile(tasks_path):
        atomic_write_json(tasks_path, {"tasks": []})
    completed_path = os.path.join(kernel_dir, "completed.json")
    if not os.path.isfile(completed_path):
        atomic_write_json(completed_path, {"completed": []})

    return slug


def _install_bundle(
    world_path: str,
    bundle_entry: Dict[str, Any],
    walnut_by_slug: Dict[str, Dict[str, Any]],
    stage_outputs_dir: str,
) -> str:
    """Install one bundle's manifest + tasks at ``<walnut>/<bundle>/``.

    Resolves the parent walnut's ``domain_dir`` via the ``walnut_by_slug``
    lookup; raises ``Stage5Error`` on lookup miss (the spine declared a
    bundle whose parent walnut isn't in the walnut roster). Returns the
    compound slug ``<walnut>__<bundle>`` for caller logging.
    """
    walnut_slug = bundle_entry["walnut_slug"]
    bundle_slug = bundle_entry["slug"]
    compound = f"{walnut_slug}__{bundle_slug}"
    parent = walnut_by_slug.get(walnut_slug)
    if parent is None:
        raise Stage5Error(
            f"step 6: bundle parent walnut not found for {compound} at "
            f"<spine.walnut_roster missing slug {walnut_slug!r}>"
        )
    domain = parent["domain_dir"]
    bundle_dir = os.path.join(world_path, domain, walnut_slug, bundle_slug)

    src_manifest = os.path.join(
        stage_outputs_dir, "entities", compound, "context.manifest.yaml",
    )
    src_tasks = os.path.join(
        stage_outputs_dir, "entities", compound, "tasks.json",
    )

    dst_manifest = os.path.join(bundle_dir, "context.manifest.yaml")
    dst_tasks = os.path.join(bundle_dir, "tasks.json")

    _atomic_install_file(
        src_manifest, dst_manifest, kind="bundle manifest", slug=compound,
    )
    _atomic_install_file(
        src_tasks, dst_tasks, kind="bundle tasks", slug=compound,
    )
    return compound


def _install_world_files(world_path: str, stage_outputs_dir: str) -> int:
    """Install the world-level log + insights into ``<world>/.alive/``.

    Returns the count of world-level files installed (always 2 in the
    happy path; idempotent skip still counts toward the total).
    """
    src_log = os.path.join(stage_outputs_dir, "log.md")
    src_insights = os.path.join(stage_outputs_dir, "insights.md")
    dst_log = os.path.join(world_path, ".alive", "log.md")
    dst_insights = os.path.join(world_path, ".alive", "insights.md")

    _atomic_install_file(src_log, dst_log, kind="world log", slug="<world>")
    _atomic_install_file(
        src_insights, dst_insights, kind="world insights", slug="<world>",
    )
    return 2


def _cleanup_entities_dir(stage_outputs_dir: str) -> None:
    """Remove the entities tree after every install succeeds.

    Refuses to follow a symlink-to-directory (the symlink trap that makes
    ``shutil.rmtree`` capable of nuking content outside the world tree).
    Failure of ``rmtree`` itself raises ``Stage5Error`` -- install +
    cleanup is one logical unit. A re-run will be a no-op for installs
    (everything is already at the canonical paths) and will retry the
    cleanup.
    """
    entities = os.path.join(stage_outputs_dir, "entities")
    if not os.path.exists(entities):
        return
    if Path(entities).is_symlink():
        raise Stage5Error(
            f"step 6: entities path is a symlink at {entities}; refusing "
            f"to rmtree a symlink (would escape the world tree)"
        )
    try:
        shutil.rmtree(entities)
    except OSError as exc:
        raise Stage5Error(
            f"step 6: failed to remove entities dir at {entities}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def step_6_install_entities(
    world_path: str,
    *,
    spine: Dict[str, Any],
) -> Dict[str, Any]:
    """Move Stage 0-4 outputs from ``_stage_outputs/`` into the canonical layout.

    Iterates the spine rosters (NOT the filesystem) so a slug declared
    by the spine but missing its Stage 2/3 source is a fail-fast
    ``Stage5Error`` rather than a silent drop. Returns::

        {
          "step": 6,
          "status": "ok",
          "installed_walnuts": int,
          "installed_people": int,
          "installed_bundles": int,
          "world_files": int,
          "insights_sources": {<walnut_slug>: "stage4" | "stage2_placeholder"},
        }

    Idempotent: a second invocation against a world whose installs
    already landed is a no-op (every per-file install short-circuits via
    SHA-256 equality, the entities-dir cleanup is no-op when the dir is
    already gone). Plain ``Stage5Error`` on any missing spine-declared
    source -- this step does NOT enter the
    ``_build_projection_failure_envelope`` path (that wrapper is for
    subprocess crashes in step_7 / step_8).
    """
    stage_outputs_dir = os.path.join(world_path, "_stage_outputs")

    walnut_roster = spine.get("walnut_roster") or []
    people_roster = spine.get("people_roster") or []
    bundle_distribution = spine.get("bundle_distribution") or []

    # Build walnut lookup for bundle parent resolution.
    walnut_by_slug: Dict[str, Dict[str, Any]] = {}
    for entry in walnut_roster:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str):
            walnut_by_slug[slug] = entry

    insights_sources: Dict[str, str] = {}
    installed_walnuts = 0
    installed_people = 0
    installed_bundles = 0

    # Walnuts
    for entry in walnut_roster:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        domain = entry.get("domain_dir")
        if not isinstance(slug, str) or not isinstance(domain, str):
            continue
        result = _install_walnut_prose(world_path, entry, stage_outputs_dir)
        insights_sources[result["slug"]] = result["insights_source"]
        installed_walnuts += 1

    # People
    for entry in people_roster:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str):
            continue
        _install_person_walnut(world_path, entry, stage_outputs_dir)
        installed_people += 1

    # Bundles
    for entry in bundle_distribution:
        if not isinstance(entry, dict):
            continue
        bundle_slug = entry.get("slug")
        walnut_slug = entry.get("walnut_slug")
        if not isinstance(bundle_slug, str) or not isinstance(walnut_slug, str):
            continue
        _install_bundle(world_path, entry, walnut_by_slug, stage_outputs_dir)
        installed_bundles += 1

    # World-level files
    world_files = _install_world_files(world_path, stage_outputs_dir)

    # Cleanup the entities dir after every install lands. The other
    # `_stage_outputs/` subdirs (walnut-logs/, people-logs/, walnut-insights/,
    # log.md, insights.md, spine.json, anchor_moments.json, stageN_done.json)
    # stay as build provenance -- none contain `key.md`, so the index
    # walker won't pick them up.
    _cleanup_entities_dir(stage_outputs_dir)

    return {
        "step": 6,
        "status": "ok",
        "installed_walnuts": installed_walnuts,
        "installed_people": installed_people,
        "installed_bundles": installed_bundles,
        "world_files": world_files,
        "insights_sources": insights_sources,
    }


# ---------------------------------------------------------------------------
# Step 7: project.py per walnut
# ---------------------------------------------------------------------------

def step_7_project(
    world_path: str,
    *,
    spine: Dict[str, Any],
    plugin_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Shell out to ``project.py --walnut`` per walnut.

    The subprocess inherits the parent env PLUS ``ALIVE_WORLD_ROOT_OVERRIDE``
    pointing at ``world_path``. Without this override, ``project.py``'s
    world-root resolver reads ``~/.config/alive/world-root`` (the
    canonical pointer), which at step 7 still names the PREVIOUS live
    world (the commit doesn't fire until step 11). project.py walks
    ``<world>/.alive/_squirrels/`` to populate ``recent_sessions`` for
    each walnut; reading the wrong world produces empty projections.
    The override forces project.py to operate hermetically against the
    new world without depending on the pre-commit pointer state.
    """
    plugin_root = plugin_root or resolve_plugin_root()
    project_script = os.path.join(plugin_root, "scripts", "project.py")
    if not os.path.isfile(project_script):
        raise Stage5Error(
            f"step 7: project.py not found at {project_script}"
        )
    walnut_dirs = _walnut_kernel_dirs(world_path, spine)

    subprocess_env = dict(os.environ)
    subprocess_env["ALIVE_WORLD_ROOT_OVERRIDE"] = world_path

    runs: List[Dict[str, Any]] = []
    for slug, walnut_path in walnut_dirs:
        if not os.path.isdir(walnut_path):
            # Step 5 should have created this; if not, fall through
            # by creating the kernel skeleton so project.py can read
            # tasks.json / log.md from valid paths.
            os.makedirs(os.path.join(walnut_path, "_kernel"), exist_ok=True)
        proc = subprocess.run(
            [sys.executable, project_script, "--walnut", walnut_path],
            capture_output=True,
            text=True,
            check=False,
            env=subprocess_env,
        )
        runs.append({
            "walnut": slug,
            "rc": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        })
        if proc.returncode != 0:
            raise Stage5Error(
                f"step 7: project.py --walnut {walnut_path} failed: "
                f"rc={proc.returncode} stderr={proc.stderr.strip()!r}"
            )
    return {
        "step": 7,
        "status": "ok",
        "runs": runs,
    }


# ---------------------------------------------------------------------------
# Step 8: generate-index.py
# ---------------------------------------------------------------------------

def step_8_generate_index(
    world_path: str,
    *,
    plugin_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Shell out to ``generate-index.py`` against the new world path.

    ``generate-index.py`` takes the world root as a positional argument
    so it does not rely on the pointer for resolution; the env-var
    override is set anyway for parity with step 7 (so any helper the
    script invokes downstream sees the same world).
    """
    plugin_root = plugin_root or resolve_plugin_root()
    index_script = os.path.join(plugin_root, "scripts", "generate-index.py")
    if not os.path.isfile(index_script):
        raise Stage5Error(
            f"step 8: generate-index.py not found at {index_script}"
        )
    subprocess_env = dict(os.environ)
    subprocess_env["ALIVE_WORLD_ROOT_OVERRIDE"] = world_path
    proc = subprocess.run(
        [sys.executable, index_script, world_path],
        capture_output=True,
        text=True,
        check=False,
        env=subprocess_env,
    )
    if proc.returncode != 0:
        raise Stage5Error(
            f"step 8: generate-index.py {world_path} failed: "
            f"rc={proc.returncode} stderr={proc.stderr.strip()!r}"
        )
    return {
        "step": 8,
        "status": "ok",
        "rc": proc.returncode,
        "stdout": proc.stdout.strip(),
    }


# ---------------------------------------------------------------------------
# Step 9: build log
# ---------------------------------------------------------------------------

def _read_stage_marker_timestamps(partial_outputs_dir: str) -> Dict[str, str]:
    """Read each ``stage{N}_done.json`` ``frozen_at`` timestamp.

    The post-rename world holds the partial's ``_stage_outputs/``
    directory verbatim; we read the markers off the new world path.
    Missing markers are recorded as ``"missing"`` rather than aborting
    the build log -- the build log is purely advisory provenance.
    """
    out: Dict[str, str] = {}
    for n in range(0, 5):
        path = os.path.join(partial_outputs_dir, f"stage{n}_done.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts = data.get("frozen_at") or "unknown"
        except (OSError, json.JSONDecodeError):
            ts = "missing"
        out[f"stage_{n}"] = ts
    return out


def step_9_build_log(
    world_path: str,
    *,
    ulid: str,
    label: str,
    activated_at: str,
    insights_sources: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Write ``<world>/.alive/_demo-build-log.md``.

    Frontmatter shape (parsed by ``state.py`` self-heal to recover
    ``active_world``)::

        ---
        ulid: wld_...
        label: alex-boring
        activated_at: 2026-04-29T12:00:00Z
        ---
        # Build log
        - Stage 0 frozen: ...
        ...

    ``insights_sources`` (optional) is the per-walnut provenance map
    returned by ``step_6_install_entities``. When provided, the build
    log surfaces it as a "Insights provenance" subsection so R5's
    build-log-surfacing requirement is satisfied. Pass ``None`` from
    callers that have no install step (e.g. the preset path, which
    ships pre-baked insights via copytree).
    """
    stage_outputs = os.path.join(world_path, "_stage_outputs")
    timestamps = _read_stage_marker_timestamps(stage_outputs)

    body_lines = [
        "---",
        f"ulid: {ulid}",
        f"label: {label}",
        f"activated_at: {activated_at}",
        "---",
        "",
        "# Demo world build log",
        "",
        f"World ulid {ulid} (label {label}) was activated at {activated_at}.",
        "Stage marker timestamps:",
        "",
    ]
    for n in range(0, 5):
        body_lines.append(
            f"- stage {n}: {timestamps.get(f'stage_{n}', 'missing')}"
        )
    body_lines.append("")

    if insights_sources:
        body_lines.append("## Insights provenance")
        body_lines.append("")
        body_lines.append(
            "Per-walnut walnut-insights source on first install "
            "(``stage4`` = Stage 4 LLM-generated, "
            "``stage2_placeholder`` = Stage 2 deterministic placeholder):"
        )
        body_lines.append("")
        for slug in sorted(insights_sources.keys()):
            body_lines.append(f"- {slug}: {insights_sources[slug]}")
        body_lines.append("")

    body_lines.append(
        "Generated by /alive:demo Stage 5 (fn-2-2zz.9). All five "
        "stages are deterministic plus four LLM passes, gated by "
        "stage-local validators."
    )
    body_lines.append("")

    target = os.path.join(world_path, ".alive", "_demo-build-log.md")
    atomic_write_text(target, "\n".join(body_lines))
    return {
        "step": 9,
        "status": "ok",
        "path": target,
        "stage_timestamps": timestamps,
        "insights_sources": dict(insights_sources) if insights_sources else {},
    }


# ---------------------------------------------------------------------------
# Step 10: stage demo-state.json (pre-commit metadata)
# ---------------------------------------------------------------------------

def step_10_stage_demo_state(
    *,
    ulid: str,
    label: str,
    world_path: str,
    activated_at: str,
) -> Dict[str, Any]:
    """Atomically update ``demo-state.json`` to stage the new world.

    Caches the previous ``world-root`` value into ``previous_world_root``,
    marks the partial's entry in ``partial_generations`` as ``promoted``,
    populates ``active_world``. **Pre-commit metadata staging.** Step 11
    fires the actual pointer write -- if step 10 succeeds but the process
    crashes before step 11, the next ``state.py`` load self-heals
    demo-state back to match the (still-old) pointer.
    """
    state_mod = _load_demo_state()
    previous = read_world_root_file()
    previous_str: Optional[str] = str(previous) if previous is not None else None

    new_active = {
        "ulid": ulid,
        "label": label,
        "path": world_path,
        "activated_at": activated_at,
    }

    with state_mod.with_locked_state() as state:
        state["previous_world_root"] = previous_str
        state["active_world"] = new_active
        partials = state.get("partial_generations") or []
        for entry in partials:
            if entry.get("ulid") == ulid:
                entry["status"] = "promoted"
                entry["last_updated"] = activated_at
                break
        state["partial_generations"] = partials

    return {
        "step": 10,
        "status": "ok",
        "previous_world_root": previous_str,
        "active_world": new_active,
    }


# ---------------------------------------------------------------------------
# Step 11: world-root commit
# ---------------------------------------------------------------------------

def step_11_commit_world_root(world_path: str) -> Dict[str, Any]:
    """THE single commit point: atomic write of ``~/.config/alive/world-root``.

    Delegates to ``_world_root_io.write_world_root_file`` (shipped in
    #64). We do NOT add a new inline writer -- the canonical writer is
    the only sanctioned path so ``mode 0600`` + lexical normalization +
    atomic semantics are guaranteed.
    """
    write_world_root_file(world_path)
    return {
        "step": 11,
        "status": "ok",
        "world_path": world_path,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _validate_partial_ready(partial_dir: str) -> None:
    """Verify all stage{0..4}_done.json markers exist and are frozen."""
    for n in range(0, 5):
        # Stage 1 marker is anchor envelope; the partial's
        # ``_stage_outputs/anchor_moments.json`` carries `frozen` rather
        # than a stage1_done.json file. Tolerate either shape.
        if n == 1:
            anchor_path = os.path.join(
                partial_dir, "_stage_outputs", "anchor_moments.json",
            )
            stage1_done = os.path.join(
                partial_dir, "_stage_outputs", "stage1_done.json",
            )
            if os.path.isfile(stage1_done):
                marker = _load_json(stage1_done, "stage1_done.json")
                if not marker.get("frozen"):
                    raise Stage5NotReady(
                        f"stage1_done.json present but `frozen` is not true"
                    )
            elif os.path.isfile(anchor_path):
                marker = _load_json(anchor_path, "anchor_moments.json")
                if not marker.get("frozen"):
                    raise Stage5NotReady(
                        f"anchor_moments.json present but `frozen` is not true"
                    )
            else:
                raise Stage5NotReady(
                    f"missing stage 1 marker: neither stage1_done.json "
                    f"nor a frozen anchor_moments.json present"
                )
            continue

        marker_path = _stage_done_path(partial_dir, n)
        if not os.path.isfile(marker_path):
            raise Stage5NotReady(
                f"missing stage{n}_done.json at {marker_path}; "
                f"run Stages 0-4 to completion before activating"
            )
        marker = _load_json(marker_path, f"stage{n}_done.json")
        if not marker.get("frozen"):
            raise Stage5NotReady(
                f"stage{n}_done.json at {marker_path} is not frozen"
            )


#: Sidecar marker filename. Persisted into the partial dir BEFORE step 2
#: so a re-invocation after a mid-transaction crash can recover the
#: ULID + target path without remitting (otherwise step 2's atomic
#: rename would have moved the partial out from under the original
#: --partial path, leaving the user no way to resume). The marker
#: travels with the partial when step 2 renames it; a re-run can be
#: invoked with ``--partial <renamed-world-path>`` to resume.
_ACTIVATION_MARKER_FILENAME = ".alive-demo-activation.json"


def _activation_marker_path(partial_dir: str) -> str:
    return os.path.join(partial_dir, _ACTIVATION_MARKER_FILENAME)


def _read_activation_marker(partial_dir: str) -> Optional[Dict[str, Any]]:
    """Read the sidecar marker (if present); validate shape; else None.

    On any read / parse failure, returns ``None`` -- the caller mints
    a fresh activation identity rather than failing on a corrupt
    marker. The marker exists only as a hint, not as a hard contract.
    """
    path = _activation_marker_path(partial_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("ulid"), str) or not data["ulid"].startswith("wld_"):
        return None
    if not isinstance(data.get("target_world_path"), str):
        return None
    return data


def _write_activation_marker(
    partial_dir: str,
    *,
    ulid: str,
    label: str,
    target_world_path: str,
    started_at: str,
) -> None:
    """Persist the sidecar marker. Idempotent."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ulid": ulid,
        "label": label,
        "target_world_path": target_world_path,
        "started_at": started_at,
    }
    atomic_write_json(_activation_marker_path(partial_dir), payload)


def prepare_activation(
    partial_dir,
    *,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Dry-run validator: every stage{0..4} marker present + pre-check.

    Returns a planning dict with the resolved target world path and the
    pre-check findings, BUT does not perform any writes.

    Idempotent identity: if the partial directory carries the sidecar
    activation marker (``.alive-demo-activation.json``), the marker's
    ``ulid`` + ``target_world_path`` are reused so a mid-transaction
    crash can be resumed without minting a fresh identity. Without the
    marker (first run), a fresh ULID is generated and the target is
    derived as ``<base>/<ulid>/``.
    """
    partial_dir = _abspath(partial_dir)
    _validate_partial_ready(partial_dir)

    spine = _load_json(
        os.path.join(partial_dir, "_stage_outputs", "spine.json"),
        "spine.json",
    )

    lib = _load_demo_lib()
    label = (spine.get("persona") or {}).get("label") or "demo"
    base = _abspath(base_dir) if base_dir else _demo_base_dir()

    marker = _read_activation_marker(partial_dir)
    if marker is not None:
        ulid = marker["ulid"]
        target = marker["target_world_path"]
        identity_source = "marker"
    else:
        ulid = lib.new_world_ulid()
        target = os.path.join(base, ulid)
        identity_source = "fresh"

    current_root = read_world_root_file()
    findings = lib.activation_pre_check(
        str(current_root) if current_root is not None else None
    )

    return {
        "partial_dir": partial_dir,
        "target_world_path": target,
        "ulid": ulid,
        "label": label,
        "identity_source": identity_source,
        "current_world_root": str(current_root) if current_root is not None else None,
        "findings": findings,
        "needs_confirmation": bool(findings),
    }


def activate(
    partial_dir,
    *,
    confirm: bool = False,
    base_dir: Optional[str] = None,
    plugin_root: Optional[str] = None,
    fail_after_step: Optional[int] = None,
    surface_failure_blocks: bool = False,
) -> Dict[str, Any]:
    """Run the full 11-step activation transaction.

    Args:
        partial_dir: path to the ``<base>/wld_<ulid>.partial/`` directory.
        confirm: must be True if ``activation_pre_check`` returns findings.
        base_dir: override the demo base dir (test seam; production
            callers leave this unset and rely on ``$ALIVE_DEMO_BASE_DIR``).
        plugin_root: override the plugin root path (test seam).
        fail_after_step: TEST SEAM ONLY -- when set to ``N``, the
            transaction raises ``RuntimeError`` immediately AFTER
            completing step ``N`` (so step ``N+1`` does not run). The
            crash-consistency test suite uses this to verify the
            invariants at every boundary 1->2 ... 10->11. Production
            callers MUST leave this None.

    Returns:
        ``{"status": "ok" | "needs_confirmation" | "failed",
        "steps": [<per-step dict>, ...], "ulid": ..., "world_path": ...}``.

        On ``needs_confirmation``, the step-1 dict carries the findings
        list and the caller drives the bordered-block + AskUserQuestion;
        re-invoke with ``confirm=True`` to proceed. On other errors the
        function raises ``Stage5Error`` (callers translate to a
        bordered-block error surface).
    """
    plan = prepare_activation(partial_dir, base_dir=base_dir)
    partial_dir = plan["partial_dir"]
    ulid = plan["ulid"]
    label = plan["label"]
    target = plan["target_world_path"]
    current_world_root = plan["current_world_root"]

    spine = _load_json(
        os.path.join(partial_dir, "_stage_outputs", "spine.json"),
        "spine.json",
    )
    persona = spine.get("persona") or {}
    persona_full_name = persona.get("name") or "demo"

    activated_at = iso_now()
    steps: List[Dict[str, Any]] = []

    # Pre-step bookkeeping: stage the activation identity in
    # demo-state.json (status=pending_activation) AND write the
    # sidecar marker into the partial dir BEFORE step 2 fires. The
    # marker is what makes the activation idempotent on retries:
    # without it, step 2 (the rename) would move the partial out from
    # under the original `--partial` path, and a re-run with the same
    # path would fail at `_validate_partial_ready`. With it, a re-run
    # against either the original partial path (if the rename hasn't
    # fired yet) OR the renamed world path can recover the ULID +
    # target verbatim and resume from the first incomplete step. The
    # demo-state pending entry mirrors the same identity so a CLI
    # listing tool can surface "in-flight activation" state.
    _write_activation_marker(
        partial_dir,
        ulid=ulid,
        label=label,
        target_world_path=target,
        started_at=activated_at,
    )

    # Stage a `pending_activation` partial-generation entry so list /
    # status surfaces an in-flight activation cleanly. (We use the
    # existing `in_progress` enum value here -- the schema's status
    # vocabulary is `in_progress | promoted | failed`, and we mark
    # `promoted` only after step 9 stages metadata.) Skipping the
    # demo-state mutation under flock when the entry already exists
    # for this ulid -- step 9 will rewrite it with `promoted`.
    state_mod = _load_demo_state()
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
        else:
            existing["last_updated"] = activated_at
            existing["stage"] = "5_promote"
        state["partial_generations"] = partials

    # Step 1
    s1 = step_1_pre_check(current_world_root=current_world_root)
    steps.append(s1)
    if s1.get("status") == "needs_confirmation" and not confirm:
        return {
            "status": "needs_confirmation",
            "steps": steps,
            "ulid": ulid,
            "label": label,
            "world_path": target,
            "findings": s1.get("findings", []),
        }
    _maybe_crash(fail_after_step, 1)

    # Step 2: skip if the rename already happened on a previous run
    # (idempotent resume). We detect this by checking whether the
    # target world path already exists AND carries the same activation
    # marker we just wrote into the (potentially-already-renamed)
    # partial dir. The cleanest test: if `partial_dir == target` or
    # if `partial_dir` doesn't exist but `target` does AND target's
    # marker matches our ulid, the rename is already done.
    rename_already_done = False
    if partial_dir == target and os.path.isdir(target):
        rename_already_done = True
    elif not os.path.isdir(partial_dir) and os.path.isdir(target):
        target_marker = _read_activation_marker(target)
        if target_marker and target_marker.get("ulid") == ulid:
            rename_already_done = True
            partial_dir = target

    if rename_already_done:
        steps.append({
            "step": 2,
            "status": "skipped_already_done",
            "from": partial_dir,
            "to": target,
        })
    else:
        s2 = step_2_promote_partial(partial_dir, new_world_path=target)
        steps.append(s2)
    _maybe_crash(fail_after_step, 2)

    # Step 3
    s3 = step_3_preferences(target, spine=spine)
    steps.append(s3)
    _maybe_crash(fail_after_step, 3)

    # Step 4
    world_log_path = os.path.join(target, "_stage_outputs", "log.md")
    s4 = step_4_squirrel_yamls(
        target, spine=spine, world_log_path=world_log_path,
    )
    steps.append(s4)
    _maybe_crash(fail_after_step, 4)

    # Step 5
    s5 = step_5_completed_json(
        target, spine=spine, persona_full_name=persona_full_name,
    )
    steps.append(s5)
    _maybe_crash(fail_after_step, 5)

    # Step 6 -- install Stage 0-4 outputs into canonical walnut layout.
    # Failure here is a plain Stage5Error (NOT a projection-failure
    # envelope -- that wrapper is for subprocess crashes in step_7 /
    # step_8). The pointer commit at step 11 has not fired, so the
    # canonical world-root is untouched on raise.
    s6 = step_6_install_entities(target, spine=spine)
    steps.append(s6)
    _maybe_crash(fail_after_step, 6)

    # Step 7 -- per-walnut project.py shell-out. Failure here means the
    # deterministic projection layer crashed before the activation
    # transaction reached step 11's commit point. With
    # surface_failure_blocks=True we translate the crash into a failure
    # envelope (15b in the epic spec) and abort cleanly; the caller
    # prints the rendered_block at the squirrel surface. Without it, we
    # re-raise (preserves the existing Stage5Error contract for callers
    # that haven't migrated yet).
    try:
        s7 = step_7_project(target, spine=spine, plugin_root=plugin_root)
    except (Stage5Error, subprocess.SubprocessError, OSError) as exc:
        if not surface_failure_blocks:
            raise
        return _build_projection_failure_envelope(
            exc=exc,
            partial_dir=target,
            ulid=ulid,
            label=label,
            steps=steps,
            world_path=target,
            failing_step=7,
        )
    steps.append(s7)
    _maybe_crash(fail_after_step, 7)

    # Step 8 -- world-wide generate-index.py shell-out. Same wrapping
    # contract as step 7.
    try:
        s8 = step_8_generate_index(target, plugin_root=plugin_root)
    except (Stage5Error, subprocess.SubprocessError, OSError) as exc:
        if not surface_failure_blocks:
            raise
        return _build_projection_failure_envelope(
            exc=exc,
            partial_dir=target,
            ulid=ulid,
            label=label,
            steps=steps,
            world_path=target,
            failing_step=8,
        )
    steps.append(s8)
    _maybe_crash(fail_after_step, 8)

    # Step 9 -- build log. Thread step 6's per-walnut
    # ``insights_sources`` map into the log so R5's provenance
    # surfacing requirement is satisfied.
    s9 = step_9_build_log(
        target,
        ulid=ulid,
        label=label,
        activated_at=activated_at,
        insights_sources=s6.get("insights_sources"),
    )
    steps.append(s9)
    _maybe_crash(fail_after_step, 9)

    # Step 10 -- demo-state mutation. OSError here means the atomic write
    # failed (disk full, read-only filesystem, permission denied). We
    # MUST NOT try to mutate demo-state.json from the failure handler
    # because that mutation would also fail; the handler's contract
    # explicitly notes "demo-state.json NOT corrupted" (the atomic
    # rename either lands the new content fully or leaves the previous
    # content intact). 15c in the epic spec.
    try:
        s10 = step_10_stage_demo_state(
            ulid=ulid, label=label, world_path=target, activated_at=activated_at,
        )
    except OSError as exc:
        if not surface_failure_blocks:
            raise
        lib = _load_demo_lib()
        target_path = exc.filename or _state_path_hint()
        report = lib.report_atomic_write_failure(target_path, exc)
        return {
            "status": "failed",
            "failure_mode": "atomic_write_failure",
            "rendered_block": report["rendered_block"],
            "errno": report.get("errno"),
            "target_path": report.get("target_path"),
            "ulid": ulid,
            "label": label,
            "world_path": target,
            "steps": steps,
        }
    steps.append(s10)
    _maybe_crash(fail_after_step, 10)

    # Step 11 -- THE commit point.
    s11 = step_11_commit_world_root(target)
    steps.append(s11)
    _maybe_crash(fail_after_step, 11)

    return {
        "status": "ok",
        "steps": steps,
        "ulid": ulid,
        "label": label,
        "world_path": target,
        "activated_at": activated_at,
    }


def _build_projection_failure_envelope(
    *,
    exc: BaseException,
    partial_dir: str,
    ulid: str,
    label: str,
    steps: List[Dict[str, Any]],
    world_path: str,
    failing_step: int,
) -> Dict[str, Any]:
    """Translate a step-7 / step-8 crash into a failure envelope (15b).

    Builds the rendered failure block via ``lib.report_projection_failure``,
    marks the partial as failed at ``5_promote`` in demo-state.json, and
    returns the envelope shape the squirrel surface consumes. The pointer
    commit at step 11 has not yet fired, so the canonical world-root is
    untouched (the docstring on the rendered block calls this out).
    """
    lib = _load_demo_lib()
    summary = (
        f"step {failing_step}: {type(exc).__name__}: {exc}"
    )

    # Best-effort: tease the failing walnut slug out of the Stage5Error
    # message. The step_7 raiser embeds the walnut path verbatim in the
    # message, of the form "step 7: project.py --walnut <abs-path> failed".
    failing_walnut: Optional[str] = None
    msg = str(exc)
    match = re.search(r"--walnut\s+(\S+)", msg)
    if match:
        walnut_path = match.group(1)
        failing_walnut = os.path.basename(os.path.normpath(walnut_path))

    report = lib.report_projection_failure(
        partial_dir=partial_dir,
        exception_summary=summary,
        failing_walnut=failing_walnut,
    )
    return {
        "status": "failed",
        "failure_mode": "projection_failure",
        "rendered_block": report["rendered_block"],
        "state_updated": report.get("state_updated", False),
        "ulid": ulid,
        "label": label,
        "world_path": world_path,
        "steps": steps,
        "failing_step": failing_step,
        "failing_walnut": failing_walnut,
        "summary": summary,
    }


def _state_path_hint() -> str:
    """Best-effort target path for the atomic-write failure block.

    Used when the captured ``OSError`` does not expose ``filename``
    (some Python versions / wrappers strip it). Defaults to the canonical
    demo-state.json path so the human still has something concrete to
    inspect.
    """
    state_mod = _load_demo_state()
    try:
        return state_mod.state_path()
    except Exception:  # noqa: BLE001
        return "~/.config/alive/demo-state.json"


def _maybe_crash(fail_after_step: Optional[int], current: int) -> None:
    """Test seam: raise RuntimeError after the given step number.

    The crash-consistency suite uses ``fail_after_step=N`` to abort the
    transaction immediately after step ``N`` completes (so step ``N+1``
    does not run). Production callers leave ``fail_after_step`` unset.
    """
    if fail_after_step is None:
        return
    if current == fail_after_step:
        raise RuntimeError(
            f"crash-consistency test seam: aborting AFTER step {current} "
            f"(step {current + 1} did not run)"
        )


__all__ = (
    "SCHEMA_VERSION",
    "COMPLETED_TASK_FRACTION",
    "Stage5Error",
    "Stage5NotReady",
    "step_1_pre_check",
    "step_2_promote_partial",
    "step_3_preferences",
    "step_4_squirrel_yamls",
    "step_5_completed_json",
    "step_6_install_entities",
    "step_7_project",
    "step_8_generate_index",
    "step_9_build_log",
    "step_10_stage_demo_state",
    "step_11_commit_world_root",
    "prepare_activation",
    "activate",
)
