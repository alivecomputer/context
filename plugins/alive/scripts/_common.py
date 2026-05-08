"""ALIVE Context System -- shared Python helpers.

Single source of truth for the small set of utilities that every agent-facing
script (tasks.py, project.py, generate-index.py, future alive-CLI subcommands)
needs. Stdlib-only. Python 3.14.x target.

Exports:
  atomic_write_text(path, content)  -- text files (log.md, _index.yaml, ...)
  atomic_write_json(path, obj)      -- JSON files (tasks.json, now.json, ...)
  _read_json(path, key, strict=True) -- parse JSON, defaulting on missing
  find_all_walnuts(world_root)      -- walk ALIVE domain dirs for walnuts
  find_world_root(walnut_path)      -- port of hooks/scripts/alive-common.sh:find_world
  find_world_root_with_strategy(walnut_path) -- same, returns (root, strategy_label)
                                       on success; on failure returns
                                       (None, reason) where reason is one of
                                       the WORLD_ROOT_FAIL_REASON taxonomy
                                       constants ("not_found", "stale_config",
                                       "invalid_override", "denied_home").
  resolve_plugin_root(override=None) -- override -> ALIVE_PLUGIN_ROOT -> auto-discover
  iso_now()                         -- UTC ISO 8601 timestamp
  resolve_session_id(override=None) -- override -> ALIVE_SESSION_ID -> CLAUDE_SESSION_ID -> synthesized
  squirrel_short_id(session_id)     -- session_id[:8]

Design notes:
  - _read_json default is strict=True to match existing tasks.py callers;
    quiet fallbacks must opt in via strict=False.
  - find_world_root_with_strategy mirrors the bash sibling
    ``alive-common.sh::find_world`` (see fn-15-la5.4 / fn-15-la5.5). Tier
    order is locked at: env override -> config file (with legacy walnut
    migration) -> bootstrap cwd walk-up -> fail. ``$ALIVE_WORLD_ROOT`` is
    NOT consumed; the env-var tier was retired in fn-15-la5. Cowork
    mount-scan remains bash-only by design (see comment in
    :func:`find_world_root_with_strategy`).
  - Synthesized session IDs guarantee 8 leading hex chars so that
    `session_id[:8]` (squirrel_short_id) always yields the conventional
    squirrel label consumed by alive-context-watch.sh and project.py.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Vendored package bootstrap
# ---------------------------------------------------------------------------
#
# `_common` is imported by every agent-facing script in this plugin (tasks.py,
# project.py, generate-index.py, cli.py, log.py, promote.py, doctor.py, ...).
# Importing it has the side effect of putting the plugin's `_vendor/` directory
# on sys.path so callers can `from ulid import ULID` without any per-script
# wiring. Vendoring policy + provenance live in `_vendor/README.md`.
#
# Stdlib-only contract is preserved: only pure-Python, zero-transitive-dep
# packages may be vendored here. Today the only entry is `python-ulid 3.1.0`
# (MIT, mdomke); see `_vendor/README.md`.
#
# The insert is idempotent: re-importing `_common` does not stack duplicate
# entries because we check membership first.
_VENDOR_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "_vendor")
)
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def _default_file_mode():
    """Return the mode a plain `open(path, 'w')` would leave on disk."""
    # Match the effective permissions of a normal open-for-write: 0o666
    # masked by the process umask. getumask requires a read-set dance
    # since there's no getter in older Python.
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _atomic_write_bytes(path, data):
    """Write `data` (bytes) to `path` atomically.

    Uses tempfile.mkstemp in the *parent directory* (guarantees same
    filesystem for os.replace) with a unique suffix. Two concurrent
    writers therefore never touch the same temp file -- the fixed
    `path + ".tmp"` pattern would have let writer A's partial stream leak
    into writer B's replace, which is exactly the corruption fn-12 wants
    to close.

    Preserves the target's existing file mode on overwrite; for new files
    uses the default umask-adjusted mode (matches `open(path, "w")` so
    tasks.json / now.json / _index.* keep their usual 0o644-ish permissions
    instead of mkstemp's 0o600 default).

    Creates parent directories as needed.
    """
    path = os.fspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    # mkstemp returns an open fd we must close ourselves. It creates the
    # file with mode 0o600; we adjust to match either the existing target
    # or the umask-derived default before the os.replace swaps it in.
    fd, tmp = tempfile.mkstemp(
        dir=parent,
        prefix="." + os.path.basename(path) + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            target_mode = os.stat(path).st_mode & 0o777
        except FileNotFoundError:
            target_mode = _default_file_mode()
        os.chmod(tmp, target_mode)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup on failure -- os.replace may or may not have
        # consumed tmp. Ignore errors on the cleanup path itself.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, content):
    """Write text to path atomically.

    UTF-8 encoded. Creates parent directories as needed. Caller is
    responsible for trailing newlines -- this function writes content
    verbatim. Concurrent-writer safe (see _atomic_write_bytes).
    """
    _atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_json(path, obj):
    """Write JSON to path atomically.

    Formatting matches the fn-12-a7w T1 acceptance spec: indent=2,
    sort_keys=True, ensure_ascii=False, trailing newline. Sorted keys
    mean existing tasks.json / now.json / _index.json files rewrite in
    alphabetical key order on their next touch (cosmetic diff, no
    behaviour change -- the agent reads via json.load into a dict and
    looks up by key name, not by position). Concurrent-writer safe.
    """
    body = (
        json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )
    _atomic_write_bytes(path, body.encode("utf-8"))


# ---------------------------------------------------------------------------
# JSON reader
# ---------------------------------------------------------------------------

def _read_json(path, key, strict=True):
    """Read a JSON file. Return {key: []} if missing.

    If strict (default), exits on any read/parse failure; if strict=False,
    prints a warning to stderr and returns None so callers can skip
    gracefully. Treats all expected failure modes the same way: malformed
    JSON, broken UTF-8, and OS errors (permission denied, transient I/O
    on network FS, etc.). Bare `open`/`json.load` would let OSError or
    UnicodeDecodeError escape as a traceback, which is the wrong failure
    shape for a shared "safe read" helper.
    """
    path = os.fspath(path)
    if not os.path.exists(path):
        return {key: []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "malformed or unreadable {} ({}: {})".format(
            path, type(exc).__name__, exc
        )
        if strict:
            print("Error: " + msg, file=sys.stderr)
            sys.exit(1)
        print("Warning: skipping " + msg, file=sys.stderr)
        return None

    if not isinstance(data, dict) or key not in data:
        if strict:
            print("Error: malformed {}".format(path), file=sys.stderr)
            sys.exit(1)
        print("Warning: skipping malformed {}".format(path), file=sys.stderr)
        return None
    return data


# ---------------------------------------------------------------------------
# Walnut / world discovery
# ---------------------------------------------------------------------------

_DOMAIN_DIRS = ("01_Archive", "02_Life", "04_Ventures", "05_Experiments")

# Directories we never descend into when scanning for walnuts.
# 04_Ventures/ in real worlds frequently hosts non-walnut code repos
# (node_modules, dist, build, __pycache__, etc.); without this skip-set the
# tasks.py --world scan was pathologically slow. Names align with
# scripts/walnut_paths.py and scripts/tasks.py:_all_task_files so all three
# scanners agree on what "not a walnut directory" means.
_WALNUT_SCAN_SKIP_DIRS = frozenset({
    ".git", ".next", ".venv",
    "__pycache__", "build", "dist", "node_modules", "raw",
    "target", "venv",
})

#: Directory-name prefixes marking a point-in-time walnut snapshot
#: rather than a live walnut tree. Held in lockstep with
#: ``system_upgrade.version_detect._ARCHIVED_SNAPSHOT_PREFIXES`` so
#: every walnut-iterating scanner agrees on what is and isn't a walnut.
_ARCHIVED_SNAPSHOT_PREFIXES = ("walnut-duplicates-",)


def _is_archived_snapshot_dir(name):
    """Return True iff *name* is a point-in-time walnut backup directory."""
    return any(name.startswith(p) for p in _ARCHIVED_SNAPSHOT_PREFIXES)


def find_all_walnuts(world_root):
    """Return sorted list of walnut directories under world_root.

    A walnut is any directory containing _kernel/key.md. Scans the canonical
    ALIVE domain directories only (01_Archive, 02_Life, 04_Ventures,
    05_Experiments). Stops at nested walnut boundaries and skips the
    standard non-content dir set (see _WALNUT_SCAN_SKIP_DIRS) so that code
    repos living inside ventures never explode the scan. Archived-snapshot
    directories (``walnut-duplicates-*``) are pruned at traversal time so
    backup copies of walnuts the operator already moved aside do NOT
    re-register as live walnuts.
    """
    world_root = os.fspath(world_root)
    walnuts = []
    for domain in _DOMAIN_DIRS:
        domain_path = os.path.join(world_root, domain)
        if not os.path.isdir(domain_path):
            continue
        for root, dirs, _files in os.walk(domain_path):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in _WALNUT_SCAN_SKIP_DIRS
                and not _is_archived_snapshot_dir(d)
            ]
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                walnuts.append(root)
                dirs[:] = []  # do not descend into nested walnuts
    return sorted(walnuts)


def _looks_like_world_root(path):
    """Legacy any-1-of-4 predicate. PRESERVED FOR BACKWARDS COMPAT ONLY.

    The fn-15-la5 resolver delegates to
    ``_world_root_io.is_valid_world_root`` (which requires .alive/ OR >= 2
    domain children, and runs unmount detection before any stat). This
    helper is retained because external scripts may still import it; new
    code MUST use ``is_valid_world_root`` instead.
    """
    if os.path.isdir(os.path.join(path, ".alive")):
        return True
    for domain in _DOMAIN_DIRS:
        if os.path.isdir(os.path.join(path, domain)):
            return True
    return False


#: Labels for the world-root resolution strategies. Kept as string
#: constants so that ``doctor --check=world-root`` can surface the matched
#: strategy to the agent without any of the callers leaking implementation
#: details (e.g. what ".alive/" literally looks like on disk).
#:
#: fn-15-la5 (T4 + T5) collapsed the four-tier scheme into three live
#: strategies that mirror the bash sibling ``find_world``:
#:   * OVERRIDE     -- ``$ALIVE_WORLD_ROOT_OVERRIDE`` env var won
#:   * CONFIG_FILE  -- ``~/.config/alive/world-root`` (or legacy walnut
#:                     path with one-time migration) won
#:   * BOOTSTRAP    -- cwd walk-up via ``is_valid_world_root`` won;
#:                     collapses the old ALIVE_MARKER and DOMAIN_DIRS
#:                     since the predicate is now unified.
#:
#: ``WORLD_ROOT_STRATEGY_ENV_VAR`` is retained for one release as a
#: deprecation breadcrumb -- no code path returns it any more, but
#: external JSON consumers that pattern-matched on the old kebab-case
#: id will see the constant disappear in a future release.
#: ``WORLD_ROOT_STRATEGY_ALIVE_MARKER`` and
#: ``WORLD_ROOT_STRATEGY_DOMAIN_DIRS`` are likewise retained as
#: deprecated breadcrumbs (the resolver no longer distinguishes the
#: two; both have been folded into ``BOOTSTRAP``).
WORLD_ROOT_STRATEGY_OVERRIDE = "override"
WORLD_ROOT_STRATEGY_CONFIG_FILE = "config-file"
WORLD_ROOT_STRATEGY_BOOTSTRAP = "bootstrap"
# Deprecated; retained so existing callers + JSON consumers see the
# rename rather than a hard import-error during the transition window.
WORLD_ROOT_STRATEGY_ALIVE_MARKER = "alive-marker"
WORLD_ROOT_STRATEGY_DOMAIN_DIRS = "domain-dirs"
WORLD_ROOT_STRATEGY_ENV_VAR = "env-var"

#: Canonical mapping from RETIRED strategy ids to the live strategy
#: that subsumes them. Consumers that still see an old id (loaded
#: from a stale ``alive doctor`` JSON dump, an external script that
#: hardcoded the kebab-case name, etc.) MUST run it through this map
#: before labeling or serializing -- otherwise the surface text reads
#: as if ``$ALIVE_WORLD_ROOT`` (or the old two-strategy walk-up) is
#: still a live tier.
#:
#: The map is one-shot: a single pass canonicalizes every retired id
#: to a live one. ``doctor.py`` and any future ``alive schema``
#: consumer are expected to apply ``canonical_strategy(...)`` (defined
#: below) before lookup against ``_STRATEGY_LABELS``.
WORLD_ROOT_STRATEGY_DEPRECATION_MAP = {
    WORLD_ROOT_STRATEGY_ENV_VAR: WORLD_ROOT_STRATEGY_OVERRIDE,
    WORLD_ROOT_STRATEGY_ALIVE_MARKER: WORLD_ROOT_STRATEGY_BOOTSTRAP,
    WORLD_ROOT_STRATEGY_DOMAIN_DIRS: WORLD_ROOT_STRATEGY_BOOTSTRAP,
}


def canonical_strategy(strategy):
    """Map a (possibly deprecated) strategy id to its live equivalent.

    Returns the input unchanged when it is already a live id (or
    unknown). Idempotent. Use this at every label / serialization
    boundary so retired ids never reach external surface text.
    """
    return WORLD_ROOT_STRATEGY_DEPRECATION_MAP.get(strategy, strategy)


#: Resolver-failure taxonomy. Mirrors the bash sibling's
#: ``WORLD_ROOT_FAIL_REASON`` exports (see ``alive-common.sh::find_world``)
#: so cross-impl tests can assert identical reasons for identical failure
#: conditions.
WORLD_ROOT_FAIL_NOT_FOUND = "not_found"
WORLD_ROOT_FAIL_STALE_CONFIG = "stale_config"
WORLD_ROOT_FAIL_INVALID_OVERRIDE = "invalid_override"
WORLD_ROOT_FAIL_DENIED_HOME = "denied_home"


def _normalize_lexically(raw):
    """Lexically normalize a path. Pure string op (no fs touches).

    Wraps ``_world_root_io.lexical_normalize_path`` so callers get the
    same expanduser + abspath + normpath pipeline the rest of fn-15-la5
    runs on. Returns ``None`` on rejection rather than raising so the
    resolver can keep walking siblings without try/except scaffolding.

    NEVER calls ``os.path.realpath``: symlink resolution is the
    consumer's choice at point of use, per the locked
    "lexical-only paths" contract.
    """
    try:
        from _world_root_io import lexical_normalize_path  # noqa: PLC0415
        return lexical_normalize_path(raw)
    except (TypeError, ValueError):
        return None


#: Advisory reason name for the cwd-vs-config divergence channel.
#: Locked in lockstep with the bash sibling
#: (``alive-common.sh::_alive_detect_cwd_config_divergence``) so cross-impl
#: tests can assert byte-equal advisory state.
WORLD_ROOT_ADVISORY_CWD_CONFIG_DIVERGENCE = "cwd_config_divergence"


def _surface_gate_permits_divergence_check():
    """Return True iff the surface gate currently permits the cwd walk-up.

    Locked gate (Bash + Python parity): runs only when
    ``$CLAUDE_CODE_HOOK_EVENT=SessionStart`` OR
    ``$ALIVE_RESOLVER_DIVERGENCE_CHECK=1``. Default off so non-Claude-Code
    surfaces (alive-mcp, Hermes, Codex, future native clients) pay zero
    overhead and never see the advisory. ``SessionResume`` is intentionally
    NOT included -- resume continues against the already-loaded world and
    surfacing divergence mid-flight has no actionable meaning.
    """
    if os.environ.get("CLAUDE_CODE_HOOK_EVENT") == "SessionStart":
        return True
    if os.environ.get("ALIVE_RESOLVER_DIVERGENCE_CHECK") == "1":
        return True
    return False


def _detect_cwd_config_divergence(config_world, walnut_path):
    """Set the divergence advisory env channel when cwd != config-world.

    Called from ``find_world_root_with_strategy`` AFTER a successful
    tier-2 (config-file) resolve. Walks ``walnut_path`` (or cwd) up via
    ``is_valid_world_root`` looking for a valid world; if found AND it
    differs from ``config_world`` AND ``validate_path_choice`` does not
    return ``deny`` for it, sets:

        os.environ["WORLD_ROOT_ADVISORY_REASON"] = "cwd_config_divergence"
        os.environ["WORLD_ROOT_DIVERGENT_CWD_PATH"] = <cwd-resolved world>

    Skip-conditions (no advisory):
      * Surface gate closed (default for non-CC callers).
      * cwd walk-up finds nothing valid.
      * cwd-resolved == config-resolved.
      * cwd-resolved is ``deny`` (system roots, ``/private/var/folders``).
        ``confirm_required`` (home / cloud) DOES surface the advisory; the
        user can decide via doctor with ``--allow-home`` / ``--allow-cloud``
        at fix time.

    Best-effort: any exception is swallowed (no advisory). Never raises.
    """
    if not _surface_gate_permits_divergence_check():
        return

    # Local imports to keep the cold-start cost on the success path bounded
    # to the actual divergence-check work.
    try:
        from _world_root_io import (  # noqa: PLC0415
            is_valid_world_root,
            validate_path_choice,
        )
    except ImportError:
        return

    raw_start = os.fspath(walnut_path) if walnut_path else os.getcwd()
    start = _normalize_lexically(raw_start)
    if start is None:
        # Unnormalizable input: same defensive default as tier-3 below.
        start = os.path.abspath(os.path.expanduser(raw_start))

    cwd_world = None
    cwd_decision = None
    current = start
    while current and current != "/":
        candidate = _normalize_lexically(current) or current
        try:
            if is_valid_world_root(candidate):
                decision = validate_path_choice(candidate)
                cwd_world = candidate
                cwd_decision = decision.decision
                break
        except (OSError, ValueError):
            # Fail-soft: skip candidates that the predicate rejects.
            pass
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    if cwd_world is None:
        return
    if cwd_decision == "deny":
        return
    if cwd_world == config_world:
        return

    os.environ["WORLD_ROOT_ADVISORY_REASON"] = (
        WORLD_ROOT_ADVISORY_CWD_CONFIG_DIVERGENCE
    )
    os.environ["WORLD_ROOT_DIVERGENT_CWD_PATH"] = cwd_world


def find_world_root_with_strategy(walnut_path):
    """Resolve world root and return ``(root, strategy)`` or ``(None, reason)``.

    Mirrors the bash sibling ``alive-common.sh::find_world`` (locked by
    fn-15-la5.4). Tier order:

      1. ``$ALIVE_WORLD_ROOT_OVERRIDE`` env override. Set-but-invalid
         fails LOUD with reason ``invalid_override`` rather than falling
         through.
      2. ``~/.config/alive/world-root`` config file (with one-time
         legacy walnut migration via T1's ``read_world_root_file``).
         Stale / corrupt content fails LOUD with reason ``stale_config``;
         tier-3 only fires when both files are absent.
      3. Bootstrap cwd walk-up via ``is_valid_world_root``. Each
         candidate is additionally filtered through
         ``validate_path_choice``; ``confirm_required`` (e.g. bare
         ``$HOME``, iCloud, Dropbox, GoogleDrive) and ``deny`` candidates
         are REJECTED. The first ``allow`` candidate wins.
      4. Fail with reason ``denied_home`` if a confirm_required:home
         candidate was seen and refused, otherwise ``not_found``.

    Cowork mount-scan (the bash sibling's tier-3b) is intentionally
    NOT replicated here: Python tier 3 has only the cwd walk-up branch
    by design (see ``project.py:find_world_root`` historical comment).
    Surfaces that need cowork discovery pass ``--plugin-root`` or set
    ``$ALIVE_WORLD_ROOT_OVERRIDE`` explicitly.

    ``$ALIVE_WORLD_ROOT`` is NOT consumed (the env-var tier was retired
    in fn-15-la5). Callers that need an explicit env override use
    ``$ALIVE_WORLD_ROOT_OVERRIDE`` instead.

    Returns:
        ``(root, strategy)`` on success where ``strategy`` is one of
        ``WORLD_ROOT_STRATEGY_OVERRIDE`` / ``_CONFIG_FILE`` / ``_BOOTSTRAP``.
        ``(None, reason)`` on failure where ``reason`` is one of
        ``WORLD_ROOT_FAIL_NOT_FOUND`` / ``_STALE_CONFIG`` /
        ``_INVALID_OVERRIDE`` / ``_DENIED_HOME``.
    """
    # Local import to avoid pulling _world_root_io at module-import
    # time (keeps the import graph cheap for callers that only need
    # ``atomic_write_*`` helpers).
    from _world_root_io import (  # noqa: PLC0415
        WorldRootStatus,
        is_valid_world_root,
        read_world_root_file,
        validate_path_choice,
        validate_world_root,
    )

    # fn-25: clear divergence advisory channel on entry (parity with the
    # bash sibling's ``unset WORLD_ROOT_DIVERGENT_CWD_PATH`` at find_world
    # entry). Prior calls cannot leak into this resolve. The
    # WORLD_ROOT_ADVISORY_REASON channel is shared with migration_write_failed
    # and is managed by ``read_world_root_file``; we defer to it for that
    # value but ensure stale divergence path state is never observed.
    os.environ.pop("WORLD_ROOT_DIVERGENT_CWD_PATH", None)
    if (
        os.environ.get("WORLD_ROOT_ADVISORY_REASON")
        == WORLD_ROOT_ADVISORY_CWD_CONFIG_DIVERGENCE
    ):
        os.environ.pop("WORLD_ROOT_ADVISORY_REASON", None)

    # ---- Tier 1: env override -----------------------------------------
    override_raw = os.environ.get("ALIVE_WORLD_ROOT_OVERRIDE")
    if override_raw:
        override_norm = _normalize_lexically(override_raw)
        if override_norm is not None and validate_world_root(override_norm) == WorldRootStatus.OK:
            return override_norm, WORLD_ROOT_STRATEGY_OVERRIDE
        # Set-but-invalid override fails loud rather than falling
        # through; an explicit override should never silently get
        # downgraded to a different world.
        return None, WORLD_ROOT_FAIL_INVALID_OVERRIDE

    # ---- Tier 2: config file (alive + legacy walnut migration) --------
    # ``read_world_root_file`` returns ``None`` for both "absent" and
    # "stale" today (the bash sibling distinguishes via rc + exported
    # status; the Python helper folds them so the resolver can stay
    # simple). To honour the spec's "fail loud on stale" requirement we
    # detect the stale case directly: if either config file contains
    # parseable content but the helper returned None, that's stale.
    config_path = os.path.expanduser("~/.config/alive/world-root")
    legacy_path = os.path.expanduser("~/.config/walnut/world-root")
    config_helper_result = None
    config_helper_error = None
    try:
        config_helper_result = read_world_root_file()
    except ValueError:
        # Corrupt content (multi-line / empty / non-absolute) -> stale.
        config_helper_error = "corrupt"

    if config_helper_error == "corrupt":
        return None, WORLD_ROOT_FAIL_STALE_CONFIG
    if config_helper_result is not None:
        config_world = str(config_helper_result)
        # fn-25: surface-gated cwd-vs-config divergence advisory. Skipped
        # when read_world_root_file already set migration_write_failed
        # (the more actionable signal; user must heal the alive-config
        # writability before any divergence --fix could land). The
        # migration-failed channel is exported via WORLD_ROOT_FAIL_REASON
        # by read_world_root_file (see _world_root_io.read_world_root_file)
        # rather than WORLD_ROOT_ADVISORY_REASON, so that's where we read
        # the discriminator from.
        if (
            os.environ.get("WORLD_ROOT_FAIL_REASON")
            != "migration_write_failed"
        ):
            _detect_cwd_config_divergence(config_world, walnut_path)
        return config_world, WORLD_ROOT_STRATEGY_CONFIG_FILE

    # Helper returned None: distinguish "absent" (falls through) from
    # "present but stale" (fail loud). Inspect the on-disk files
    # directly to decide.
    for cfg in (config_path, legacy_path):
        if os.path.isfile(cfg):
            try:
                with open(cfg, "r", encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                # Present but unreadable (perm 0, bad encoding) is
                # treated the same way the bash sibling treats it:
                # stale_config fail-loud rather than silent fall-through.
                return None, WORLD_ROOT_FAIL_STALE_CONFIG
            stripped = raw.strip()
            if not stripped:
                # Empty file is corrupt content per T1 helper rules.
                return None, WORLD_ROOT_FAIL_STALE_CONFIG
            non_empty_lines = [
                line for line in stripped.splitlines() if line.strip() != ""
            ]
            if len(non_empty_lines) != 1:
                return None, WORLD_ROOT_FAIL_STALE_CONFIG
            # Single parseable path on disk + helper returned None
            # means the path no longer validates -> stale.
            return None, WORLD_ROOT_FAIL_STALE_CONFIG

    # Both config files genuinely absent: fall through to bootstrap.

    # ---- Tier 3: bootstrap cwd walk-up --------------------------------
    # Normalize the starting path through the same lexical pipeline used
    # by the predicate so a path that fails normalization can never
    # slip past validation. Falling back to ``$PWD`` matches the bash
    # sibling's ``${HOOK_CWD:-${CLAUDE_PROJECT_DIR:-$PWD}}`` choice;
    # Python callers pass ``walnut_path`` explicitly so we honour it
    # first.
    raw_start = os.fspath(walnut_path) if walnut_path else os.getcwd()
    start = _normalize_lexically(raw_start)
    if start is None:
        # Unnormalizable input: defer to the legacy abspath/expanduser
        # so the resolver's behavior is at least defined. The
        # validate_path_choice gate below will reject anything truly
        # weird.
        start = os.path.abspath(os.path.expanduser(raw_start))

    saw_home_confirm_required = False
    current = start
    while current and current != "/":
        candidate = _normalize_lexically(current) or current
        if is_valid_world_root(candidate):
            decision = validate_path_choice(candidate)
            if decision.decision == "allow":
                return candidate, WORLD_ROOT_STRATEGY_BOOTSTRAP
            if decision.decision == "confirm_required" and decision.category == "home":
                saw_home_confirm_required = True
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # ``/`` is intentionally NOT probed at the end of the walk: the bash
    # sibling terminates the loop at ``/`` without testing it (and ``/``
    # would be hard-denied by ``validate_path_choice`` regardless).

    # ---- Tier 4: fail -------------------------------------------------
    if saw_home_confirm_required:
        return None, WORLD_ROOT_FAIL_DENIED_HOME
    return None, WORLD_ROOT_FAIL_NOT_FOUND


def find_world_root(walnut_path):
    """Find the ALIVE world root for a given walnut path.

    Thin wrapper over :func:`find_world_root_with_strategy`; raises
    ``FileNotFoundError`` on failure with the resolver's fail-reason
    embedded in the message so callers that don't want to destructure
    the tuple can still surface a useful error.

    Tier order is locked: env override -> config file -> bootstrap
    cwd walk-up -> fail. See
    :func:`find_world_root_with_strategy` for the full contract.
    """
    result, label = find_world_root_with_strategy(walnut_path)
    if result is None:
        # ``label`` is the fail-reason taxonomy in this branch.
        raise FileNotFoundError(
            "Could not locate ALIVE world root from {} (reason: {}). "
            "Tried: $ALIVE_WORLD_ROOT_OVERRIDE, "
            "~/.config/alive/world-root (with legacy walnut migration), "
            "and cwd walk-up via is_valid_world_root.".format(
                os.fspath(walnut_path) if walnut_path else os.getcwd(),
                label,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Plugin-root resolution
# ---------------------------------------------------------------------------

def _looks_like_plugin_root(path):
    """True if path contains .claude-plugin/plugin.json."""
    return os.path.isfile(os.path.join(path, ".claude-plugin", "plugin.json"))


def resolve_plugin_root(override=None):
    """Resolve the ALIVE plugin root directory.

    Resolution order:
      1. Explicit `override` argument (from CLI --plugin-root).
      2. ALIVE_PLUGIN_ROOT environment variable.
      3. Auto-discovery: walk up from this file looking for
         `.claude-plugin/plugin.json`.

    Explicit sources (override + env) are validated against the
    `.claude-plugin/plugin.json` marker and raise FileNotFoundError on
    mismatch -- a silent fallback would let `alive --plugin-root /bad/path`
    mask user misconfiguration. Non-Claude-Code surfaces (Cowork, MCP,
    future native clients) don't set ALIVE_PLUGIN_ROOT, so the override
    argument is the deterministic entry point.
    """
    if override:
        path = os.path.abspath(
            os.path.expandvars(os.path.expanduser(override))
        )
        if not _looks_like_plugin_root(path):
            raise FileNotFoundError(
                "--plugin-root {!r} is not an ALIVE plugin root "
                "(missing .claude-plugin/plugin.json).".format(path)
            )
        return path

    env = os.environ.get("ALIVE_PLUGIN_ROOT")
    if env:
        path = os.path.abspath(
            os.path.expandvars(os.path.expanduser(env))
        )
        if not _looks_like_plugin_root(path):
            raise FileNotFoundError(
                "$ALIVE_PLUGIN_ROOT={!r} is not an ALIVE plugin root "
                "(missing .claude-plugin/plugin.json).".format(path)
            )
        return path

    # Auto-discover: walk up from this file until .claude-plugin/plugin.json.
    current = os.path.dirname(os.path.abspath(__file__))
    while True:
        if _looks_like_plugin_root(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    raise FileNotFoundError(
        "Could not resolve ALIVE plugin root. Tried: --plugin-root override, "
        "$ALIVE_PLUGIN_ROOT, and walk-up from {} for "
        ".claude-plugin/plugin.json.".format(
            os.path.dirname(os.path.abspath(__file__))
        )
    )


# ---------------------------------------------------------------------------
# Time + session helpers
# ---------------------------------------------------------------------------

def iso_now():
    """Return current UTC time as an ISO 8601 string (second precision, Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _synthesize_session_id():
    """Construct a synthesized session ID: {hex8}-anon-{pid}-{hex6}.

    Leading 8 chars are hex so that squirrel_short_id(session_id) yields a
    valid squirrel label matching the `[a-f0-9]{8}` convention used by
    alive-context-watch.sh and project.py:parse_log.
    """
    seed = "{}-{}".format(uuid.getnode(), time.time_ns())
    hex8 = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    hex6 = secrets.token_hex(3)
    return "{}-anon-{}-{}".format(hex8, os.getpid(), hex6)


def resolve_session_id(override=None):
    """Resolve the current session ID.

    Chain:
      1. Explicit `override` argument.
      2. ALIVE_SESSION_ID environment variable.
      3. CLAUDE_SESSION_ID environment variable.
      4. Synthesized `{hex8}-anon-{pid}-{hex6}`.

    Returns the full session ID. Callers that need the 8-char squirrel label
    should pipe the result through squirrel_short_id().
    """
    if override:
        return override
    env = os.environ.get("ALIVE_SESSION_ID")
    if env:
        return env
    env = os.environ.get("CLAUDE_SESSION_ID")
    if env:
        return env
    return _synthesize_session_id()


# ---------------------------------------------------------------------------
# Advisory file locking (fcntl.flock context manager + ownership token)
# ---------------------------------------------------------------------------

#: Default total wait budget (seconds) and per-retry sleep for flock_file.
#: 5s / 100ms => 50 non-blocking attempts before giving up. Tuned to match
#: the log.py lock acquisition timing so promote.py and tasks.py share the
#: same backpressure characteristics on contended walnuts.
_FLOCK_DEFAULT_TIMEOUT_SECONDS = 5.0
_FLOCK_DEFAULT_RETRY_INTERVAL = 0.1


class FlockTimeoutError(OSError):
    """Raised when ``flock_file`` cannot acquire the lock within the budget.

    Subclasses ``OSError`` so callers that already catch ``OSError`` on
    flock paths see the timeout naturally; callers that want to report a
    distinct exit code (e.g. promote.py's exit 5 for lock contention)
    catch this subclass first.
    """


class WrongLockError(RuntimeError):
    """Raised when an `_unlocked` helper is handed a `LockGuard` for the
    wrong lockfile path.

    The split-locking design (``tasks.add`` locks; ``tasks.add_unlocked``
    is called by promote.py from inside its own outer lock) needs a sound
    proof-of-ownership check, since `fcntl.flock` is per-fd and gives no
    portable way to ask "did THIS process already acquire THIS lockfile?"
    The token-passing pattern -- locking helpers hand callers a
    ``LockGuard(path, fd)`` and unlocked helpers refuse to run unless the
    guard's path matches the lockfile they would have taken -- closes
    that gap statically. Mismatched-guard callers see this exception
    BEFORE any state mutation.
    """


@dataclass(frozen=True)
class LockGuard:
    """Proof-of-ownership token for an advisory ``fcntl.flock`` hold.

    Yielded by :func:`flock_file`. Pass to ``_unlocked`` helpers that
    expect the caller to already hold a specific lockfile (the helper
    asserts ``guard.path == expected_lockfile`` and raises
    :class:`WrongLockError` on mismatch). Frozen so a caller cannot
    forge a guard by mutating an existing token.

    Attributes
    ----------
    path : str
        Absolute path of the lockfile the guard was acquired against.
        Compared via byte equality after both sides apply
        ``os.path.abspath`` -- callers are responsible for normalizing
        the lockfile path before passing it to ``flock_file`` (a guard
        with a non-canonical path will fail the equality check on a
        canonicalized expected path).
    fd : int
        The open file descriptor backing the lock. Exposed for callers
        that need it (rare); :func:`flock_file` owns the close + unlock
        on context exit.
    """

    path: str
    fd: int


@contextlib.contextmanager
def flock_file(
    lock_path,
    timeout_seconds: float = _FLOCK_DEFAULT_TIMEOUT_SECONDS,
    retry_interval: float = _FLOCK_DEFAULT_RETRY_INTERVAL,
):
    """Acquire an advisory ``LOCK_EX`` lock on *lock_path*; yield a
    :class:`LockGuard` token.

    The lockfile is created (mode ``0o644``) if missing. The lock is
    acquired with ``LOCK_EX | LOCK_NB`` in a bounded retry loop so a
    blocked writer never stalls longer than ``timeout_seconds``; on
    timeout :class:`FlockTimeoutError` is raised. The fd carries the
    ``O_CLOEXEC`` flag where supported so child processes never inherit
    the lock holder by accident.

    Parent directories are created as needed. The yielded guard's
    ``path`` is the absolute (`os.path.abspath`) form of *lock_path* so
    downstream ``guard.path == ...`` checks compare canonical paths
    regardless of how the caller spelled the input.

    Why a separate lockfile (not the file being protected): the protected
    file (e.g. ``_kernel/tasks.json``) is rewritten via
    ``atomic_write_json`` which renames a tempfile over the inode. Locks
    on a renamed inode are meaningless on POSIX -- the second writer
    locks the new inode while the first writer thinks they hold the lock
    on the old one. Routing every lock through a dedicated sentinel file
    avoids this entire class of bug.
    """
    lock_path = os.path.abspath(os.fspath(lock_path))
    parent = os.path.dirname(lock_path) or "."
    os.makedirs(parent, exist_ok=True)

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, 0o644)
    try:
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pass
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
            if time.monotonic() >= deadline:
                raise FlockTimeoutError(
                    "flock_file timed out after {:.3f}s on {}".format(
                        float(timeout_seconds), lock_path
                    )
                )
            time.sleep(float(retry_interval))
        guard = LockGuard(path=lock_path, fd=fd)
        try:
            yield guard
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # Closing the fd implicitly releases the flock on POSIX,
                # so swallow rather than mask the original exception.
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Time + session helpers (continued)
# ---------------------------------------------------------------------------

def squirrel_short_id(session_id):
    """Return the 8-character squirrel label for a session ID.

    Trivially `session_id[:8]` today -- kept as a dedicated function so that
    the convention lives in one place and can be hardened later (e.g. once
    the v4 atoms-pipeline revisits the session-id scheme).
    """
    if session_id is None:
        return ""
    return str(session_id)[:8]
