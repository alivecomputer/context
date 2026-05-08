#!/usr/bin/env python3
"""``alive doctor`` -- environment probe subcommand.

Emits structured JSON describing the state of every piece of environment
the other ``alive`` subcommands depend on: filesystem permissions on the
walnut, the Python interpreter, walnut-root resolution, and the presence
of the ``git`` CLI.

Design notes
------------
* **Agent-facing JSON by default.** Stdout is a single JSON object
  whenever stdout is not a TTY (the common case: subprocess capture
  from skills, agents, hooks). Human-readable text mode is emitted
  on TTY stdout unless ``--json`` forces JSON, or ``--text`` forces
  text from a non-TTY caller. Argparse usage errors are wrapped to
  emit the same envelope (JSON or text) on stdout -- the agent never
  has to fall back to parsing stderr. The ``hint`` field on every
  check is the actionable guidance per the Anthropic tool-writing
  guide -- it is what the agent quotes back to the human when
  something is wrong.
* **Mode-selectable via ``--check``.** The default run walks the full
  battery (``perms``, ``python``, ``world-root``, ``git``; plus ``log``
  only when ``--walnut`` is provided so the T7 skill precheck can ask
  for it by name). ``--check=<name>`` runs exactly one check and emits a
  narrower envelope: ``{"check": {...}, "degraded": bool}``.
* **Exit-code contract.**
    - ``0``  all-ok
    - ``1``  any non-permission ``fail`` surfaced
    - ``2``  usage error (argparse / bad ``--check`` name)
    - ``3``  ``--walnut`` path does not exist
    - ``4``  any permission-style ``fail`` surfaced (``perms`` / ``log``).
      Separate code because the T7 skill precheck wants to distinguish
      "re-run with sudo?" from "fix your Python version".
* **No ``yq`` check.** The plugin does not depend on ``yq`` -- any
  scripts that needed YAML-parsing on the shell-side have been migrated
  to stdlib Python via ``_common.py``.
* **``find_world_root`` with strategy label.** We consume
  ``find_world_root_with_strategy`` (see ``_common.py``) so the
  ``world-root`` check can tell the agent *which* strategy matched
  (``override`` vs ``config file`` vs ``bootstrap``). That
  information is load-bearing for debugging install-time routing
  issues. The fn-15-la5 rework collapses the historical ``.alive/
  marker`` / ``domain dirs`` / ``env var`` strategies into the new
  three-tier scheme; on failure the helper returns
  ``(None, fail_reason)`` rather than raising, and the fail-reason
  surfaces in ``detail`` so T7's ``--fix`` can branch on it.

``SCHEMA_METADATA``
-------------------
Exported at module top-level for T4 (``alive schema``) to inspect. The
registry consumes this to build the machine-readable CLI contract --
subcommand name, summary, args, and the one-line agent-facing purpose
the skill can surface without loading ``--help``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

# Ensure `import _common` works whether doctor.py is imported from the
# dispatcher or invoked directly (python3 scripts/doctor.py ...).
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _common import (  # noqa: E402
    WORLD_ROOT_STRATEGY_ALIVE_MARKER,
    WORLD_ROOT_STRATEGY_BOOTSTRAP,
    WORLD_ROOT_STRATEGY_CONFIG_FILE,
    WORLD_ROOT_STRATEGY_DOMAIN_DIRS,
    WORLD_ROOT_STRATEGY_ENV_VAR,
    WORLD_ROOT_STRATEGY_OVERRIDE,
    canonical_strategy,
    find_world_root_with_strategy,
)


# fn-15-la5 T7: locked v3.x stable contract. The doctor surface emits
# EXACTLY three strategy labels post-T7. The retired ids (``env-var``,
# ``bootstrap-marker``, ``bootstrap-domain-dirs``) are kept ONLY as a
# deprecation breadcrumb in ``SCHEMA_METADATA`` so external JSON
# consumers loading a stale doctor dump can translate. No code path in
# this module returns a deprecated label; ``canonical_strategy`` gates
# every label lookup so a deprecated id slipping in from a third-party
# caller is mapped onto its current equivalent before surfacing.
_STRATEGY_LABELS = {
    WORLD_ROOT_STRATEGY_OVERRIDE: "env override",
    WORLD_ROOT_STRATEGY_CONFIG_FILE: "config file",
    WORLD_ROOT_STRATEGY_BOOTSTRAP: "bootstrap (cwd walk-up)",
}

# Locked deprecation map (fn-15-la5 T7). External JSON consumers that
# pattern-matched on the pre-T5 strategy ids read this from
# ``SCHEMA_METADATA["world_root_strategies"]["deprecated"]`` to translate
# old labels onto the live three. Stable from v3.x.
#
# The public-contract names (``env-var``, ``bootstrap-marker``,
# ``bootstrap-domain-dirs``) differ from ``_common``'s internal retired
# constants (``env-var``, ``alive-marker``, ``domain-dirs``): the
# external surface uses ``bootstrap-`` prefixes to make the
# subsumption-into-bootstrap explicit. Both name shapes resolve to the
# same live id when fed through ``_resolve_deprecated_label`` below.
_STRATEGY_DEPRECATION_MAP = {
    # Public-contract names (pre-T5 surface text):
    "env-var": WORLD_ROOT_STRATEGY_OVERRIDE,
    "bootstrap-marker": WORLD_ROOT_STRATEGY_BOOTSTRAP,
    "bootstrap-domain-dirs": WORLD_ROOT_STRATEGY_BOOTSTRAP,
    # Internal _common retired constants (forwarded so callers passing
    # those by accident still get a live id back):
    WORLD_ROOT_STRATEGY_ENV_VAR: WORLD_ROOT_STRATEGY_OVERRIDE,
    WORLD_ROOT_STRATEGY_ALIVE_MARKER: WORLD_ROOT_STRATEGY_BOOTSTRAP,
    WORLD_ROOT_STRATEGY_DOMAIN_DIRS: WORLD_ROOT_STRATEGY_BOOTSTRAP,
}


def _resolve_deprecated_label(label):
    """Translate a (possibly-deprecated) label to a current live id.

    Idempotent on current ids -- ``override`` -> ``override``,
    ``config-file`` -> ``config-file``, ``bootstrap`` -> ``bootstrap``.
    Rewrites every key in ``_STRATEGY_DEPRECATION_MAP`` to its current
    equivalent. Unknown ids pass through unchanged so external callers
    that pre-canonicalized via ``_common.canonical_strategy`` still get
    the same answer.
    """
    return _STRATEGY_DEPRECATION_MAP.get(label, label)


# ---------------------------------------------------------------------------
# Schema metadata (consumed by T4 alive schema)
# ---------------------------------------------------------------------------

#: Top-level metadata describing this subcommand. T4's ``alive schema``
#: walks the subcommand registry and collects each module's
#: ``SCHEMA_METADATA`` dict; keeping the contract declarative here means
#: the schema-JSON stays in sync with the argparse definition below
#: without duplication.
SCHEMA_METADATA = {
    "name": "doctor",
    "summary": (
        "Environment probe. Default JSON output describes filesystem "
        "permissions, Python version, walnut-root resolution, and git "
        "availability."
    ),
    "description": (
        "Default run walks every applicable check. Narrow with "
        "``--check=<name>`` for T7-style preflights (e.g. "
        "``alive doctor --check=log --walnut <path>`` before log prepend). "
        "Walnut is auto-detected by walking up from cwd for ``_kernel/"
        "key.md`` when ``--walnut`` is omitted."
    ),
    "checks": [
        "perms",
        "log",
        "world-root",
        "python",
        "git",
    ],
    "output_modes": {
        "json": (
            "Default when stdout is not a TTY. Forced with ``--json``."
        ),
        "text": (
            "Default when stdout is a TTY. Forced with ``--text``."
        ),
    },
    # fn-15-la5 T7: locked v3.x stable contract. ``current`` enumerates
    # the live strategy ids; ``labels`` maps each to a human-readable
    # form; ``deprecated`` translates retired ids (kept for one release
    # so external JSON consumers reading stale doctor dumps can rewrite
    # cleanly); ``stable_since`` pins the version this contract took
    # effect. Code that takes a label string and looks it up via
    # ``deprecated`` MUST get a current label back for both current and
    # deprecated inputs (idempotency-on-current; rewrite-on-deprecated).
    "world_root_strategies": {
        "current": [
            WORLD_ROOT_STRATEGY_OVERRIDE,
            WORLD_ROOT_STRATEGY_CONFIG_FILE,
            WORLD_ROOT_STRATEGY_BOOTSTRAP,
        ],
        "labels": {
            WORLD_ROOT_STRATEGY_OVERRIDE: "env override",
            WORLD_ROOT_STRATEGY_CONFIG_FILE: "config file",
            WORLD_ROOT_STRATEGY_BOOTSTRAP: "bootstrap (cwd walk-up)",
        },
        "deprecated": {
            WORLD_ROOT_STRATEGY_ENV_VAR: WORLD_ROOT_STRATEGY_OVERRIDE,
            "bootstrap-marker": WORLD_ROOT_STRATEGY_BOOTSTRAP,
            "bootstrap-domain-dirs": WORLD_ROOT_STRATEGY_BOOTSTRAP,
        },
        "stable_since": "v3.x",
    },
    "exit_codes": {
        "0": "all checks ok",
        "1": "one or more non-permission checks failed",
        "2": "usage error",
        "3": "walnut path does not exist",
        "4": "one or more permission checks failed",
    },
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum Python version we'll accept without warning.
PYTHON_WARN_MIN = (3, 11)
#: Version we'll call "ok" without reservation.
PYTHON_OK_MIN = (3, 14)

# Check statuses. Kept as constants so typos fail at import time rather
# than silently producing status strings the agent does not recognise.
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

#: Checks whose ``fail`` should drive exit code 4 instead of 1. The T7
#: skill uses this distinction to differentiate "permission problem,
#: surface to human" from "environment broken, abort".
_PERMISSION_CHECKS = frozenset({"perms", "log"})


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_result(name, status, detail, hint=None):
    """Build a uniform check-result dict.

    ``hint`` is always present in the payload (null rather than omitted)
    so the agent can destructure without probing for key existence.
    """
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "hint": hint,
    }


def _probe_write(path):
    """Return ``(ok, reason)`` for write access to a directory *path*.

    Uses ``tempfile.mkstemp`` in the target directory so the probe does
    not race with concurrent writers on a real file name. Cleans up on
    every path (success AND failure) -- callers must not see leftover
    probe files under ``_kernel/``.
    """
    if not os.path.isdir(path):
        return False, "not a directory: {}".format(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".alive-doctor-probe-", dir=path)
    except OSError as exc:
        return False, "cannot create probe file: {}".format(exc)
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(tmp)
    except OSError:
        # Best-effort cleanup -- if the probe succeeded but the unlink
        # failed we still consider the directory writable; the agent
        # will clean up the stray dotfile on the next save.
        pass
    return True, None


def check_perms(walnut):
    """Permission check: walnut + ``_kernel/`` must both be writable.

    We probe *both* directories independently because subtly different
    permissions (e.g. ``_kernel/`` being writable but the walnut dir
    being read-only) would otherwise surface as cryptic failures deep
    in ``alive log prepend`` after the lock had already been acquired.
    """
    if walnut is None:
        return _check_result(
            "perms",
            STATUS_FAIL,
            "no walnut provided",
            hint="pass --walnut <path> to probe walnut permissions",
        )
    kernel = os.path.join(walnut, "_kernel")

    ok_walnut, err_walnut = _probe_write(walnut)
    if not ok_walnut:
        return _check_result(
            "perms",
            STATUS_FAIL,
            "walnut dir not writable ({}): {}".format(walnut, err_walnut),
            hint=(
                "check ownership/permissions on {}; "
                "`chmod u+w` or re-clone as the current user".format(walnut)
            ),
        )

    ok_kernel, err_kernel = _probe_write(kernel)
    if not ok_kernel:
        return _check_result(
            "perms",
            STATUS_FAIL,
            "_kernel not writable ({}): {}".format(kernel, err_kernel),
            hint=(
                "check ownership/permissions on {}; "
                "the squirrel writes log/state files here".format(kernel)
            ),
        )

    return _check_result(
        "perms",
        STATUS_OK,
        "walnut and _kernel writable: {}".format(walnut),
    )


def check_log(walnut):
    """Write-access check for ``_kernel/log.md`` specifically.

    T7's skill cut-over calls ``alive doctor --check=log --walnut <p>``
    as a precheck for ``alive log prepend`` -- we want a failure *here*
    rather than a cryptic write-error inside the locked region of the
    log prepend subcommand. We distinguish three cases:
      * file exists and is writable -> ok
      * file does not exist but ``_kernel/`` is writable -> ok (the
        prepend subcommand creates it on first call)
      * either the file exists and is not writable, OR ``_kernel/`` is
        not writable -> fail
    """
    if walnut is None:
        return _check_result(
            "log",
            STATUS_FAIL,
            "no walnut provided",
            hint="pass --walnut <path> to probe log.md writability",
        )
    kernel = os.path.join(walnut, "_kernel")
    log_path = os.path.join(kernel, "log.md")

    # Both log.md AND its parent directory must be writable: the fn-12
    # log prepend flow is tempfile-in-dir + os.replace (atomic write),
    # so a writable log.md inside a read-only _kernel/ still fails at
    # the rename step. Probe the dir unconditionally so the precheck
    # can't green-light a path the actual write will fail on.
    dir_ok, dir_err = _probe_write(kernel)
    if not dir_ok:
        return _check_result(
            "log",
            STATUS_FAIL,
            "_kernel not writable ({}): {}".format(kernel, dir_err),
            hint=(
                "ensure {} exists and is writable by the current user; "
                "`mkdir -p` + `chmod u+w` as needed. `alive log prepend` "
                "writes via temp-file + rename, which requires a "
                "writable parent directory even when log.md itself is "
                "writable".format(kernel)
            ),
        )

    if os.path.lexists(log_path):
        # Require a regular file -- ``os.access(dir, W_OK)`` returns
        # True on writable directories, so without the isfile gate a
        # broken fixture with ``log.md/`` being a directory (or a
        # device node, or a broken symlink whose target is a dir)
        # would false-positive as "log ok".
        if not os.path.isfile(log_path):
            return _check_result(
                "log",
                STATUS_FAIL,
                "log.md is not a regular file: {}".format(log_path),
                hint=(
                    "remove/rename {} and re-run; `alive log prepend` "
                    "expects a plain UTF-8 text file at this "
                    "path".format(log_path)
                ),
            )
        if os.access(log_path, os.W_OK):
            return _check_result(
                "log",
                STATUS_OK,
                "log.md + _kernel writable: {}".format(log_path),
            )
        return _check_result(
            "log",
            STATUS_FAIL,
            "log.md not writable: {}".format(log_path),
            hint=(
                "`chmod u+w {}` -- `alive log prepend` cannot "
                "rewrite a read-only log".format(log_path)
            ),
        )

    # File does not exist yet; _kernel was already probed above.
    return _check_result(
        "log",
        STATUS_OK,
        (
            "log.md does not exist yet; _kernel is writable so "
            "`alive log prepend` will create it on first call"
        ),
    )


#: Manual recovery one-liner emitted on STATUS_FAIL (fn-15-la5 T7).
#: Surfaced verbatim in the ``hint`` field so a stale-config bricked
#: install can be repaired even when the user has not yet learned about
#: ``alive doctor --fix --world-root <path>``. Format-string-free so
#: tests can assert substring equality without escape collisions.
_WORLD_ROOT_FAIL_RECOVERY = (
    "rm ~/.config/alive/world-root  # then re-run setup, or use "
    "`alive doctor --fix --world-root <path>` to pin a new path"
)


def check_world_root(walnut, cwd=None):
    """Resolve world root; surface the matched strategy.

    fn-15-la5 T7: the world-root contract is install-scoped, not
    walnut-scoped. The check now runs with a ``cwd`` argument that
    defaults to ``os.getcwd()``; the legacy ``walnut`` parameter is
    kept for backward compatibility but is no longer required. When
    both are absent the resolver still has a starting point (current
    working directory) so the bootstrap walk-up can fire.

    Return statuses (locked):
        * ``STATUS_OK``   -- strategy is ``override`` or ``config-file``
        * ``STATUS_WARN`` -- strategy is ``bootstrap`` (advisory: pin
          via ``alive doctor --fix``)
        * ``STATUS_FAIL`` -- no world found. ``hint`` includes the
          manual ``rm ~/.config/alive/world-root`` recovery one-liner.

    Retired strategy ids are canonicalized (``canonical_strategy`` +
    ``_resolve_deprecated_label``) before label lookup so external
    JSON consumers never see a deprecated id.
    """
    # T7 contract: prefer cwd (install-scoped) over walnut (legacy).
    # Either path produces a starting directory for the resolver's
    # bootstrap walk-up; the resolver itself is unchanged.
    start = cwd if cwd is not None else walnut
    if start is None:
        # Neither supplied -- defensive default. handle() always passes
        # a non-None cwd (defaulting to os.getcwd()), so we should not
        # reach this branch in normal operation; keep the fallback so
        # direct callers (e.g. an external script invoking the function
        # by hand) still get a sane result.
        try:
            start = os.getcwd()
        except OSError:
            return _check_result(
                "world-root",
                STATUS_FAIL,
                "no cwd available and no --walnut provided",
                hint=_WORLD_ROOT_FAIL_RECOVERY,
            )
    # fn-25: open the surface gate while running the resolver from the
    # doctor surface. Without this, ``alive doctor --check=world-root``
    # invocations from a TTY would never run the cwd walk-up (the
    # CLAUDE_CODE_HOOK_EVENT env var is only set by Claude Code's hook
    # surface), so a user running ``alive doctor`` directly to triage
    # divergence would see ``degraded: false`` even when standing in a
    # different valid world. Opening the explicit-opt-in flag makes the
    # doctor command authoritative for its own diagnosis. Restored on
    # exit so we don't leak the flag to other helpers in the same process.
    prior_gate = os.environ.get("ALIVE_RESOLVER_DIVERGENCE_CHECK")
    os.environ["ALIVE_RESOLVER_DIVERGENCE_CHECK"] = "1"
    try:
        root, strategy = find_world_root_with_strategy(start)
    finally:
        if prior_gate is None:
            os.environ.pop("ALIVE_RESOLVER_DIVERGENCE_CHECK", None)
        else:
            os.environ["ALIVE_RESOLVER_DIVERGENCE_CHECK"] = prior_gate
    if root is None:
        # ``strategy`` here carries the WORLD_ROOT_FAIL_REASON taxonomy
        # (not_found / stale_config / invalid_override / denied_home).
        # The hint MUST include the manual recovery one-liner so a
        # bricked install can be repaired even when the user has not
        # yet learned about ``alive doctor --fix --world-root <path>``.
        return _check_result(
            "world-root",
            STATUS_FAIL,
            "could not resolve world root from {} (reason: {})".format(
                start, strategy
            ),
            hint=_WORLD_ROOT_FAIL_RECOVERY,
        )
    # Canonicalize the strategy id through both maps -- ``_common``'s
    # internal constants (``alive-marker`` / ``domain-dirs`` /
    # ``env-var``) AND the external public-contract names
    # (``bootstrap-marker`` / ``bootstrap-domain-dirs`` / ``env-var``).
    # The double pass is idempotent on the current three live ids.
    canonical = _resolve_deprecated_label(canonical_strategy(strategy))
    label = _STRATEGY_LABELS.get(canonical, canonical)

    # fn-25: pick up the divergence advisory side channel populated by
    # the resolver (``_common._detect_cwd_config_divergence``). The
    # advisory is set ONLY after a tier-2 success when the cwd walks up
    # to a different valid world. Surface it with a STATUS_WARN so the
    # default-mode summary reports degraded:true and the user knows they
    # have a config-vs-cwd mismatch worth healing.
    divergence_detected = (
        os.environ.get("WORLD_ROOT_ADVISORY_REASON")
        == "cwd_config_divergence"
    )
    divergent_cwd = os.environ.get("WORLD_ROOT_DIVERGENT_CWD_PATH") or None

    if canonical == WORLD_ROOT_STRATEGY_BOOTSTRAP:
        # Bootstrap-resolves is advisory: the world resolves today via
        # cwd walk-up but moving cwd / running outside any walnut would
        # break it. Recommend pinning via ``alive doctor --fix``.
        result = _check_result(
            "world-root",
            STATUS_WARN,
            "resolved via {}: {}".format(label, root),
            hint=(
                "Run `alive doctor --check=world-root --fix` to pin "
                "this world-root via ~/.config/alive/world-root"
            ),
        )
    elif divergence_detected and divergent_cwd:
        # Config-resolved successfully BUT cwd is in a different valid
        # world. Surface as warn (degraded but not broken) and steer the
        # user toward the doctor --fix self-heal path. The detail copy
        # names both paths verbatim per the locked acceptance.
        result = _check_result(
            "world-root",
            STATUS_WARN,
            "cwd-vs-config divergence: cwd is in {}, config resolved {}: {}".format(
                divergent_cwd, label, root
            ),
            hint=(
                "Run `alive doctor --check=world-root --fix` and "
                "restart Claude Code to switch the config to the world "
                "you are standing in"
            ),
        )
    else:
        result = _check_result(
            "world-root",
            STATUS_OK,
            "resolved via {}: {}".format(label, root),
        )
    result["root"] = root
    result["strategy"] = canonical
    if divergence_detected and divergent_cwd:
        # fn-25: structured divergence info for JSON consumers. Sibling
        # to ``detail`` (which stays a string) so the existing schema
        # contract is preserved; agents that parse divergence specifically
        # destructure ``check.divergence``.
        result["divergence"] = {
            "divergence": True,
            "config_resolved": root,
            "cwd_resolved": divergent_cwd,
        }
    return result


def check_python():
    """Python version check.

    - >= 3.14 -> ok
    - >= 3.11 -> warn (works today but pinned for fn-12+ refactor)
    - <  3.11 -> fail
    """
    v = sys.version_info
    version_str = "{}.{}.{}".format(v.major, v.minor, v.micro)
    current = (v.major, v.minor)

    if current >= PYTHON_OK_MIN:
        return _check_result(
            "python",
            STATUS_OK,
            "python {}".format(version_str),
        )
    if current >= PYTHON_WARN_MIN:
        return _check_result(
            "python",
            STATUS_WARN,
            "python {} (below {}.{} target)".format(
                version_str, PYTHON_OK_MIN[0], PYTHON_OK_MIN[1]
            ),
            hint=(
                "runs today but upgrade to {}.{}+ before the fn-12 "
                "abstraction work compounds".format(
                    PYTHON_OK_MIN[0], PYTHON_OK_MIN[1]
                )
            ),
        )
    return _check_result(
        "python",
        STATUS_FAIL,
        "python {} is below the {}.{} minimum".format(
            version_str, PYTHON_WARN_MIN[0], PYTHON_WARN_MIN[1]
        ),
        hint=(
            "install python {}.{} or newer; the alive CLI relies on "
            "features introduced since 3.11".format(
                PYTHON_OK_MIN[0], PYTHON_OK_MIN[1]
            )
        ),
    )


def check_git():
    """Is ``git`` on PATH?"""
    git = shutil.which("git")
    if git:
        return _check_result(
            "git",
            STATUS_OK,
            "git at {}".format(git),
        )
    return _check_result(
        "git",
        STATUS_FAIL,
        "git not found on PATH",
        hint=(
            "install git (e.g. `brew install git` or your distro's "
            "package manager); several alive subcommands shell out to it"
        ),
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# Map of check-name -> (callable, kind). ``kind`` distinguishes checks
# that REQUIRE the walnut arg ("walnut") from those that take ``cwd``
# instead ("cwd") and from walnut-independent ones ("none").
#
# fn-15-la5 T7 demoted ``world-root`` from "walnut" to "cwd": the
# world-root contract is install-scoped, not walnut-scoped. Outside any
# walnut, ``--check=world-root`` still fires using the supplied / default
# ``--cwd``. Inside a walnut, the check uses the walnut path as the
# starting point (still cwd-shaped from the resolver's perspective).
_CHECKS = {
    "perms": (check_perms, "walnut"),
    "log": (check_log, "walnut"),
    "world-root": (check_world_root, "cwd"),
    "python": (check_python, "none"),
    "git": (check_git, "none"),
}


def _run_single_check(name, walnut, cwd):
    """Run one check by name. Raises KeyError on unknown name."""
    fn, kind = _CHECKS[name]
    if kind == "walnut":
        return fn(walnut)
    if kind == "cwd":
        # ``world-root`` is install-scoped post-T7: ``cwd`` is the
        # primary starting point and ALWAYS wins inside
        # ``check_world_root`` when non-None. ``handle()`` defaults
        # ``cwd`` to ``os.getcwd()`` so it is effectively never None
        # in normal operation; the legacy ``walnut`` positional is
        # passed through only as a final fallback for direct callers
        # of ``_run_single_check`` that may have left ``cwd`` unset.
        return fn(walnut, cwd=cwd)
    return fn()


def _degraded(checks):
    """Any check warn/fail -> degraded."""
    return any(c["status"] != STATUS_OK for c in checks)


def _compute_exit_code(checks):
    """Map statuses to exit code per the contract in the module docstring."""
    has_perm_fail = any(
        c["status"] == STATUS_FAIL and c["name"] in _PERMISSION_CHECKS
        for c in checks
    )
    if has_perm_fail:
        return 4
    has_other_fail = any(c["status"] == STATUS_FAIL for c in checks)
    if has_other_fail:
        return 1
    return 0


def _summary_line(checks):
    """One-line human-ish summary, e.g. ``4 ok, 1 warn``."""
    counts = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    parts = []
    for k in (STATUS_OK, STATUS_WARN, STATUS_FAIL):
        if counts[k]:
            parts.append("{} {}".format(counts[k], k))
    return ", ".join(parts) if parts else "no checks run"


# ---------------------------------------------------------------------------
# Walnut auto-detection
# ---------------------------------------------------------------------------

def _detect_walnut_from_cwd():
    """Walk up from ``os.getcwd()`` looking for ``_kernel/key.md``.

    Returns the walnut root path or ``None``. A walnut is any directory
    containing ``_kernel/key.md`` -- matches the convention used by
    ``_common.find_all_walnuts``. Pure best-effort: silent failure
    returns None so the caller can decide whether to fall back to
    walnut-independent checks.
    """
    try:
        current = os.path.abspath(os.getcwd())
    except OSError:
        return None
    while True:
        if os.path.isfile(os.path.join(current, "_kernel", "key.md")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

def _render_check_line(c):
    """Render one check-result dict as a human-readable line."""
    marker = {
        STATUS_OK: "ok  ",
        STATUS_WARN: "warn",
        STATUS_FAIL: "fail",
    }.get(c["status"], c["status"])
    base = "[{}] {}: {}".format(marker, c["name"], c["detail"])
    if c.get("hint"):
        base += "\n       hint: {}".format(c["hint"])
    return base


def _render_text_default(payload):
    """Render the default-mode payload as human-readable text."""
    lines = []
    for c in payload["checks"]:
        lines.append(_render_check_line(c))
    lines.append("")
    lines.append(
        "summary: {} ({})".format(
            payload["summary"],
            "degraded" if payload["degraded"] else "healthy",
        )
    )
    if payload.get("migration_hint"):
        lines.append("")
        lines.append("migration: {}".format(payload["migration_hint"]))
    return "\n".join(lines)


def _render_text_narrow(payload):
    """Render the ``--check=<name>`` narrow payload as text."""
    c = payload["check"]
    line = _render_check_line(c)
    suffix = "\ndegraded: {}".format(
        "true" if payload["degraded"] else "false"
    )
    if payload.get("migration_hint"):
        suffix += "\nmigration: {}".format(payload["migration_hint"])
    return line + suffix


def _render_text_error(payload):
    """Render the error envelope (exit 2/3) as text."""
    out = "error: {}".format(payload.get("error", ""))
    if payload.get("hint"):
        out += "\nhint: {}".format(payload["hint"])
    return out


def _render_text_fix(payload):
    """Render the ``--fix`` payload as human-readable text.

    Three shapes:
        * Successful no-op (already pinned): single line with the strategy.
        * Successful write (bootstrap or recovery): two lines (action + path).
        * Failed (setup hint or unknown strategy): the fix detail + the
          underlying check line if present.
    """
    fix = payload.get("fix") or {}
    action = fix.get("action", "")
    detail = fix.get("detail", "")
    marker = "ok  " if fix.get("ok") else "fail"
    lines = ["[{}] fix: {}".format(marker, detail or action)]
    check = payload.get("check")
    if check:
        lines.append(_render_check_line(check))
    if "degraded" in payload:
        lines.append(
            "degraded: {}".format(
                "true" if payload["degraded"] else "false"
            )
        )
    if payload.get("migration_hint"):
        lines.append("migration: {}".format(payload["migration_hint"]))
    return "\n".join(lines)


def _emit(payload, json_mode, renderer):
    """Write the payload to stdout in JSON or text mode.

    ``json_mode`` is a tri-state:
      * True  -- force JSON (``--json`` flag OR stdout is not a TTY)
      * False -- force text (``--text`` explicit)
      * None  -- unreachable at call-time; ``handle`` resolves to bool

    ``renderer`` is a callable ``payload -> str`` for the text path.
    """
    if json_mode:
        print(json.dumps(payload, indent=2))
    else:
        print(renderer(payload))


# ---------------------------------------------------------------------------
# Argparse registration
# ---------------------------------------------------------------------------

def register(subparsers):
    """Register the ``doctor`` subcommand on the CLI dispatcher.

    Mirrors ``--plugin-root`` on the subparser so ``alive doctor
    --plugin-root ...`` and ``alive --plugin-root ... doctor`` both
    parse (argparse's standard subparser semantics don't let the
    top-level flag appear *after* the subcommand otherwise).
    """
    parser = subparsers.add_parser(
        "doctor",
        help=SCHEMA_METADATA["summary"],
        description=SCHEMA_METADATA["summary"],
    )
    parser.add_argument(
        "--plugin-root",
        default=None,
        help=(
            "Override the ALIVE plugin root directory "
            "(defaults: $ALIVE_PLUGIN_ROOT, then auto-discovery)."
        ),
    )
    parser.add_argument(
        "--walnut",
        default=None,
        help="Path to the walnut to probe. Required for perms/log.",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help=(
            "Starting directory for ``--check=world-root`` bootstrap "
            "discovery. Defaults to ``os.getcwd()``. Install-scoped: "
            "no ``--walnut`` required."
        ),
    )
    parser.add_argument(
        "--check",
        default=None,
        choices=sorted(_CHECKS.keys()),
        help=(
            "Run a single named check. Default runs every applicable check."
        ),
    )
    # fn-15-la5 T7: ``--fix`` mode pins the resolved path into
    # ~/.config/alive/world-root via T1's atomic helper. Scoped to
    # ``--check=world-root`` -- other checks ignore the flag.
    parser.add_argument(
        "--fix",
        dest="fix",
        action="store_true",
        default=False,
        help=(
            "With ``--check=world-root``: pin the resolved path into "
            "~/.config/alive/world-root via the atomic config-file "
            "helper. With ``--world-root <path>``: recovery mode -- "
            "atomically replace the config file regardless of any "
            "stale-config brick state."
        ),
    )
    parser.add_argument(
        "--world-root",
        dest="world_root",
        default=None,
        help=(
            "Recovery path used by ``--fix --world-root <path>``. "
            "Lexically normalized; runs the canonical ``is_valid_world_root`` "
            "predicate AND the system-path policy validator before writing."
        ),
    )
    parser.add_argument(
        "--allow-home",
        dest="allow_home",
        action="store_true",
        default=False,
        help=(
            "With ``--fix --world-root $HOME``: bypass the "
            "``confirm_required: home`` decision and write the file."
        ),
    )
    parser.add_argument(
        "--allow-cloud",
        dest="allow_cloud",
        action="store_true",
        default=False,
        help=(
            "With ``--fix --world-root <cloud-sync path>``: bypass the "
            "``confirm_required: cloud`` decision (iCloud / Dropbox / "
            "Google Drive) and write the file."
        ),
    )
    # Output mode: JSON default-on when stdout is not a TTY (agent
    # consumption); text mode on TTY stdout (human triage). ``--json``
    # forces JSON regardless of TTY; ``--text`` forces human text.
    # Mutually exclusive -- you don't want both active at once.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        default=None,
        help="Force JSON output (default when stdout is not a TTY).",
    )
    mode_group.add_argument(
        "--text",
        dest="json_mode",
        action="store_false",
        default=None,
        help="Force human-readable text output (default on TTY stdout).",
    )
    # Wrap the subparser's error path so usage errors emit JSON rather
    # than the default argparse text-to-stderr. Keeps the "stdout is
    # always JSON" contract honest for agents that only ever parse
    # stdout -- they'd otherwise see an empty stdout + text stderr and
    # guess. Exit code stays at 2 (usage error) per the module contract.
    parser.error = _make_json_error_handler(parser)  # type: ignore[assignment]
    # Stash SCHEMA_METADATA on the parser so ``alive schema`` can walk it
    # without depending on this module's sys.modules name matching the
    # CLI command. Keyed by schema.SCHEMA_METADATA_DEFAULT_KEY -- imported
    # locally so doctor.py doesn't pull schema.py at module-import time.
    from schema import SCHEMA_METADATA_DEFAULT_KEY  # noqa: E402
    parser.set_defaults(
        _handler=handle,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )
    return parser


def _make_json_error_handler(parser):
    """Return an ``argparse.error`` replacement that emits a JSON envelope.

    Argparse's default ``parser.error`` prints ``usage: ...`` + the
    error to stderr and calls ``sys.exit(2)``. That's the right exit
    code, but it breaks the JSON-on-stdout contract. We override to
    emit the error payload (JSON on non-TTY, text on TTY) to stdout
    and keep exit 2. We cannot consult a resolved ``json_mode`` here
    because argparse has not finished parsing yet -- TTY-probe is the
    closest proxy that matches the post-parse default.
    """
    def _error(message):
        payload = {
            "error": "usage: {}".format(message),
            "hint": (
                "run `alive doctor --help` for the full flag list; "
                "`--check=<name>` accepts: "
                + ", ".join(sorted(_CHECKS.keys()))
            ),
        }
        json_mode = _resolve_json_mode(None)
        _emit(payload, json_mode, _render_text_error)
        # argparse expects error() to not return -- exit with usage code.
        sys.exit(2)
    return _error


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _resolve_json_mode(arg_value):
    """Resolve final JSON/text mode.

    ``arg_value`` is True (--json), False (--text), or None (unspecified).
    When unspecified, default to JSON for non-TTY stdout (agents,
    subprocess capture) and text for TTY (human triage). This matches
    the spec's ``--json default-on; text mode on TTY stdout`` wording.
    """
    if arg_value is not None:
        return bool(arg_value)
    try:
        return not sys.stdout.isatty()
    except (AttributeError, OSError):
        # If we can't probe isatty (unusual file object), default to
        # JSON -- safer for agent pipelines.
        return True


def handle(args):
    """Execute the subcommand based on parsed args; returns exit code."""
    json_mode = _resolve_json_mode(getattr(args, "json_mode", None))

    # Walnut resolution: explicit --walnut wins. If absent, walk up
    # from cwd looking for _kernel/key.md (so ``alive doctor`` Just
    # Works inside a walnut directory -- the default-run acceptance
    # test wants >= 4 checks with no args).
    walnut = args.walnut
    if walnut is not None:
        walnut_abs = os.path.abspath(os.path.expanduser(walnut))
        if not os.path.isdir(walnut_abs):
            payload = {
                "error": "walnut path does not exist: {}".format(walnut_abs),
                "hint": (
                    "verify --walnut points to the walnut directory "
                    "(the one containing _kernel/key.md)"
                ),
            }
            _emit(payload, json_mode, _render_text_error)
            return 3
        walnut = walnut_abs
    else:
        walnut = _detect_walnut_from_cwd()
        # walnut may still be None here -- the default-run loop below
        # skips walnut-dependent checks in that case rather than
        # failing them (a caller outside any walnut asking "is python
        # ok?" should get a clean answer).

    # fn-15-la5 T7: install-scoped cwd. Spec wording is "when --walnut
    # is absent, check_world_root uses --cwd for bootstrap discovery".
    # That is: --walnut wins when both are present (preserves the
    # historical --walnut <path> contract), --cwd kicks in when only
    # cwd is supplied or when neither is. We track ``cwd_explicit``
    # so we can distinguish "user passed --cwd <path>" from "argparse
    # filled in None"; only the explicit case overrides --walnut.
    cwd_explicit = getattr(args, "cwd", None) is not None
    if cwd_explicit:
        cwd = os.path.abspath(os.path.expanduser(args.cwd))
    elif walnut is not None:
        # Honour the legacy --walnut start point. check_world_root
        # treats this as cwd for the bootstrap walk-up; tier 1 + 2
        # (override / config-file) ignore it the same way.
        cwd = walnut
    else:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = None

    fix = bool(getattr(args, "fix", False))
    world_root_arg = getattr(args, "world_root", None)
    allow_home = bool(getattr(args, "allow_home", False))
    allow_cloud = bool(getattr(args, "allow_cloud", False))

    # ``--fix --world-root <path>`` is the recovery branch. Validate +
    # write before any check runs; bypasses every stale-config brick.
    if fix and world_root_arg is not None:
        return _handle_fix_with_world_root(
            world_root_arg,
            allow_home=allow_home,
            allow_cloud=allow_cloud,
            json_mode=json_mode,
        )
    if world_root_arg is not None and not fix:
        # ``--world-root`` is scoped to the ``--fix`` recovery flow per
        # the locked spec ("scoped to alive doctor --fix only"). A bare
        # ``--world-root`` without ``--fix`` is a footgun -- the user
        # has expressed an intent to pin a path but supplied no action,
        # so the flag would silently no-op. Refuse with a usage error.
        payload = {
            "error": (
                "--world-root is only meaningful with --fix; bare "
                "--world-root has no action and would silently no-op"
            ),
            "hint": (
                "supply --fix --world-root <path> to use the recovery "
                "flow, or drop --world-root to inspect the resolver"
            ),
        }
        _emit(payload, json_mode, _render_text_error)
        return 2
    if (allow_home or allow_cloud) and not (fix and world_root_arg):
        # ``--allow-home`` / ``--allow-cloud`` are scoped to the
        # ``--fix --world-root`` recovery flow. Surface a usage error
        # rather than silently ignoring them.
        payload = {
            "error": (
                "--allow-home / --allow-cloud are only meaningful with "
                "--fix --world-root <path>"
            ),
            "hint": (
                "drop the flag, or supply --fix --world-root <path> to "
                "use the recovery flow"
            ),
        }
        _emit(payload, json_mode, _render_text_error)
        return 2

    # Narrow mode -- exactly one named check.
    if args.check is not None:
        try:
            result = _run_single_check(args.check, walnut, cwd)
        except KeyError:
            # argparse's ``choices=`` should have prevented this; surface
            # exit code 2 for safety if a future refactor removes the
            # choices list. JSON envelope goes to STDOUT (not stderr)
            # so the "stdout is the single source of truth" contract
            # holds on every exit path the agent can hit.
            payload = {
                "error": "unknown check: {}".format(args.check),
                "hint": (
                    "--check accepts: "
                    + ", ".join(sorted(_CHECKS.keys()))
                ),
            }
            _emit(payload, json_mode, _render_text_error)
            return 2
        # ``--fix`` (no --world-root): pin the resolved path. Only
        # applies to ``--check=world-root``; other checks ignore the
        # flag (they don't have a "fix me" semantic today).
        if fix and args.check == "world-root":
            return _handle_fix_after_check(result, json_mode=json_mode)
        payload = {
            "check": result,
            "degraded": result["status"] != STATUS_OK,
        }
        _maybe_attach_migration_hint(payload)
        _emit(payload, json_mode, _render_text_narrow)
        return _compute_exit_code([result])

    if fix:
        # ``--fix`` without ``--check=world-root`` is ambiguous: the
        # default run touches every check and there's no single
        # "resolved path" to pin. Refuse with a usage error rather
        # than silently dropping the flag.
        payload = {
            "error": (
                "--fix requires --check=world-root (no other check "
                "supports a fix mode today)"
            ),
            "hint": (
                "run `alive doctor --check=world-root --fix` to pin "
                "the resolved world root"
            ),
        }
        _emit(payload, json_mode, _render_text_error)
        return 2

    # Default mode -- every applicable check. Walnut-dependent checks are
    # skipped when walnut could not be resolved, rather than reported as
    # failures. ``world-root`` runs install-scoped (uses cwd) so it does
    # NOT require a walnut.
    checks = []
    for name in ("perms", "log", "world-root", "python", "git"):
        fn, kind = _CHECKS[name]
        if kind == "walnut":
            if walnut is None:
                continue
            checks.append(fn(walnut))
        elif kind == "cwd":
            # world-root: legacy ``walnut`` (when present) flows in as
            # the first positional, ``cwd`` as the kwarg. cwd takes
            # precedence in ``check_world_root`` so the install-scoped
            # contract holds even when --walnut is absent.
            checks.append(fn(walnut, cwd=cwd))
        else:
            checks.append(fn())

    payload = {
        "checks": checks,
        "degraded": _degraded(checks),
        "summary": _summary_line(checks),
    }
    if walnut is not None:
        payload["walnut"] = walnut
    _maybe_attach_migration_hint(payload)
    _emit(payload, json_mode, _render_text_default)
    return _compute_exit_code(checks)


# ---------------------------------------------------------------------------
# fn-15-la5 T7: --fix mode + migration hint
# ---------------------------------------------------------------------------

#: Migration hint message. Surfaces ONCE per session when the user has
#: ``$ALIVE_WORLD_ROOT`` set in their shell (not the hook-mirrored
#: session-source) and has NOT switched to the new override env var.
_MIGRATION_HINT_MESSAGE = (
    "ALIVE_WORLD_ROOT is now session-mirror-only; use "
    "ALIVE_WORLD_ROOT_OVERRIDE to override the resolver."
)

#: Sentinel cache key. Mirrors T6's per-session sentinel pattern:
#: atomic ``mkdir`` of a directory under ``${TMPDIR:-/tmp}/`` whose
#: name is keyed by the session id; the first process to acquire wins
#: and emits, every other process sees ``EEXIST`` and stays silent.
_MIGRATION_HINT_CACHE_KEY = "migration-hint"


def _migration_hint_should_fire():
    """True iff the migration-hint env-var combo is currently active.

    Conditions (locked by spec, all three required):
        1. ``$ALIVE_WORLD_ROOT`` is set (legacy session-mirror env var).
        2. ``$ALIVE_WORLD_ROOT_SOURCE != "session"``. The hook layer
           writes ``session`` to disambiguate the user-set case from
           the hook-mirrored case; only the user-set case warrants
           the migration hint.
        3. ``$ALIVE_WORLD_ROOT_OVERRIDE`` is NOT set. Once the user
           has migrated to the new env var the hint is silent.
    """
    if not os.environ.get("ALIVE_WORLD_ROOT"):
        return False
    if os.environ.get("ALIVE_WORLD_ROOT_SOURCE") == "session":
        return False
    if os.environ.get("ALIVE_WORLD_ROOT_OVERRIDE"):
        return False
    return True


def _migration_hint_sentinel_dir():
    """Per-session sentinel dir for the migration hint.

    Mirrors T6's ``_alive_session_sentinel_dir`` in ``alive-common.sh``:
    ``${TMPDIR:-/tmp}/alive-upgrade-warned-<session-id>/``. Same shape
    so a single session emits the bridge warn AND the migration hint
    independently of each other (different cache keys -> different
    files inside the dir).
    """
    sid = (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("ALIVE_SESSION_ID")
        or os.environ.get("HOOK_SESSION_ID")
        or "no-session"
    )
    # Sanitize: replace anything that isn't [A-Za-z0-9._-] with '_' so
    # a malformed session id can't escape into a different tmp path.
    safe = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in sid
    )
    tmpdir = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmpdir, "alive-upgrade-warned-{}".format(safe))


def _try_acquire_migration_hint_sentinel():
    """Atomic-mkdir + sentinel-file race. Returns True iff THIS process
    acquired the migration-hint slot (first emit per session).

    The dir is shared with T6's bridge warn; we use a per-cache-key file
    inside the dir so the two don't race each other. ``open(..., "x")``
    is atomic at the kernel level on every POSIX filesystem we target.
    """
    parent = _migration_hint_sentinel_dir()
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    except OSError:
        # If we cannot create the parent, treat as "loser" and stay
        # silent rather than emit on every invocation.
        return False
    sentinel_file = os.path.join(parent, _MIGRATION_HINT_CACHE_KEY)
    try:
        # Exclusive create: only ONE process per session wins.
        fd = os.open(sentinel_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except OSError:
        return False


def _maybe_attach_migration_hint(payload):
    """Mutate ``payload`` to add a one-time migration hint when conditions fire.

    The hint surfaces under ``payload["migration_hint"]`` so JSON
    consumers can detect it without having to grep ``checks[].hint``
    strings. Text rendering (``_render_text_default`` /
    ``_render_text_narrow``) treats the field as advisory and prints
    it on a final line.
    """
    if not _migration_hint_should_fire():
        return
    if not _try_acquire_migration_hint_sentinel():
        return
    payload["migration_hint"] = _MIGRATION_HINT_MESSAGE


def _handle_fix_after_check(result, json_mode):
    """Implement ``alive doctor --check=world-root --fix`` (no --world-root).

    Branch on the strategy field of the resolved check result:
      * ``override`` / ``config-file``: no-op, print
        "Already pinned via <strategy>".
      * ``bootstrap``: write the resolved root to
        ~/.config/alive/world-root via T1's atomic helper. Confirm.
      * ``not-found`` (STATUS_FAIL): surface the ``/alive:world``
        setup hint.
    """
    # Local import: keeps doctor.py's import graph cheap when no
    # --fix path is exercised.
    from _world_root_io import write_world_root_file  # noqa: PLC0415

    strategy = result.get("strategy")
    root = result.get("root")
    status = result.get("status")

    # ``degraded`` in fix payloads is always derived from the embedded
    # ``check`` status -- pure function of "checks reported here".
    # Consumers that want the post-write health state read
    # ``fix.action == "wrote-config-file"`` (the next invocation will
    # see strategy=config-file and report STATUS_OK on its own).
    if status == STATUS_FAIL:
        payload = {
            "fix": {
                "action": "setup-hint",
                "ok": False,
                "detail": (
                    "No world root resolved. Run `/alive:world` to "
                    "scaffold one, OR `alive doctor --fix --world-root "
                    "<path>` to pin an existing path."
                ),
            },
            "check": result,
            "degraded": True,
        }
        _maybe_attach_migration_hint(payload)
        _emit(payload, json_mode, _render_text_fix)
        return 1

    # fn-25: divergence self-heal. When the resolver flagged a
    # cwd-vs-config divergence (config-file strategy succeeded but cwd
    # walks up to a different valid world), --fix writes the cwd-resolved
    # world to ~/.config/alive/world-root. Restart-after-fix is part of
    # the user contract and surfaces in the detail copy: the CURRENT
    # session continues running against the OLD config-resolved world
    # until the next session start re-runs find_world.
    divergence_info = result.get("divergence") or {}
    if (
        strategy == WORLD_ROOT_STRATEGY_CONFIG_FILE
        and divergence_info.get("divergence")
    ):
        cwd_resolved = divergence_info.get("cwd_resolved")
        config_resolved = divergence_info.get("config_resolved") or root
        if not cwd_resolved:
            # Defensive: divergence flag was set but cwd path is empty.
            # Fall through to the no-op branch rather than write garbage.
            pass
        else:
            try:
                write_world_root_file(cwd_resolved)
            except (OSError, ValueError) as exc:
                payload = {
                    "error": (
                        "failed to write ~/.config/alive/world-root: {}"
                        .format(exc)
                    ),
                    "hint": (
                        "check that ~/.config/alive/ is writable; rerun "
                        "with --world-root <path> to bypass any stale state"
                    ),
                }
                _emit(payload, json_mode, _render_text_error)
                return 1
            payload = {
                "fix": {
                    "action": "wrote-config-file",
                    "ok": True,
                    "detail": (
                        "Updated ~/.config/alive/world-root: {} -> {}. "
                        "Restart Claude Code for the new config to take "
                        "effect."
                    ).format(config_resolved, cwd_resolved),
                    "path": cwd_resolved,
                    "previous_path": config_resolved,
                },
                "check": result,
                # ``degraded`` mirrors the embedded check (still warn at
                # the moment the fix ran). The next invocation will
                # report STATUS_OK with no divergence flag.
                "degraded": result["status"] != STATUS_OK,
            }
            _maybe_attach_migration_hint(payload)
            _emit(payload, json_mode, _render_text_fix)
            return 0

    if strategy in (
        WORLD_ROOT_STRATEGY_OVERRIDE,
        WORLD_ROOT_STRATEGY_CONFIG_FILE,
    ):
        label = _STRATEGY_LABELS.get(strategy, strategy)
        payload = {
            "fix": {
                "action": "noop",
                "ok": True,
                "detail": "Already pinned via {}.".format(label),
            },
            "check": result,
            "degraded": result["status"] != STATUS_OK,
        }
        _maybe_attach_migration_hint(payload)
        _emit(payload, json_mode, _render_text_fix)
        return _compute_exit_code([result])

    if strategy == WORLD_ROOT_STRATEGY_BOOTSTRAP:
        if not root:
            # Defensive: bootstrap-resolves implies root is non-empty.
            payload = {
                "error": (
                    "bootstrap strategy returned an empty root; "
                    "refusing to write"
                ),
                "hint": (
                    "this is a bug -- file an issue with your "
                    "ALIVE_WORLD_ROOT_OVERRIDE / ~/.config/alive/world-root "
                    "state"
                ),
            }
            _emit(payload, json_mode, _render_text_error)
            return 1
        try:
            write_world_root_file(root)
        except (OSError, ValueError) as exc:
            payload = {
                "error": (
                    "failed to write ~/.config/alive/world-root: {}"
                    .format(exc)
                ),
                "hint": (
                    "check that ~/.config/alive/ is writable; rerun "
                    "with --world-root <path> to bypass any stale state"
                ),
            }
            _emit(payload, json_mode, _render_text_error)
            return 1
        # ``degraded`` is derived from the embedded check (warn) so a
        # JSON consumer reading a bootstrap-fix dump sees an internally
        # consistent payload: the check that triggered the fix is still
        # warn at the moment the fix ran. The action field is the
        # authoritative "did the fix succeed" signal.
        payload = {
            "fix": {
                "action": "wrote-config-file",
                "ok": True,
                "detail": (
                    "Pinned world root to ~/.config/alive/world-root: {}"
                    .format(root)
                ),
                "path": root,
            },
            "check": result,
            "degraded": result["status"] != STATUS_OK,
        }
        _maybe_attach_migration_hint(payload)
        _emit(payload, json_mode, _render_text_fix)
        return 0

    # Unknown strategy. Should never reach here because every live
    # strategy id is in ``_STRATEGY_LABELS`` above; surface a soft
    # error so a future-added strategy that forgot to register a label
    # is discoverable rather than silent.
    payload = {
        "error": (
            "--fix does not know how to handle strategy {!r}".format(
                strategy
            )
        ),
        "hint": (
            "this is a bug -- file an issue. The strategy must be one "
            "of: {}".format(", ".join(sorted(_STRATEGY_LABELS)))
        ),
    }
    _emit(payload, json_mode, _render_text_error)
    return 1


def _handle_fix_with_world_root(
    raw_path, allow_home, allow_cloud, json_mode
):
    """Implement ``alive doctor --fix --world-root <path>`` recovery.

    Lexically normalize, run the canonical ``is_valid_world_root``
    predicate AND ``validate_path_choice`` (T2's policy), then write
    via T1's helper. Bypasses any stale-config brick state.

    Confirm-required categories require an explicit allow flag:
        * ``home`` -> ``--allow-home``
        * ``cloud`` -> ``--allow-cloud``

    ``deny`` refuses regardless of flags.

    Exit codes:
        0  -- write succeeded.
        1  -- validation failed in a way that's not a usage error
              (path is not a world root, predicate failed).
        2  -- usage error (confirm_required without the allow flag,
              deny category, normalization rejection).
    """
    from _world_root_io import (  # noqa: PLC0415
        is_valid_world_root,
        lexical_normalize_path,
        validate_path_choice,
        write_world_root_file,
    )

    # 1. Lexical normalization. Failures are usage errors -- the user
    # gave us a malformed path.
    try:
        normalized = lexical_normalize_path(raw_path)
    except (TypeError, ValueError) as exc:
        payload = {
            "error": "invalid --world-root path: {}".format(exc),
            "hint": (
                "supply an absolute, lexically-clean path (no ``~user``, "
                "no relative segments, no tab/newline characters)"
            ),
        }
        _emit(payload, json_mode, _render_text_error)
        return 2

    # 2. Policy check FIRST. ``deny`` paths (system roots, ``/``,
    # ``/Volumes``) MUST refuse with exit 2 regardless of whether the
    # path happens to look like a world root -- the spec's exit-code
    # contract says deny + confirm_required-without-flag are usage
    # errors (2), while predicate-failure is a non-permission fail (1).
    # Running the policy gate first preserves that distinction.
    decision = validate_path_choice(normalized)
    if decision.decision == "deny":
        payload = {
            "error": "refusing to pin {}: {}".format(
                normalized, decision.message
            ),
            "hint": (
                "system roots and bare ``/`` cannot host a world; pick "
                "a path in your home or a mounted volume"
            ),
        }
        _emit(payload, json_mode, _render_text_error)
        return 2
    if decision.decision == "confirm_required":
        category = decision.category
        if category == "home" and not allow_home:
            payload = {
                "error": "refusing to pin {}: {}".format(
                    normalized, decision.message
                ),
                "hint": (
                    "rerun with --allow-home to confirm pinning the "
                    "world root at your home directory"
                ),
            }
            _emit(payload, json_mode, _render_text_error)
            return 2
        if category == "cloud" and not allow_cloud:
            payload = {
                "error": "refusing to pin {}: {}".format(
                    normalized, decision.message
                ),
                "hint": (
                    "rerun with --allow-cloud to confirm pinning the "
                    "world root inside a cloud-sync directory"
                ),
            }
            _emit(payload, json_mode, _render_text_error)
            return 2
        # Unknown confirm-required category. Be conservative: refuse.
        if category not in ("home", "cloud"):
            payload = {
                "error": "refusing to pin {}: {}".format(
                    normalized, decision.message
                ),
                "hint": (
                    "this confirm_required category ({}) has no "
                    "matching allow flag; file an issue"
                    .format(category or "<empty>")
                ),
            }
            _emit(payload, json_mode, _render_text_error)
            return 2

    # 4. Predicate check. Only reached when policy is allow OR an
    # allow-flag overrode a confirm_required decision. A path that
    # doesn't look like a world root cannot be safely pinned -- the
    # resolver would surface every subsequent invocation as stale.
    # Exit code 1 (non-permission fail) per the spec; usage-error
    # paths (deny / confirm-required-without-flag) already returned
    # exit 2 above.
    if not is_valid_world_root(normalized):
        payload = {
            "error": (
                "{} is not a valid world root (no .alive/ marker and "
                "fewer than 2 domain dirs, OR path is missing / on an "
                "unmounted volume)".format(normalized)
            ),
            "hint": (
                "scaffold the path first via `/alive:world`, OR pick "
                "an existing world root"
            ),
        }
        _emit(payload, json_mode, _render_text_error)
        return 1

    # 5. Write via T1's helper. Atomic: a failure here leaves the
    # previous config-file state intact.
    try:
        write_world_root_file(normalized)
    except (OSError, ValueError) as exc:
        payload = {
            "error": (
                "failed to write ~/.config/alive/world-root: {}"
                .format(exc)
            ),
            "hint": (
                "check that ~/.config/alive/ is writable; if the dir "
                "is locked, ``mkdir -p ~/.config/alive`` first"
            ),
        }
        _emit(payload, json_mode, _render_text_error)
        return 1

    # ``degraded: False`` is honest here: the recovery path was
    # explicitly user-driven, no embedded ``check`` is included, and
    # the next invocation will resolve via config-file -> STATUS_OK.
    payload = {
        "fix": {
            "action": "wrote-config-file",
            "ok": True,
            "detail": (
                "Pinned world root to ~/.config/alive/world-root: {}"
                .format(normalized)
            ),
            "path": normalized,
        },
        "degraded": False,
    }
    _maybe_attach_migration_hint(payload)
    _emit(payload, json_mode, _render_text_fix)
    return 0


# ---------------------------------------------------------------------------
# Direct-invocation support (python3 scripts/doctor.py ...)
# ---------------------------------------------------------------------------

def _standalone_main(argv=None):
    """Allow ``python3 scripts/doctor.py ...`` for debugging.

    Not wired to the ``alive`` CLI -- that path goes through cli.py's
    dispatcher -- but keeps the module self-sufficient for ad-hoc
    triage and for the ``SCHEMA_METADATA`` import paths in T4.
    """
    parser = argparse.ArgumentParser(prog="alive-doctor")
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)
    # Force the subcommand so argparse resolves cleanly.
    args = parser.parse_args(["doctor"] + (list(argv) if argv else []))
    return handle(args)


if __name__ == "__main__":
    sys.exit(_standalone_main(sys.argv[1:]))
