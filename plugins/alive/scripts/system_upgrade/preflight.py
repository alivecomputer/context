"""Pre-flight guard chain for ``alive system-upgrade``.

Runs in the locked phase-1 order:

    0a. system-path policy gate (lexical pass)
    1.  resolve + realpath world_root
    1a. system-path policy gate (resolved pass) -- catches symlink bypass
    2.  ``.alive/`` symlink check (unconditional, no override flag)
    3.  submodule-walnut detection
    4.  UpgradeLock.acquire() (lock-meta write happens only AFTER 0a-3 pass)
    5.  dirty-stash check (post-lock so we don't compete with another upgrade)
    6.  Syncthing-active check
    7.  half-sync-marker check

Each guard refuses with a specific exit code class + JSON ``error_code``
per the table in the task spec. Override flags only bypass the
corresponding guard (none of them bypass the path-policy gates or the
``.alive`` symlink check).
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from _world_root_io import validate_path_choice


__all__ = (
    "EXIT_GENERAL",
    "EXIT_USAGE",
    "EXIT_NOT_FOUND",
    "EXIT_PERMISSION",
    "EXIT_LOCK_CONTENTION",
    "PreflightRefusal",
    "GuardResult",
    "run_path_policy_gate",
    "check_alive_symlink",
    "check_submodule_walnut",
    "check_dirty_stash",
    "check_syncthing_active",
    "check_half_sync_marker",
)


# Exit-code constants, mirrored from the bible's universal 5-level shape.
# Subcategories live in the JSON ``error_code`` field, not in expanded
# numeric exit codes.
EXIT_GENERAL = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_PERMISSION = 4
EXIT_LOCK_CONTENTION = 5


@dataclass(frozen=True)
class PreflightRefusal(Exception):
    """Structured refusal raised by a preflight guard.

    Attributes
    ----------
    exit_code : int
        Numeric exit code from the universal 5-level shape.
    error_code : str
        Subcategory string consumed by the JSON ``error_code`` field
        (e.g. ``unsafe_target:system_root:lexical``,
        ``boundary_violation:alive_must_be_real_directory``,
        ``dirty_stash``).
    message : str
        Human-readable explanation, suitable for stderr / JSON ``error``.
    confirm_path : str
        For ``unsafe_target_tty_confirm_required:*`` refusals, the
        EXACT path the operator must type back. Empty string for
        non-confirm refusals. The CLI uses this (NOT the lexical
        argv) for the type-back compare so a symlink-bypass case
        (lexical = ``~/work-world``, resolved = ``~/Dropbox/x``)
        cannot be satisfied by typing the benign symlink path.
    """

    exit_code: int
    error_code: str
    message: str
    confirm_path: str = ""

    def __post_init__(self) -> None:
        # dataclass(frozen=True) + Exception requires manual __init__
        # of Exception's args; do it after attribute assignment.
        Exception.__init__(self, self.message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


@dataclass(frozen=True)
class GuardResult:
    """Outcome of a single guard. ``ok=True`` means proceed."""

    name: str
    ok: bool
    refusal: Optional[PreflightRefusal] = None


# ---------------------------------------------------------------------------
# Path-policy gate (steps 0a + 1a)
# ---------------------------------------------------------------------------

def _is_existing_world(target: str) -> bool:
    """Return True if *target* contains a high-confidence world marker.

    Treats the presence of a ``.alive/`` directory at the path as proof
    that the path is an operating world (not a fresh setup target).
    Used by ``run_path_policy_gate`` to skip the home + cloud
    confirm-required gates when the operator is upgrading an existing
    world rather than initializing a new one.

    Filesystem access is intentional here -- ``validate_path_choice``
    is lexical-only by design; the upgrade-vs-setup distinction
    requires a stat. Best-effort: any OSError ("path doesn't exist
    yet," "permission denied") returns False so the gate falls
    through to its normal refusal path.
    """
    try:
        return os.path.isdir(os.path.join(target, ".alive"))
    except (OSError, TypeError, ValueError):
        return False


def run_path_policy_gate(
    target: str,
    pass_label: str,
    *,
    unsafe_confirm_target: bool = False,
    non_interactive: bool = False,
) -> None:
    """Run ``validate_path_choice`` and refuse on deny / confirm-required.

    Parameters
    ----------
    target:
        The path to validate. Step 0a passes the lexical input; step 1a
        passes the resolved path AFTER ``os.path.realpath``.
    pass_label:
        ``"lexical"`` or ``"resolved"`` -- embedded in the error_code
        so the operator knows which pass tripped the refusal.
    unsafe_confirm_target:
        ``True`` if the caller passed ``--unsafe-confirm-target``.
        Combined with TTY confirm in interactive mode OR sufficient
        alone in non-interactive mode; never bypasses ``deny``.
    non_interactive:
        ``True`` when ``--non-interactive`` was passed. Skips the TTY
        type-back loop; ``unsafe_confirm_target`` alone is sufficient.

    Raises
    ------
    PreflightRefusal
        On deny (always) or confirm-required without the bypass flag
        (or, in interactive mode, without a successful TTY confirm).
    """
    decision = validate_path_choice(target)
    if decision.decision == "allow":
        return

    if decision.decision == "deny":
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="unsafe_target:{}:{}".format(
                decision.category, pass_label
            ),
            message=(
                "refusing destructive op against {}: {} ({} pass)"
                .format(target, decision.message, pass_label)
            ),
        )

    # confirm_required
    #
    # The ``home`` and ``cloud`` categories are scatter / corruption
    # risk warnings that apply at SETUP time -- they exist so a new
    # user does not turn ``$HOME`` (or a cloud-sync subtree) into a
    # world by accident, ending up with ``01_Archive/`` etc as
    # top-level home directories. For an UPGRADE, the operator
    # already made that choice (potentially months/years ago). The
    # warning is correct in the abstract but moot in the specific:
    # there is nothing left to scatter; the world already exists at
    # this path.
    #
    # Skip the gate when the target is an existing world. Marker
    # presence is "the path contains a ``.alive/`` directory" --
    # the same probe the CLI's ``--world-root`` cwd-walking uses to
    # decide a high-confidence world hit. This is a filesystem
    # access (validate_path_choice itself is lexical-only); we do
    # it here in the gate so the policy module stays pure.
    if (
        decision.category in ("home", "cloud")
        and _is_existing_world(target)
    ):
        return
    if not unsafe_confirm_target:
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="unsafe_target_confirm_required:{}:{}".format(
                decision.category, pass_label
            ),
            message=(
                "refusing destructive op against {} ({} pass): {}; "
                "pass --unsafe-confirm-target (and confirm via TTY in "
                "interactive mode) to override."
                .format(target, pass_label, decision.message)
            ),
        )

    # unsafe-confirm-target was passed. Non-interactive mode is sufficient.
    if non_interactive:
        return

    # Interactive mode: a TTY type-back loop must happen at the call
    # site (the orchestrator owns input). We surface a refusal here
    # with a sentinel error_code so the orchestrator knows to prompt.
    # The orchestrator catches this, runs the prompt, and re-invokes
    # this function with non_interactive=True if the type-back matched.
    # The error_code is the sentinel; the message is intentionally
    # neutral about whether the operator is on a real TTY -- the
    # caller knows that and adds the right wording (TTY: prompt to
    # type the path back; non-TTY: tell the operator to pass
    # --non-interactive). Don't pre-judge here.
    raise PreflightRefusal(
        exit_code=EXIT_GENERAL,
        error_code="unsafe_target_tty_confirm_required:{}:{}".format(
            decision.category, pass_label
        ),
        message=(
            "destructive op against {} ({} pass) requires TTY type-back "
            "confirmation: {}".format(
                target, pass_label, decision.message,
            )
        ),
        # Critical: the path the operator must type back is THIS pass's
        # path -- NOT the lexical argv. Otherwise the resolved-pass
        # (symlink-bypass) refusal could be satisfied by typing the
        # benign-looking symlink path back. The CLI uses this field.
        confirm_path=str(target),
    )


# ---------------------------------------------------------------------------
# .alive symlink check (step 2, unconditional)
# ---------------------------------------------------------------------------

def check_alive_symlink(world_root_resolved: str) -> None:
    """Refuse if ``<world>/.alive`` is a symlink (any target) OR a non-dir.

    Bans ALL ``.alive`` symlinks, not just
    escapes. Rationale: ``.alive/`` is system-upgrade-owned state;
    allowing symlinks creates ambiguity about where backup tarballs
    and lock files actually land vs where the user thinks they land,
    and risks dangling-link breakage on subsequent runs.

    Equally, an existing ``.alive`` that is NOT a directory (a regular
    file, FIFO, device node, etc.) is rejected with the same boundary
    violation -- subsequent lock-meta writes target ``.alive/...``,
    which would fail with a confusing ``NotADirectoryError`` if we
    let the run proceed.

    No override flag bypasses these checks. ``--force`` is NOT honoured.

    Side effect: if ``.alive`` does not exist as ANY filesystem entity,
    create it as a real directory (precondition for the lock writes
    that ``--dry-run`` already allows -- see epic spec § ``--dry-run``
    semantics). Filesystem permission errors during the mkdir path
    surface as ``PreflightRefusal(exit_code=4, error_code=
    "permission:alive_mkdir")`` so the CLI can produce the documented
    exit-code-4 envelope rather than an unstructured exception.
    """
    alive_path = os.path.join(world_root_resolved, ".alive")
    if os.path.islink(alive_path):
        link_target = os.readlink(alive_path)
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="boundary_violation:alive_must_be_real_directory",
            message=(
                ".alive must be a real directory, not a symlink (target: {})"
                .format(link_target)
            ),
        )
    if os.path.exists(alive_path):
        if not os.path.isdir(alive_path):
            raise PreflightRefusal(
                exit_code=EXIT_GENERAL,
                error_code=(
                    "boundary_violation:alive_must_be_real_directory"
                ),
                message=(
                    ".alive exists but is not a directory ({}); "
                    "system-upgrade requires .alive/ to be a real "
                    "directory."
                    .format(alive_path)
                ),
            )
        return
    # .alive is missing -- create it. Use ``exist_ok=True`` so a
    # concurrent first-run race (two upgrades observe missing .alive,
    # one wins, the other would otherwise see FileExistsError) does
    # NOT mis-report as ``permission:alive_mkdir`` and skip the lock-
    # contention path that's supposed to fire at phase 4. The race
    # winner created the dir; the loser must still pass the
    # symlink/non-dir checks before returning, then continue to lock
    # acquisition where contention is properly surfaced.
    #
    # PermissionError / other OSError surface as the documented
    # exit-code-4 refusal so the CLI envelope is structured.
    try:
        os.makedirs(alive_path, mode=0o755, exist_ok=True)
    except PermissionError as exc:
        raise PreflightRefusal(
            exit_code=EXIT_PERMISSION,
            error_code="permission:alive_mkdir",
            message=(
                "permission denied creating {}: {}".format(alive_path, exc)
            ),
        ) from exc
    except OSError as exc:
        # ENOSPC, EROFS, ENOTDIR-on-parent, etc. Map to permission
        # bucket so retries gate on a consistent error_code; the
        # message preserves the underlying errno text.
        raise PreflightRefusal(
            exit_code=EXIT_PERMISSION,
            error_code="permission:alive_mkdir",
            message=(
                "filesystem error creating {}: {}".format(alive_path, exc)
            ),
        ) from exc

    # Race winner OR fresh create: re-validate the post-state. If
    # another process created .alive as something we don't accept
    # (symlink, non-dir) between our checks above and the makedirs
    # call, fail loud now rather than letting later writes encounter
    # a confusing NotADirectoryError.
    if os.path.islink(alive_path) or not os.path.isdir(alive_path):
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="boundary_violation:alive_must_be_real_directory",
            message=(
                ".alive at {} was concurrently created as a non-real-"
                "directory; refusing to proceed.".format(alive_path)
            ),
        )


# ---------------------------------------------------------------------------
# Submodule-walnut detection (step 3)
# ---------------------------------------------------------------------------

def check_submodule_walnut(world_root_resolved: str) -> None:
    """Refuse if ``world_root_resolved`` itself is a git submodule mount.

    Heuristic: ``world_root/.git`` is a *file* (not a directory), and
    the file's first line is ``gitdir: <relative-path>`` pointing at
    a parent's ``.git/modules/...``.
    """
    git_path = os.path.join(world_root_resolved, ".git")
    if not os.path.isfile(git_path):
        return
    try:
        with open(git_path, "r", encoding="utf-8") as f:
            first_line = f.readline().rstrip("\n")
    except OSError:
        return
    if not first_line.startswith("gitdir: "):
        return
    gitdir = first_line[len("gitdir: "):].strip()
    # Resolve relative to .git's parent (i.e. world_root).
    gitdir_resolved = os.path.realpath(
        os.path.join(world_root_resolved, gitdir)
    )
    # Match if the gitdir lives under any ``.git/modules/`` segment.
    parts = gitdir_resolved.split(os.sep)
    for i in range(len(parts) - 1):
        if parts[i] == ".git" and parts[i + 1] == "modules":
            raise PreflightRefusal(
                exit_code=EXIT_GENERAL,
                error_code="submodule_mount_refused",
                message=(
                    "{} is a git submodule mount (gitdir resolves under "
                    "{}); system-upgrade refuses to operate on submodule "
                    "walnuts because parent-repo state would be "
                    "implicitly mutated."
                    .format(world_root_resolved, gitdir_resolved)
                ),
            )


# ---------------------------------------------------------------------------
# Dirty session stash (step 5, post-lock)
# ---------------------------------------------------------------------------

def _scan_squirrel_yaml(home: Optional[str] = None) -> List[str]:
    """Return the list of squirrel YAML paths under ``~/.alive/_squirrels/``.

    Empty list when the directory does not exist.
    """
    if home is None:
        home = os.path.expanduser("~")
    base = os.path.join(home, ".alive", "_squirrels")
    if not os.path.isdir(base):
        return []
    return sorted(
        os.path.join(base, n) for n in os.listdir(base)
        if n.endswith(".yaml")
    )


def _frontmatter_value(text: str, key: str) -> Optional[str]:
    """Return the literal value of a top-level key in a YAML frontmatter.

    Stdlib regex-style scan (PyYAML banned). Walks line-by-line; matches
    ``<key>: <value>`` at column 0; returns the trimmed value or None
    if no match. Handles quoted strings minimally (strips paired quotes).
    """
    needle = "{}:".format(key)
    for raw in text.splitlines():
        if raw.startswith(needle):
            val = raw[len(needle):].strip()
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            return val
    return None


def _has_nonempty_stash(text: str) -> bool:
    """True iff the YAML's ``stash:`` list has at least one entry.

    Looks for a ``stash:`` line followed by at least one non-empty
    indented line beginning with ``-``. ``stash: []`` (inline empty
    list) is treated as empty.
    """
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        stripped = raw.rstrip()
        if stripped == "stash: []":
            return False
        if stripped == "stash:":
            # Look ahead for indented "- " entry.
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if not nxt.strip():
                    continue
                if not nxt.startswith(" ") and not nxt.startswith("\t"):
                    # End of stash block before any entry.
                    return False
                if nxt.lstrip().startswith("- "):
                    return True
            return False
    return False


def check_dirty_stash(
    world_root_resolved: str,
    *,
    force_dirty: bool = False,
    home: Optional[str] = None,
) -> None:
    """Refuse on a dirty session stash unless ``--force-dirty``.

    Scans ``~/.alive/_squirrels/*.yaml`` for entries where:
        ``walnut == basename(world_root_resolved)`` AND
        ``ended is None`` AND
        the ``stash`` list is non-empty.

    Any matching entry triggers refusal.
    """
    if force_dirty:
        return
    walnut_name = os.path.basename(world_root_resolved.rstrip("/"))
    for path in _scan_squirrel_yaml(home=home):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        # Cheap filter: walnut name must match.
        walnut_val = _frontmatter_value(text, "walnut")
        if walnut_val != walnut_name:
            continue
        ended_val = _frontmatter_value(text, "ended")
        # ``ended: null`` / ``ended:`` / no entry -> None
        if ended_val not in (None, "", "null", "~"):
            continue
        if not _has_nonempty_stash(text):
            continue
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="dirty_stash",
            message=(
                "active session stash for {} ({}); save or pass "
                "--force-dirty to proceed."
                .format(walnut_name, path)
            ),
        )


# ---------------------------------------------------------------------------
# Syncthing-active check (step 6)
# ---------------------------------------------------------------------------

def _stversions_recent_mtime(stversions: str, window_seconds: float) -> bool:
    """True iff any file under ``stversions`` has mtime within the window.

    Recursive walk; returns on first hit so a busy Syncthing world is
    detected cheaply.
    """
    cutoff = time.time() - window_seconds
    for root, _, files in os.walk(stversions):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                return True
    return False


def check_syncthing_active(
    world_root_resolved: str,
    *,
    syncthing_coordinated: bool = False,
    window_seconds: float = 60.0,
) -> None:
    """Refuse if Syncthing appears to be actively replicating the world.

    Heuristic:
        ``<world>/.stfolder`` is a directory AND
        ``<world>/.stversions`` is a directory AND
        a file under ``.stversions/`` has mtime within ``window_seconds``
        of now.

    ``--syncthing-coordinated`` bypasses (operator has paused sync).
    """
    if syncthing_coordinated:
        return
    stfolder = os.path.join(world_root_resolved, ".stfolder")
    stversions = os.path.join(world_root_resolved, ".stversions")
    if not (os.path.isdir(stfolder) and os.path.isdir(stversions)):
        return
    if _stversions_recent_mtime(stversions, window_seconds):
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="syncthing_active",
            message=(
                "Syncthing appears to be replicating {} actively "
                "(.stversions has files modified in the last {}s); "
                "pause sync and retry, or pass --syncthing-coordinated."
                .format(world_root_resolved, window_seconds)
            ),
        )


# ---------------------------------------------------------------------------
# Half-sync marker (step 7)
# ---------------------------------------------------------------------------

def check_half_sync_marker(
    world_root_resolved: str,
    *,
    force_incomplete_sync: bool = False,
) -> None:
    """Refuse on a half-sync marker unless ``--force-incomplete-sync``.

    Detection: either a top-level ``.syncthing-half-sync`` file exists,
    or any path under the world matches ``sync-conflict-*`` (recursive
    glob).
    """
    if force_incomplete_sync:
        return
    marker = os.path.join(world_root_resolved, ".syncthing-half-sync")
    if os.path.exists(marker):
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="half_sync_marker",
            message=(
                "{} present; sync is incomplete. Resolve the half-sync "
                "or pass --force-incomplete-sync.".format(marker)
            ),
        )
    pattern = os.path.join(world_root_resolved, "**", "sync-conflict-*")
    matches = glob.glob(pattern, recursive=True)
    if matches:
        raise PreflightRefusal(
            exit_code=EXIT_GENERAL,
            error_code="half_sync_marker",
            message=(
                "{} sync-conflict markers found under {} (e.g. {}); "
                "resolve or pass --force-incomplete-sync."
                .format(len(matches), world_root_resolved, matches[0])
            ),
        )


# ---------------------------------------------------------------------------
# High-level chain runner (used by orchestrator)
# ---------------------------------------------------------------------------

def run_pre_lock_chain(
    target_lexical: str,
    *,
    unsafe_confirm_target: bool = False,
    non_interactive: bool = False,
) -> str:
    """Run steps 0a + 1 + 1a + 2 + 3; return ``world_root_resolved``.

    Caller invokes ``UpgradeLock.acquire()`` after this returns and
    then runs ``run_post_lock_chain`` with the override flags.
    """
    # Step 0a
    run_path_policy_gate(
        target_lexical,
        pass_label="lexical",
        unsafe_confirm_target=unsafe_confirm_target,
        non_interactive=non_interactive,
    )
    # Step 1
    if not os.path.exists(target_lexical):
        raise PreflightRefusal(
            exit_code=EXIT_NOT_FOUND,
            error_code="missing_world",
            message="target path does not exist: {}".format(target_lexical),
        )
    if not os.path.isdir(target_lexical):
        raise PreflightRefusal(
            exit_code=EXIT_NOT_FOUND,
            error_code="missing_world",
            message="target is not a directory: {}".format(target_lexical),
        )
    world_root_resolved = os.path.realpath(target_lexical)
    # Step 1a
    run_path_policy_gate(
        world_root_resolved,
        pass_label="resolved",
        unsafe_confirm_target=unsafe_confirm_target,
        non_interactive=non_interactive,
    )
    # Step 2
    check_alive_symlink(world_root_resolved)
    # Step 3
    check_submodule_walnut(world_root_resolved)
    return world_root_resolved


def run_post_lock_chain(
    world_root_resolved: str,
    *,
    force_dirty: bool = False,
    syncthing_coordinated: bool = False,
    force_incomplete_sync: bool = False,
) -> None:
    """Run steps 5 + 6 + 7. Caller has already acquired the lock."""
    check_dirty_stash(
        world_root_resolved, force_dirty=force_dirty,
    )
    check_syncthing_active(
        world_root_resolved,
        syncthing_coordinated=syncthing_coordinated,
    )
    check_half_sync_marker(
        world_root_resolved,
        force_incomplete_sync=force_incomplete_sync,
    )
