"""alive-demo state file (`~/.config/alive/demo-state.json`) — readers + writers.

Single source of truth for the cross-cutting state the `/alive:demo` skill
maintains across stages: the active world (if any), a cached previous
world-root pointer (so deactivate can revert), and a list of in-flight
partial generations.

Path is **canonical** at `~/.config/alive/demo-state.json` — the same
directory as the world-root pointer. Per the codex review, there is NO
`$ALIVE_DEMO_STATE_DIR` env override; tests isolate via
`monkeypatch.setenv("HOME", str(tmp_path))` and the `os.path.expanduser`
calls below resolve under the synthetic home.

The state file co-exists with `~/.config/alive/world-root` (the pointer
multiple readers across the system already hard-code). Both files share a
single sentinel lockfile at `~/.config/alive/.demo-state.lock` —
demo-state.json is rewritten under `flock_file()` to serialize against
concurrent skill invocations from parallel sessions.

## Self-heal contract (Stage 5 crash-consistency)

The activation transaction's only commit point is step 10 (atomic write of
`~/.config/alive/world-root`). Step 9 stages metadata into demo-state.json
*before* step 10 lands. A crash between 9 and 10 leaves demo-state.json
claiming `active_world.path = <new>` while the world-root pointer still
names the previous world.

`load_state()` runs a one-step self-heal on every read: it cross-checks
`demo-state.json[active_world][path]` against `read_world_root_file()`
and, on mismatch, rewrites demo-state.json so `active_world` matches the
pointer (or sets it to `null` if the pointer is non-demo). World-root is
the source of truth; demo-state is a read-through cache that converges to
it on the next load.

This is what allows the 11-step activation transaction to have only one
commit point. Failures at steps 1-10 are detectable + recoverable; step 11
is the atomic flip.

## Schema

```
{
  "schema_version": "0.1",
  "active_world": null | {
    "ulid": "wld_<26-char-lowercase>",
    "label": "<derived-label>",
    "path": "<absolute-path>",
    "activated_at": "<ISO-8601-Z>"
  },
  "previous_world_root": null | "<absolute-path>",
  "partial_generations": [
    {
      "ulid": "wld_<...>",
      "label": "<...>",
      "stage": "0_spine" | "1_anchor" | ... | "5_promote",
      "started_at": "<ISO>",
      "last_updated": "<ISO>",
      "status": "in_progress" | "promoted" | "failed",
      "failed_at_stage": "<stage-key>",  // optional; recovery metadata
      "failed_reason": "<str-or-null>",  // optional
      "failed_at": "<ISO-or-null>",      // optional

      // Custom-path orchestrator metadata (fn-2-2zz.16):
      "size": "small" | "medium" | "large" | null,
      "description_path": "<absolute-path-or-null>",
      "partial_dir": "<absolute-path-or-null>"
    },
    ...
  ]
}
```

The custom-path fields (``size``, ``description_path``, ``partial_dir``)
are explicit-allow optional strings (or null). The validator's
explicit-allow approach silently drops unknown keys today, so anything
not whitelisted gets lost on round-trip; the whitelist is the only
thing that needs extending. **No schema_version bump** -- the new
fields are additive and round-trip safely through ``load_state`` /
``save_state``.

`schema_version` mismatch on load raises `SchemaVersionMismatch`; the
caller (the skill router) renders a bordered-block error directing the
human to `/alive:demo reset`.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional

# ``_common`` is on the package's importable path because the plugin's
# `cli.py` inserts `scripts/` onto `sys.path` at module-import time. We
# re-do that insertion defensively so that direct test imports
# (`from skills.demo import state`) still resolve `_common` and
# `_world_root_io` without going through `cli.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_HERE, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _common import atomic_write_json, flock_file, iso_now  # noqa: E402
from _world_root_io import (  # noqa: E402
    read_world_root_file,
    write_world_root_file,  # re-exported for callers who need the writer
)

__all__ = (
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "LOCK_FILENAME",
    "POINTER_INVALID",
    "POINTER_UNREADABLE",
    "SchemaVersionMismatch",
    "DemoStateError",
    "default_state",
    "state_path",
    "lock_path",
    "load_state",
    "save_state",
    "with_locked_state",
    "self_heal",
    "list_partials",
    "find_partial",
    "upsert_partial",
    "mark_partial_failed",
    "find_resumable_partials",
    "clear_failure",
    "advance_partial_stage",
)

#: Canonical schema version. Bump on every breaking change to the JSON
#: shape; tests that hand-compose state files assert this constant.
SCHEMA_VERSION = "0.1"

#: Filename inside `~/.config/alive/`.
STATE_FILENAME = "demo-state.json"

#: Lockfile name (sentinel — never `os.replace`-d, so flock is meaningful;
#: see `_common.flock_file` docstring on why state files cannot be locked
#: directly).
LOCK_FILENAME = ".demo-state.lock"

#: Stage labels that may appear in `partial_generations[*].stage` /
#: `failed_at_stage`. Stage 1 is in-session UX (no LLM, no validator) so
#: it has no fail-stage entry. Validated on save.
_VALID_STAGES = (
    "0_spine",
    "1_anchor",
    "2_entities",
    "3_timeline",
    "4_insights",
    "5_promote",
)

_VALID_PARTIAL_STATUSES = ("in_progress", "promoted", "failed")


class DemoStateError(RuntimeError):
    """Base error for demo-state IO + schema problems."""


class SchemaVersionMismatch(DemoStateError):
    """Raised by `load_state` when the on-disk `schema_version` doesn't match.

    The caller (skill router) catches this and renders a bordered-block
    error directing the human to `/alive:demo reset`.
    """

    def __init__(self, found: str, expected: str = SCHEMA_VERSION) -> None:
        self.found = found
        self.expected = expected
        super().__init__(
            f"demo-state.json schema_version mismatch: found {found!r}, "
            f"expected {expected!r}. Run `/alive:demo reset` to rebuild."
        )


# ---------------------------------------------------------------------------
# Path helpers (HOME-relative, no env override)
# ---------------------------------------------------------------------------

def _config_dir() -> str:
    """Resolve `~/.config/alive/` under the current `HOME`. No env override."""
    return os.path.expanduser("~/.config/alive")


def state_path() -> str:
    """Absolute path of the canonical demo-state.json file.

    Resolved fresh on every call so test isolation via
    `monkeypatch.setenv("HOME", str(tmp_path))` takes effect immediately.
    """
    return os.path.join(_config_dir(), STATE_FILENAME)


def lock_path() -> str:
    """Absolute path of the demo-state lockfile."""
    return os.path.join(_config_dir(), LOCK_FILENAME)


# ---------------------------------------------------------------------------
# Default state + validation
# ---------------------------------------------------------------------------

def default_state() -> Dict[str, Any]:
    """Return a fresh, empty demo-state dict.

    The shape matches the schema documented at module top. `partial_generations`
    is a fresh list every call so callers can mutate without aliasing.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "active_world": None,
        "previous_world_root": None,
        "partial_generations": [],
    }


def _validate_active_world(active: Any) -> Optional[Dict[str, Any]]:
    """Coerce + validate an `active_world` payload. None → None.

    Required fields: ulid, label, path, activated_at. All strings.
    """
    if active is None:
        return None
    if not isinstance(active, dict):
        raise DemoStateError(f"active_world must be dict or null, got {type(active).__name__}")
    required = ("ulid", "label", "path", "activated_at")
    missing = [k for k in required if k not in active]
    if missing:
        raise DemoStateError(f"active_world missing keys: {missing}")
    for k in required:
        if not isinstance(active[k], str):
            raise DemoStateError(f"active_world.{k} must be str, got {type(active[k]).__name__}")
    return {k: active[k] for k in required}


def _validate_partial(entry: Any) -> Dict[str, Any]:
    """Coerce + validate a single `partial_generations` entry."""
    if not isinstance(entry, dict):
        raise DemoStateError(f"partial_generations entry must be dict, got {type(entry).__name__}")
    required = ("ulid", "label", "stage", "started_at", "last_updated", "status")
    missing = [k for k in required if k not in entry]
    if missing:
        raise DemoStateError(f"partial entry missing keys: {missing}")
    for k in ("ulid", "label", "started_at", "last_updated"):
        if not isinstance(entry[k], str):
            raise DemoStateError(f"partial entry {k} must be str, got {type(entry[k]).__name__}")
    if entry["stage"] not in _VALID_STAGES:
        raise DemoStateError(f"partial entry stage {entry['stage']!r} not in {_VALID_STAGES}")
    if entry["status"] not in _VALID_PARTIAL_STATUSES:
        raise DemoStateError(
            f"partial entry status {entry['status']!r} not in {_VALID_PARTIAL_STATUSES}"
        )
    out = {k: entry[k] for k in required}
    if "failed_at_stage" in entry:
        if entry["failed_at_stage"] is not None and entry["failed_at_stage"] not in _VALID_STAGES:
            raise DemoStateError(
                f"partial entry failed_at_stage {entry['failed_at_stage']!r} "
                f"not in {_VALID_STAGES}"
            )
        out["failed_at_stage"] = entry["failed_at_stage"]
    # Optional failure metadata (fn-2-2zz.13). Recorded alongside
    # `failed_at_stage` so a resume offer can present the failure cause
    # without re-deriving it from logs. Both are pure strings.
    if "failed_reason" in entry:
        if entry["failed_reason"] is not None and not isinstance(entry["failed_reason"], str):
            raise DemoStateError(
                f"partial entry failed_reason must be str or null, "
                f"got {type(entry['failed_reason']).__name__}"
            )
        out["failed_reason"] = entry["failed_reason"]
    if "failed_at" in entry:
        if entry["failed_at"] is not None and not isinstance(entry["failed_at"], str):
            raise DemoStateError(
                f"partial entry failed_at must be ISO str or null, "
                f"got {type(entry['failed_at']).__name__}"
            )
        out["failed_at"] = entry["failed_at"]
    # Custom-path orchestrator metadata (fn-2-2zz.16). Recorded at
    # ``alive demo create prepare`` time so the orchestrator's resume /
    # retry surface can re-find the partial directory + persona text
    # without scanning disk. All three are optional strings (or null);
    # missing keys round-trip cleanly because ``out`` is built from the
    # required-set + explicit-allow whitelist below.
    if "size" in entry:
        if entry["size"] is not None and not isinstance(entry["size"], str):
            raise DemoStateError(
                f"partial entry size must be str or null, "
                f"got {type(entry['size']).__name__}"
            )
        out["size"] = entry["size"]
    if "description_path" in entry:
        if entry["description_path"] is not None and not isinstance(
            entry["description_path"], str
        ):
            raise DemoStateError(
                f"partial entry description_path must be str or null, "
                f"got {type(entry['description_path']).__name__}"
            )
        out["description_path"] = entry["description_path"]
    if "partial_dir" in entry:
        if entry["partial_dir"] is not None and not isinstance(
            entry["partial_dir"], str
        ):
            raise DemoStateError(
                f"partial entry partial_dir must be str or null, "
                f"got {type(entry['partial_dir']).__name__}"
            )
        out["partial_dir"] = entry["partial_dir"]
    return out


def _validate_state(data: Any) -> Dict[str, Any]:
    """Validate top-level shape of a state dict. Returns canonicalized copy."""
    if not isinstance(data, dict):
        raise DemoStateError(f"state must be dict, got {type(data).__name__}")
    if "schema_version" not in data:
        raise DemoStateError("state missing schema_version")
    if data["schema_version"] != SCHEMA_VERSION:
        raise SchemaVersionMismatch(found=data["schema_version"])

    out = default_state()
    out["active_world"] = _validate_active_world(data.get("active_world"))

    prev = data.get("previous_world_root")
    if prev is not None and not isinstance(prev, str):
        raise DemoStateError(
            f"previous_world_root must be str or null, got {type(prev).__name__}"
        )
    out["previous_world_root"] = prev

    partials = data.get("partial_generations", [])
    if not isinstance(partials, list):
        raise DemoStateError(
            f"partial_generations must be list, got {type(partials).__name__}"
        )
    out["partial_generations"] = [_validate_partial(p) for p in partials]
    return out


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def _read_state_file(path: Optional[str] = None) -> Dict[str, Any]:
    """Raw read of demo-state.json. Missing → fresh default. Corrupt → raise.

    Internal helper — callers should use `load_state()` (which adds the
    self-heal step on top of this).
    """
    p = path if path is not None else state_path()
    if not os.path.exists(p):
        return default_state()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoStateError(
            f"demo-state.json at {p} is corrupt or unreadable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return _validate_state(data)


def save_state(state: Dict[str, Any], *, path: Optional[str] = None) -> None:
    """Atomically write `state` to demo-state.json.

    Validates shape before writing — corrupt input fails loud rather than
    leaving a half-baked file on disk. Caller is responsible for holding
    the lock (use `with_locked_state` for the read-modify-write idiom).
    """
    canonical = _validate_state(state)
    target = path if path is not None else state_path()
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    atomic_write_json(target, canonical)


def load_state(*, path: Optional[str] = None) -> Dict[str, Any]:
    """Load demo-state.json, applying the world-root self-heal on mismatch.

    Returns a validated dict matching the schema at module top. Missing file
    yields `default_state()`. Schema-version mismatch raises
    `SchemaVersionMismatch`.

    Self-heal: if `active_world.path` does not match the live
    `read_world_root_file()` value (or the pointer is missing / points at a
    non-demo directory), demo-state.json is rewritten so `active_world` agrees
    with the pointer. World-root is authoritative; demo-state is a cache.
    The rewrite happens under the same flock the caller would take for an
    update.
    """
    target = path if path is not None else state_path()
    state = _read_state_file(target)
    return self_heal(state, persist=True, path=target)


def self_heal(
    state: Dict[str, Any],
    *,
    persist: bool = True,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Reconcile `state.active_world.path` with the live world-root pointer.

    Pointer status (4-way from `_read_pointer_status`):

      * **str (valid path)**       — pointer file exists, parses, validates.
      * **None (absent)**          — pointer file genuinely missing on disk.
      * **POINTER_UNREADABLE**     — pointer file exists but the OS could
                                     not read it (``OSError``: permission,
                                     transient I/O, etc). Non-destructive
                                     — treated like rule 6 (absent). A
                                     permissions blip MUST NOT erase the
                                     cached active demo world.
      * **POINTER_INVALID**        — pointer file exists, was readable,
                                     but the content is unusable (corrupt
                                     body or a well-formed path that no
                                     longer validates). Authoritative —
                                     rule 7 fires (clear cache).

    Rules (applied in order):

      1. If `state["active_world"]` is None and the pointer is absent or
         is invalid or points at a non-demo world, leave state alone.
         demo-state's job is to track demo worlds; non-demo / unusable
         pointers are owned by the rest of the ALIVE resolver stack.
      2. If `state["active_world"]` is None and the pointer points at a
         **demo world** (recognizable by `<world>/.alive/_demo-build-log.md`),
         rebuild `active_world` from the build-log frontmatter. This is the
         primary recovery path after `/alive:demo reset` against a live
         demo world.
      3. If `active_world.path` matches `read_world_root_file()` exactly,
         no change is needed.
      4. If they disagree AND the pointer points at another demo world,
         rebuild `active_world` from THAT world's build log so demo-state
         converges to the source of truth.
      5. If they disagree AND the pointer points at a non-demo world,
         set `active_world = None` — demo-state cannot claim ownership of
         a world the demo skill did not promote.
      6. If the pointer is absent (genuinely missing — no file on disk),
         leave `active_world` alone (the live world is in an indeterminate
         state; some other ALIVE flow may be mid-write — demo-state should
         not pre-empt).
      7. If the pointer is **present but invalid / corrupt**, clear
         `active_world`. World-root is authoritative; a present-but-broken
         pointer means there is no usable live world, and the cached
         demo-state value cannot legitimately claim otherwise. (This is
         the codex-review-driven rule that distinguishes invalid from
         absent — without it, a corrupt `~/.config/alive/world-root`
         would let demo-state silently retain a stale `active_world`.)

    `previous_world_root` is left untouched — it's a cache of the value
    BEFORE the most recent activation, so divergence from the current
    pointer is expected by design.

    When `persist=True` (the default), a rewrite is committed via
    `save_state()` under a fresh `flock_file()` hold. `persist=False`
    is the test seam used by `with_locked_state()` to avoid double-locking.

    Returns the reconciled state dict (whether or not a rewrite happened).
    """
    target = path if path is not None else state_path()

    pointer_status = _read_pointer_status()
    desired_active = state.get("active_world")

    # Reduce the pointer status to a 4-way decision input:
    #   * "absent"      — file genuinely missing (rule 6, non-destructive).
    #   * "unreadable"  — file exists but OSError on read (rule 6, non-destructive).
    #   * "invalid"     — file readable but content unusable (rule 7, destructive).
    #   * dict|None     — valid path; rebuild may yield a demo dict or None
    #                     for non-demo (rules 2/4/5).
    if pointer_status is None:
        pointer_kind = "absent"
        canonical_active: Optional[Dict[str, Any]] = None
    elif pointer_status is POINTER_UNREADABLE:
        pointer_kind = "unreadable"
        canonical_active = None
    elif pointer_status is POINTER_INVALID:
        pointer_kind = "invalid"
        canonical_active = None
    else:
        # Valid path string.
        pointer_kind = "valid"
        canonical_active = _rebuild_active_from_pointer(pointer_status)

    # Decide whether a rewrite is required.
    needs_heal = False
    new_active: Optional[Dict[str, Any]] = desired_active

    if pointer_kind in ("absent", "unreadable"):
        # Rule 1 / Rule 6: pointer absent OR transiently unreadable.
        # Leave state alone — a permissions / I/O blip must NOT erase
        # the cached active demo world.
        needs_heal = False
    elif pointer_kind == "invalid":
        # Rule 7: pointer is present + readable but content is unusable;
        # clear cached active_world so demo-state cannot lie about a
        # live world that no longer exists / never existed cleanly.
        # No-op when active_world is already None.
        if desired_active is not None:
            needs_heal = True
            new_active = None
    elif desired_active is None:
        # pointer_kind == "valid"
        if canonical_active is not None:
            # Rule 2: state had no active world, but pointer names a demo
            # world. Rebuild from the build log.
            needs_heal = True
            new_active = canonical_active
        # else: rule 1 — pointer names a non-demo world; nothing to do.
    else:
        # pointer_kind == "valid", desired_active not None.
        cached_path = desired_active.get("path")
        if cached_path == pointer_status:
            # Rule 3: agree exactly. No change.
            needs_heal = False
        else:
            # Rule 4 / Rule 5: cached differs from pointer.
            needs_heal = True
            new_active = canonical_active  # dict (rule 4) or None (rule 5)

    if not needs_heal:
        return state

    healed = dict(state)
    healed["active_world"] = new_active
    healed["partial_generations"] = list(state.get("partial_generations", []))

    if persist:
        with flock_file(lock_path()):
            save_state(healed, path=target)

    return healed


def _rebuild_active_from_pointer(pointer_value: str) -> Optional[Dict[str, Any]]:
    """Return a fresh `active_world` dict if `pointer_value` is a demo world.

    A directory is recognized as a demo world by the presence of
    ``<world>/.alive/_demo-build-log.md``. The build log carries YAML
    frontmatter with the demo metadata fn-2-2zz.9 writes:

    ```
    ---
    ulid: wld_<26-char-lowercase>
    label: <derived-label>
    activated_at: <ISO-8601-Z>
    ---
    # Build log
    ...
    ```

    This function is the **read** side of that contract. fn-2-2zz.9
    (Stage 5 step 8) is the writer; the contract is locked here so the
    self-heal can recover `active_world` after a /alive:demo reset on a
    live demo world.

    Returns the reconstructed dict on success, or `None` when:
      * the build log is missing (pointer names a non-demo world),
      * the file cannot be read,
      * the YAML frontmatter is missing or malformed,
      * any of the three required keys are absent / not strings.

    Never raises — all failure modes downgrade to None so the self-heal
    rule table can apply rule 5 (clear cache) cleanly.
    """
    build_log = os.path.join(pointer_value, ".alive", "_demo-build-log.md")
    if not os.path.isfile(build_log):
        return None

    try:
        with open(build_log, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None

    fm = _parse_demo_build_log_frontmatter(text)
    if fm is None:
        return None

    required = ("ulid", "label", "activated_at")
    for k in required:
        if k not in fm or not isinstance(fm[k], str) or not fm[k]:
            return None

    return {
        "ulid": fm["ulid"],
        "label": fm["label"],
        "path": pointer_value,
        "activated_at": fm["activated_at"],
    }


def _parse_demo_build_log_frontmatter(text: str) -> Optional[Dict[str, str]]:
    """Parse the leading YAML frontmatter block of `_demo-build-log.md`.

    Narrow hand-rolled parser (no PyYAML dep — the plugin is stdlib-only).
    Recognizes the documented shape:

        ---\\n
        key: value\\n
        ...\\n
        ---\\n
        <body...>

    Lines may be blank or `# ...` comments. Values are taken verbatim
    after the `:`-and-space; quoting is NOT stripped (fn-2-2zz.9 writes
    plain unquoted scalars per the locked contract). Keys outside the
    documented set are preserved in the dict but unused by the self-heal.

    Returns `None` for any of: missing opening delimiter, missing closing
    delimiter, malformed line.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    # Normalize CRLF to LF for the closing-delimiter scan.
    body = text.replace("\r\n", "\n")
    if not body.startswith("---\n"):
        return None
    rest = body[4:]
    # Find the closing `---` on its own line.
    closing = rest.find("\n---\n")
    if closing < 0:
        # Tolerate a final `---\n` at end-of-file (no body).
        if rest.rstrip("\n").endswith("---"):
            closing = rest.rfind("\n---")
            if closing < 0:
                return None
        else:
            return None
    fm_block = rest[:closing]

    out: Dict[str, str] = {}
    for raw_line in fm_block.split("\n"):
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        sep = line.find(":")
        if sep < 0:
            return None
        key = line[:sep].strip()
        value = line[sep + 1:].strip()
        if not key:
            return None
        # Strip surrounding quotes if present (defensive — fn-2-2zz.9
        # writes plain scalars but a hand-edit might add them).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key] = value
    return out


#: Sentinel returned by `_read_pointer_status` for pointer files that exist
#: AND can be read AND have parsed-but-invalid content (corrupt body, or a
#: well-formed path that doesn't validate as a world root). Distinct from
#: ``None`` (absent) so the self-heal can apply rule 7 (clear cache).
POINTER_INVALID = object()

#: Sentinel returned by `_read_pointer_status` for pointer files that
#: exist but the read itself failed (OSError — permissions, transient I/O,
#: filesystem hiccup). DISTINCT from POINTER_INVALID so the self-heal can
#: leave demo-state alone rather than destructively clearing
#: `active_world` on a permissions blip. Treated like rule 6 (absent).
POINTER_UNREADABLE = object()


def _read_pointer_status():
    """4-way read of `~/.config/alive/world-root`.

    Returns:
        * ``str``  — absolute path, file exists + parses + validates as a
          world root.
        * ``None`` — file is genuinely absent (no file on disk).
        * ``POINTER_UNREADABLE`` — file exists but the OS could not read
          it (``OSError``: permission denied, transient I/O, etc).
          Treated by the self-heal as **non-destructive** — leave
          ``active_world`` alone (rule 6). A permissions blip must NEVER
          erase cached demo-state.
        * ``POINTER_INVALID`` — file exists, was readable, but has
          unusable content (corrupt body — multi-line, empty after
          strip, non-absolute, contains forbidden chars — or a
          well-formed path that no longer validates as a world root).
          Treated by the self-heal as **destructive** — clear
          ``active_world`` (rule 7). World-root is the source of truth;
          a parsed-but-invalid pointer is authoritative.

    The split between UNREADABLE (transient, non-destructive) and
    INVALID (parsed, destructive) is what distinguishes "the OS hiccupped"
    from "the file content is genuinely wrong." Without it, a
    `chmod 000 ~/.config/alive/world-root` would silently erase the
    cached active demo world on the next ``alive demo status``.

    The branches are pure stat / open / lexical-parse — no symlink
    resolution, no shelling out. Implements the same path conventions
    as ``_world_root_io`` (canonical config-path expansion).
    """
    import errno as _errno  # noqa: PLC0415 (locality)
    import stat as _stat  # noqa: PLC0415 (locality)

    config_path = os.path.expanduser("~/.config/alive/world-root")
    legacy_path = os.path.expanduser("~/.config/walnut/world-root")

    # Step 0: legacy-pointer probe. Per `_world_root_io.read_world_root_file`,
    # when the canonical alive pointer is missing the helper falls back to
    # the legacy walnut pointer and migrates it on the next read. We must
    # honor that contract: if the alive pointer is genuinely absent BUT the
    # legacy pointer is present, route the entire decision through the
    # canonical reader so migration runs and we surface the legacy pointer's
    # status, not a premature "absent".
    try:
        alive_exists = os.path.lexists(config_path)
    except OSError:
        # An OSError on `lexists` itself is exotic (errno would be EIO /
        # similar on the parent dir); treat as transient.
        return POINTER_UNREADABLE

    if not alive_exists:
        try:
            legacy_exists = os.path.lexists(legacy_path)
        except OSError:
            legacy_exists = False
        if legacy_exists:
            # Defer entirely to the canonical reader so legacy migration
            # runs. ValueError = corrupt content; None = stale path; str =
            # successfully migrated + validated.
            try:
                result = read_world_root_file()
            except ValueError:
                return POINTER_INVALID
            if result is None:
                return POINTER_INVALID
            return str(result)
        return None

    # Step 1: lstat. Distinguish absent / structurally-invalid / transient.
    try:
        st = os.lstat(config_path)
    except FileNotFoundError:
        # Race: file vanished between lexists and lstat. Treat as absent.
        return None
    except PermissionError:
        # Stat itself denied (rare — usually a directory-permission issue
        # on the parent). Truly transient from demo-state's POV.
        return POINTER_UNREADABLE
    except OSError as exc:
        # Other lstat failures: classify by errno. ENOENT was caught
        # above (FileNotFoundError); ENOTDIR / ELOOP indicate structural
        # problems with the parent path or the symlink chain — both are
        # parsed-but-invalid (rule 7) rather than transient.
        if exc.errno in (_errno.ENOTDIR, _errno.ELOOP):
            return POINTER_INVALID
        # Anything else (EIO, ENXIO, ...) is a genuine transient.
        return POINTER_UNREADABLE

    mode = st.st_mode

    # Step 2a: directory-at-path is structurally invalid. Without this
    # check, the open() below would raise IsADirectoryError, which we'd
    # otherwise route through the OSError unreadable path.
    if _stat.S_ISDIR(mode):
        return POINTER_INVALID

    # Step 2b: symlink — verify the target resolves to an existing file.
    # A broken symlink (target missing) is structurally invalid; a
    # symlink to a directory (or another symlink chain that resolves to
    # a directory) is also invalid. Resolution failures other than
    # "target missing" / "target is dir" are treated as transient.
    if _stat.S_ISLNK(mode):
        try:
            target_st = os.stat(config_path)  # follows the link chain
        except FileNotFoundError:
            return POINTER_INVALID  # broken symlink
        except OSError as exc:
            if exc.errno == _errno.ELOOP:
                return POINTER_INVALID  # symlink loop
            if exc.errno == _errno.ENOTDIR:
                return POINTER_INVALID  # path component clobbered
            return POINTER_UNREADABLE  # other transients
        if _stat.S_ISDIR(target_st.st_mode):
            return POINTER_INVALID
        if not _stat.S_ISREG(target_st.st_mode):
            # Symlink to a special file (FIFO, device, ...) is invalid.
            return POINTER_INVALID
    elif not _stat.S_ISREG(mode):
        # Plain file expected; FIFO / device / socket at the config path
        # is structurally invalid.
        return POINTER_INVALID

    # Step 3: read the (regular file or regular-file symlink target).
    # OSError from open is the genuinely-transient unreadable case
    # (PermissionError, EIO, ...). UnicodeDecodeError is structural-invalid.
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except (PermissionError, IsADirectoryError):
        # PermissionError is transient; IsADirectoryError shouldn't
        # reach here given the S_ISDIR check above, but be defensive.
        return POINTER_UNREADABLE
    except OSError as exc:
        if exc.errno in (_errno.ENOTDIR, _errno.ELOOP):
            return POINTER_INVALID
        return POINTER_UNREADABLE
    except UnicodeDecodeError:
        return POINTER_INVALID

    if not raw.strip():
        return POINTER_INVALID

    # Step 4: defer to the canonical helper for full content validation.
    # `read_world_root_file` raises ValueError on corrupt content
    # (multi-line, ascend-past-root, ~user, etc.) or returns None when
    # the parsed path no longer validates as a world root. Both are
    # "parsed-but-invalid" by definition.
    try:
        result = read_world_root_file()
    except ValueError:
        return POINTER_INVALID
    if result is None:
        return POINTER_INVALID
    return str(result)


def _read_pointer_safe() -> Optional[str]:
    """Backwards-compat shim — returns ``str`` or ``None``.

    Folds ``POINTER_INVALID`` back into ``None`` for callers that don't
    care about the absent-vs-invalid distinction. New code should use
    ``_read_pointer_status`` directly.
    """
    status = _read_pointer_status()
    if status is POINTER_INVALID:
        return None
    return status


# ---------------------------------------------------------------------------
# Read-modify-write idiom
# ---------------------------------------------------------------------------

class _LockedStateContext:
    """Context manager yielded by `with_locked_state()`.

    On entry: acquires the demo-state lock, runs `load_state()` (which
    includes the self-heal pass), and yields the loaded state.

    On exit: writes the (possibly-mutated-by-the-caller) state back via
    `save_state()` IF the caller did not raise. On exception, the lock
    is released without writing.

    The yielded dict is a fresh copy — mutating it in place is the
    intended idiom; the rewrite on exit picks up those mutations.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path if path is not None else state_path()
        self._lock_ctx = None
        self._state: Optional[Dict[str, Any]] = None

    def __enter__(self) -> Dict[str, Any]:
        self._lock_ctx = flock_file(lock_path())
        self._lock_ctx.__enter__()
        try:
            # Read with self-heal but do NOT let `load_state` itself
            # try to acquire the lock again — it would deadlock under
            # this same flock. Pass `persist=False` and persist on exit.
            raw = _read_state_file(self._path)
            self._state = self_heal(raw, persist=False, path=self._path)
            return self._state
        except Exception:
            self._lock_ctx.__exit__(None, None, None)
            self._lock_ctx = None
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and self._state is not None:
                save_state(self._state, path=self._path)
        finally:
            if self._lock_ctx is not None:
                self._lock_ctx.__exit__(exc_type, exc, tb)
                self._lock_ctx = None


def with_locked_state(*, path: Optional[str] = None) -> _LockedStateContext:
    """Lock + load + (on clean exit) save the demo-state file.

    Use as the canonical read-modify-write entry point::

        with with_locked_state() as state:
            state["partial_generations"].append({...})
            # state auto-saved on clean __exit__

    Acquires the same `~/.config/alive/.demo-state.lock` sentinel that
    activation Stage 5 step 9 takes; multiple parallel `/alive:demo`
    sessions therefore serialize cleanly on this single lockfile.
    """
    return _LockedStateContext(path=path)


# ---------------------------------------------------------------------------
# Convenience accessors (read-only — internal use)
# ---------------------------------------------------------------------------

def list_partials(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return a copy of `state['partial_generations']`."""
    return list(state.get("partial_generations", []))


def find_partial(state: Dict[str, Any], ulid: str) -> Optional[Dict[str, Any]]:
    """Return the partial-generation entry matching `ulid`, or None."""
    for entry in state.get("partial_generations", []):
        if entry.get("ulid") == ulid:
            return entry
    return None


def upsert_partial(
    partials: List[Dict[str, Any]], entry: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """In-place update-or-append by ulid. Returns the same list for chaining."""
    canonical = _validate_partial({**entry, "last_updated": entry.get("last_updated") or iso_now()})
    for i, existing in enumerate(partials):
        if existing.get("ulid") == canonical["ulid"]:
            partials[i] = canonical
            return partials
    partials.append(canonical)
    return partials


# ---------------------------------------------------------------------------
# Failure-mode mutators (fn-2-2zz.13)
# ---------------------------------------------------------------------------

def mark_partial_failed(
    partial_ulid: str,
    *,
    stage_id: str,
    reason: str,
    state_path: Optional[str] = None,
) -> None:
    """Atomically mark a partial-generation entry as failed at ``stage_id``.

    Operates under the demo-state flock so a parallel ``alive demo`` run
    cannot race the marker. Idempotent: marking the same partial with the
    same ``stage_id`` + ``reason`` twice is a no-op for the on-disk
    content (last_updated is refreshed, but no other field changes).

    Args:
        partial_ulid: The ``wld_<ulid>`` identifying the entry under
            ``partial_generations``. No-op (silently) when no entry
            matches; the caller may have skipped staging the partial row
            (e.g. failures very early in the pipeline).
        stage_id: Canonical state label ("0_spine", ..., "5_promote").
            Validated by ``_validate_partial`` before write.
        reason: Free-text label for the failure cause. Convention:
            ``"validation_double_failure"``, ``"projection_failure"``.
            Surfaced verbatim by ``alive demo resume``.
        state_path: Test seam; production callers leave None.

    The function does NOT change the entry's ``status`` field. The schema's
    status enum (``in_progress | promoted | failed``) is reserved for the
    activation lifecycle; ``failed_at_stage`` + ``failed_reason`` are the
    failure-mode marker pair so a resumable partial can stay
    ``in_progress`` while flagged for retry.
    """
    if not partial_ulid:
        return

    with with_locked_state(path=state_path) as state:
        partials = state.get("partial_generations") or []
        for entry in partials:
            if entry.get("ulid") != partial_ulid:
                continue
            entry["failed_at_stage"] = stage_id
            entry["failed_reason"] = reason
            entry["failed_at"] = iso_now()
            entry["last_updated"] = entry["failed_at"]
            break
        # If the entry isn't found, we leave state untouched. The locked
        # context still re-saves on clean exit, which is a no-op for an
        # unchanged dict (atomic_write_json is content-addressed enough
        # for this -- it always rewrites, but the bytes match).
        state["partial_generations"] = partials


def find_resumable_partials(
    state_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List partial generations that can be resumed from a known failed stage.

    A partial is resumable when its ``status`` is ``in_progress`` AND it
    carries a ``failed_at_stage`` marker. ``status == promoted`` partials
    are already activated; ``status == failed`` partials have been
    explicitly written off (no resume).

    Returns the entries newest-first by ``last_updated`` so the resume
    picker surfaces the most recent failure first.
    """
    raw = _read_state_file(state_path if state_path is not None else None)
    state = self_heal(raw, persist=False, path=state_path)
    out: List[Dict[str, Any]] = []
    for entry in state.get("partial_generations", []):
        if entry.get("status") != "in_progress":
            continue
        if not entry.get("failed_at_stage"):
            continue
        out.append(dict(entry))
    out.sort(key=lambda e: e.get("last_updated") or "", reverse=True)
    return out


def clear_failure(
    partial_ulid: str,
    *,
    state_path: Optional[str] = None,
) -> None:
    """Clear ``failed_at_stage`` (and metadata) on a partial.

    Used by ``alive demo resume`` once the user has chosen to retry: the
    failure markers must be cleared so a subsequent failure surface
    reports fresh state rather than the previous run's leftovers.
    Idempotent on partials that have no failure markers.

    The ``last_updated`` timestamp is bumped so the resume picker
    re-orders correctly on subsequent listings.
    """
    if not partial_ulid:
        return

    with with_locked_state(path=state_path) as state:
        partials = state.get("partial_generations") or []
        for entry in partials:
            if entry.get("ulid") != partial_ulid:
                continue
            changed = False
            for k in ("failed_at_stage", "failed_reason", "failed_at"):
                if k in entry:
                    entry.pop(k, None)
                    changed = True
            if changed:
                entry["last_updated"] = iso_now()
            break
        state["partial_generations"] = partials


# ---------------------------------------------------------------------------
# Stage-progress mutator (fn-2-2zz.16 custom-path orchestrator)
# ---------------------------------------------------------------------------

def advance_partial_stage(
    partial_dir: str,
    new_stage: str,
    *,
    state_path: Optional[str] = None,
) -> Optional[str]:
    """Atomically advance a partial-generation row's ``stage`` field.

    Called from each successful per-stage freeze helper
    (``stage1.freeze_anchors`` after Stage 0 success ->
    ``"1_anchor"``; ``stage2.freeze_stage`` -> ``"2_entities"``,
    ``"3_timeline"``, ...) so that ``alive demo status`` /
    ``alive demo resume`` reflect the actual in-flight stage on disk.

    The partial's ULID is derived from the ``partial_dir`` basename
    (matches the ``wld_<ulid>.partial`` convention). If the basename
    does not parse as a partial, OR if no row in
    ``partial_generations`` matches the derived ULID, this is a no-op
    -- legacy partials created before fn-2-2zz.16 (or partials that
    were never registered with demo-state, e.g. test fixtures) do not
    have a row to advance.

    Args:
        partial_dir: absolute path of the partial directory (the same
            value the freeze helper was given).
        new_stage: the NEXT stage label. Validated against
            ``_VALID_STAGES``; an unknown value raises
            ``DemoStateError``. Convention: pass the in-flight stage
            after the freeze (e.g. Stage 0 success -> ``"1_anchor"``,
            Stage 1 success -> ``"2_entities"``, ..., Stage 4 success
            -> ``"5_promote"``).
        state_path: test seam; production callers leave None.

    Returns:
        The derived ``wld_<ulid>`` if a row was found and advanced,
        else ``None``.

    The mutation is idempotent: advancing to the same stage twice is a
    no-op for the on-disk state-file content (the row's
    ``last_updated`` timestamp is refreshed but no other field
    changes).
    """
    if new_stage not in _VALID_STAGES:
        raise DemoStateError(
            f"new_stage {new_stage!r} not in {_VALID_STAGES}"
        )

    if not isinstance(partial_dir, str) or not partial_dir:
        return None
    base = os.path.basename(os.path.normpath(partial_dir))
    if base.endswith(".partial"):
        base = base[: -len(".partial")]
    if not base.startswith("wld_") or len(base) <= 4:
        return None
    partial_ulid = base

    with with_locked_state(path=state_path) as state:
        partials = state.get("partial_generations") or []
        matched: Optional[str] = None
        for entry in partials:
            if entry.get("ulid") != partial_ulid:
                continue
            entry["stage"] = new_stage
            entry["last_updated"] = iso_now()
            # Clear any stale failure markers from a previous attempt.
            # A successful stage advance is the documented signal that
            # the partial is no longer in a "needs resume" state for
            # the previously-failed stage. Mirrors the contract in
            # README.md: "the per-stage freeze step clears the failure
            # marker on success." Without this, ``alive demo resume``
            # would still surface the partial because the resume
            # filter only checks ``status == "in_progress"`` plus a
            # non-empty ``failed_at_stage``.
            for k in ("failed_at_stage", "failed_reason", "failed_at"):
                entry.pop(k, None)
            matched = partial_ulid
            break
        state["partial_generations"] = partials
    return matched
