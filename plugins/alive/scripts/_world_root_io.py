"""World-root persistence + canonical predicate (fn-15-la5.1, fn-15-la5.2).

This module is the canonical Python side of the world-resolution
contract introduced by epic fn-15-la5. It owns:

* The atomic config-file helper pair
  (``read_world_root_file`` / ``write_world_root_file``).
* The shared ``is_valid_world_root`` predicate.
* ``validate_world_root`` -- predicate plus per-OS unmount detection
  yielding a ``WorldRootStatus`` enum.
* ``describe_world_root`` -- a thin wrapper used by setup flows that
  want to surface *why* a candidate path is or is not a world root.
* ``validate_path_choice`` (fn-15-la5.2) -- the system-path policy
  validator that returns an allow/deny/confirm_required decision per
  the locked policy table. Reused by setup, ``alive doctor --fix
  --world-root``, and ``.walnut`` import.

Locked design constraints (do not regress these without a fresh
plan-review round):

* **Module layering.** Imports are stdlib + ``_atomic_io`` only. We do
  NOT import from ``_common`` to keep ``_world_root_io`` on a lower tier
  than ``_common``; a future task can migrate ``_common``'s atomic
  helper here, but the reverse direction would create a cycle.
* **Lexical-only path handling.** ``os.path.realpath`` is BANNED. All
  normalization is ``expanduser`` + ``abspath`` + ``normpath``. Symlink
  resolution is the consumer's choice at call time.
* **Stat-free mount detection.** Mount-parent detection is pure string
  analysis against a cached ``mount`` / ``/proc/mounts`` snapshot. Only
  after detection clears do we touch the filesystem with
  ``os.path.isdir`` / ``os.path.exists``.
* **Whitespace.** ``content.strip()`` only -- never collapse internal
  whitespace; paths can legitimately contain spaces.
* **Bash parity.** ``alive-common.sh`` carries a sibling implementation
  with byte-identical predicate output for every fixture case in
  ``tests/fixtures/world_root_predicate_cases.json``.
"""

from __future__ import annotations

import enum
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Tuple

from _atomic_io import atomic_write_text


__all__ = (
    "WorldRootStatus",
    "PathDecision",
    "ALIVE_CONFIG_PATH",
    "LEGACY_WALNUT_CONFIG_PATH",
    "WORLD_ROOT_DOMAIN_DIRS",
    "WALNUT_SCAN_DOMAIN_DIRS",
    "_WORLD_ROOT_DOMAIN_DIRS",
    "_WALNUT_SCAN_DOMAIN_DIRS",
    "is_valid_world_root",
    "validate_world_root",
    "validate_path_choice",
    "describe_world_root",
    "read_world_root_file",
    "write_world_root_file",
    "lexical_normalize_path",
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical config path. Tier-2 reads use this first.
ALIVE_CONFIG_PATH = "~/.config/alive/world-root"

#: Legacy pre-rename config path. Tier-2 reads fall back to this and
#: migrate to ``ALIVE_CONFIG_PATH`` on first hit.
LEGACY_WALNUT_CONFIG_PATH = "~/.config/walnut/world-root"


#: Direct-children set used by the world-root predicate. Includes
#: ``03_Inbox`` because at the world level, inbox IS a domain dir.
WORLD_ROOT_DOMAIN_DIRS: Tuple[str, ...] = (
    "01_Archive",
    "02_Life",
    "03_Inbox",
    "04_Ventures",
    "05_Experiments",
)

#: Walnut-scan set. Excludes ``03_Inbox`` because inbox is not a
#: walnut container -- referenced here so callers that today reach for
#: a generic "domain dirs" tuple can migrate to a clearly-named symbol.
WALNUT_SCAN_DOMAIN_DIRS: Tuple[str, ...] = (
    "01_Archive",
    "02_Life",
    "04_Ventures",
    "05_Experiments",
)

# Underscore-prefixed aliases retained verbatim so callers that index
# into the module-private name still resolve, and tests can assert on
# both forms. Both aliases share the same tuple object.
_WORLD_ROOT_DOMAIN_DIRS: Tuple[str, ...] = WORLD_ROOT_DOMAIN_DIRS
_WALNUT_SCAN_DOMAIN_DIRS: Tuple[str, ...] = WALNUT_SCAN_DOMAIN_DIRS

# Sanity assertion to surface accidental conflation; the bash sibling
# carries the same shape via two distinct array variables.
assert WORLD_ROOT_DOMAIN_DIRS is not WALNUT_SCAN_DOMAIN_DIRS  # nosec
assert WORLD_ROOT_DOMAIN_DIRS != WALNUT_SCAN_DOMAIN_DIRS  # nosec
assert _WORLD_ROOT_DOMAIN_DIRS is not _WALNUT_SCAN_DOMAIN_DIRS  # nosec


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class WorldRootStatus(str, enum.Enum):
    """Outcome of ``validate_world_root``.

    Inheriting ``str`` keeps the values JSON-serializable without a
    custom encoder; ``doctor --check=world-root`` reports the value
    string directly.
    """

    OK = "ok"
    MISSING_DIR = "missing_dir"
    MISSING_MARKER = "missing_marker"
    UNMOUNTED_VOLUME = "unmounted_volume"


# ---------------------------------------------------------------------------
# Lexical normalization
# ---------------------------------------------------------------------------


def lexical_normalize_path(path) -> str:
    """Lexically normalize a path. Pure string op (no fs touches).

    Order of operations:
        1. ``os.fspath`` + reject empty.
        2. ``os.path.expanduser`` -- ``~`` / ``~/`` only; ``~user`` is
           rejected (bash sibling rejects it; expanduser would leave
           it unchanged).
        3. Reject relative paths.
        4. Pre-check for ascend-past-root by walking segments. Required
           because ``os.path.normpath`` silently consumes leading
           ``/..`` (which would otherwise mask the error).
        5. ``os.path.abspath`` + ``os.path.normpath`` -- the canonical
           composition the spec calls for.
        6. Collapse a leading ``//`` (POSIX-preserved by ``normpath``)
           to ``/`` so the bash sibling stays in parity.

    Returns the normalized path string. Raises ``ValueError`` for the
    rejection cases.
    """
    raw = os.fspath(path)
    if raw == "":
        raise ValueError("empty path")
    # Reject paths containing tab / newline / CR. POSIX permits these
    # bytes in filenames, but they break every text-line / TSV
    # protocol the rest of this module emits over (config-file lines,
    # bash ``validate_path_choice`` 3-tab-field output, hook-shaped
    # JSON). Hard-rejecting at the normalization boundary keeps every
    # downstream surface safe by construction; the bash sibling
    # rejects the same characters in lock-step.
    for forbidden in ("\t", "\n", "\r"):
        if forbidden in raw:
            raise ValueError(
                "path contains forbidden control character (tab/newline/CR): {!r}"
                .format(raw)
            )
    # Reject ``~user`` based on the RAW token shape -- relying on
    # ``expanduser`` to leave the string unchanged is unreliable
    # because a real ``~root`` (or any existing user) would expand
    # successfully on the host and silently slip through. The bash
    # sibling rejects ``~user`` regardless of whether the user
    # exists on the host; we match that.
    if raw.startswith("~") and raw != "~" and not raw.startswith("~/"):
        raise ValueError("~user-style references are not supported: {!r}".format(raw))
    expanded = os.path.expanduser(raw)
    if expanded.startswith("~"):
        # Defensive: if expanduser left the leading ``~`` (e.g. HOME
        # is unset), the input is also ill-formed.
        raise ValueError("~ could not be expanded: {!r}".format(raw))
    if not os.path.isabs(expanded):
        raise ValueError("relative paths are not supported: {!r}".format(raw))

    # Pre-check for ascend-past-root. ``os.path.normpath`` silently
    # eats leading ``/..`` segments, which would mask the error.
    depth = 0
    for seg in expanded.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if depth == 0:
                raise ValueError(
                    "path ascends past root: {!r}".format(raw)
                )
            depth -= 1
            continue
        depth += 1

    normalized = os.path.normpath(os.path.abspath(expanded))
    # POSIX preserves a single leading ``//`` per its spec. Collapse
    # so the bash sibling (which collapses every ``//`` run) agrees.
    while normalized.startswith("//") and not normalized.startswith("///"):
        normalized = normalized[1:]
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


# ---------------------------------------------------------------------------
# Mount detection (pure-string against a cached snapshot)
# ---------------------------------------------------------------------------


_MOUNT_FSTAB_OCTAL = (
    ("\\040", " "),
    ("\\011", "\t"),
    ("\\012", "\n"),
    ("\\134", "\\"),
)


def _decode_fstab_octal(s: str) -> str:
    """Decode the four fstab-style octal escapes (``\\040`` etc).

    macOS ``mount`` and Linux ``/proc/mounts`` both use this encoding
    when a mount point contains whitespace or a backslash. The decode
    is idempotent on strings with no escapes (literal-space mountpoints
    pass through unchanged).
    """
    if "\\" not in s:
        return s
    out = s
    for esc, lit in _MOUNT_FSTAB_OCTAL:
        out = out.replace(esc, lit)
    return out


# Module-level cache: resolved once per process.
_MOUNT_POINTS_CACHE: Optional[List[str]] = None
# Linux: mountpoints whose filesystem type is ``autofs`` or whose
# source matches an unresponsive-fuse pattern. These are the path
# families that hang on stat the way ``/Volumes/<name>`` hangs on
# macOS. Cached alongside the regular mount-point list.
_LINUX_AUTOFS_ROOTS_CACHE: Optional[List[str]] = None
_MOUNT_PARSE_FAILED: bool = False


def _reset_mount_cache_for_tests() -> None:
    """Test-only hook to clear the mount-point cache."""
    global _MOUNT_POINTS_CACHE, _MOUNT_PARSE_FAILED, _LINUX_AUTOFS_ROOTS_CACHE
    _MOUNT_POINTS_CACHE = None
    _MOUNT_PARSE_FAILED = False
    _LINUX_AUTOFS_ROOTS_CACHE = None


def _read_mount_fixture(env_var: str) -> Optional[str]:
    """Return fixture content if ``env_var`` points at a readable file."""
    fixture_path = os.environ.get(env_var)
    if not fixture_path:
        return None
    try:
        with open(fixture_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _parse_macos_mount(text: str) -> List[str]:
    """Parse macOS ``mount`` output into a list of mount-point strings.

    Each line has shape ``<source> on <mountpoint> (<flags>)``. We
    locate the FIRST ``" on "`` (left boundary) and the LAST ``" ("``
    (right boundary) so that mountpoints containing parentheses or
    other delimiters survive. Backslash-escaped octal sequences are
    decoded post-extraction. Lines that do not match the shape are
    skipped silently.
    """
    points: List[str] = []
    for line in text.splitlines():
        if not line:
            continue
        on_idx = line.find(" on ")
        if on_idx < 0:
            continue
        paren_idx = line.rfind(" (")
        if paren_idx <= on_idx:
            continue
        mountpoint = line[on_idx + 4 : paren_idx]
        if not mountpoint:
            continue
        points.append(_decode_fstab_octal(mountpoint))
    return points


def _parse_linux_proc_mounts(text: str) -> Tuple[List[str], List[str]]:
    """Parse ``/proc/mounts`` into ``(all_mountpoints, autofs_roots)``.

    Each line has columns ``<source> <mountpoint> <fstype> ...``. The
    mountpoint (column 2) is decoded from fstab-style octal escapes
    unconditionally. Entries whose filesystem type is ``autofs`` or
    whose source/fstype matches an unresponsive-fuse pattern are
    additionally collected into ``autofs_roots`` -- those are the
    paths that hang on ``stat`` if their data sub-mount drops.
    """
    points: List[str] = []
    autofs_roots: List[str] = []
    for line in text.splitlines():
        if not line:
            continue
        cols = line.split()
        if len(cols) < 3:
            continue
        mountpoint = _decode_fstab_octal(cols[1])
        fstype = cols[2]
        source = cols[0]
        points.append(mountpoint)
        # ``autofs`` is the canonical hang case. Fuse mounts whose
        # source uses the ``fuse.<unresponsive>`` pattern (the spec's
        # phrasing) are also flagged as potentially hanging.
        if fstype == "autofs":
            autofs_roots.append(mountpoint)
        elif fstype.startswith("fuse.") and "unresponsive" in fstype.lower():
            # Per the locked spec, the "unresponsive" marker is
            # encoded in the fstype field (e.g.
            # ``fuse.<unresponsive-name>``). Source-field heuristics
            # are unreliable; fstype is authoritative.
            autofs_roots.append(mountpoint)
    return points, autofs_roots


def _load_mount_points() -> Optional[List[str]]:
    """Load (and cache) the live mount-point list for the current OS.

    Returns ``None`` on platforms / failure modes where mount detection
    is not applicable; callers MUST treat ``None`` as "skip detection,
    fall through to fs predicate" rather than as a rejection signal.

    ``mount`` is invoked WITHOUT a timeout per the locked "lexical-only
    / no timeouts" contract. Parse / read failures are caught and
    treated as "skip detection, treat as OK".
    """
    global _MOUNT_POINTS_CACHE, _MOUNT_PARSE_FAILED, _LINUX_AUTOFS_ROOTS_CACHE
    if _MOUNT_POINTS_CACHE is not None:
        return _MOUNT_POINTS_CACHE
    if _MOUNT_PARSE_FAILED:
        return None

    sysname = platform.system()
    try:
        if sysname == "Darwin":
            text = _read_mount_fixture("ALIVE_MOUNT_OUTPUT_FIXTURE")
            if text is None:
                # NO timeout per the locked contract. ``mount`` with
                # no args is a synchronous read of an in-kernel table
                # on macOS; in practice it returns instantly. If it
                # ever doesn't, the failure surfaces as a hung hook,
                # which the user can interrupt -- preferable to a
                # false ``UNMOUNTED_VOLUME`` reject.
                proc = subprocess.run(
                    ["mount"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                text = proc.stdout or ""
            points = _parse_macos_mount(text)
            if not points:
                # Empty parse -- treat as parse failure (best-effort
                # bias). Otherwise every /Volumes/<name> would be
                # treated as unmounted, bricking valid worlds.
                _MOUNT_PARSE_FAILED = True
                if os.environ.get("ALIVE_DEBUG"):
                    print(
                        "alive: empty mount output -- skipping "
                        "unmount detection",
                        file=sys.stderr,
                    )
                return None
            _MOUNT_POINTS_CACHE = points
            _LINUX_AUTOFS_ROOTS_CACHE = []
            return _MOUNT_POINTS_CACHE
        if sysname == "Linux":
            text = _read_mount_fixture("ALIVE_PROC_MOUNTS_FIXTURE")
            if text is None:
                with open("/proc/mounts", "r", encoding="utf-8") as f:
                    text = f.read()
            points, autofs_roots = _parse_linux_proc_mounts(text)
            if not points:
                _MOUNT_PARSE_FAILED = True
                if os.environ.get("ALIVE_DEBUG"):
                    print(
                        "alive: empty /proc/mounts -- skipping "
                        "unmount detection",
                        file=sys.stderr,
                    )
                return None
            _MOUNT_POINTS_CACHE = points
            _LINUX_AUTOFS_ROOTS_CACHE = autofs_roots
            return _MOUNT_POINTS_CACHE
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        if os.environ.get("ALIVE_DEBUG"):
            print(
                "alive: mount-detect parse failed ({}: {}) -- "
                "falling back to fs predicate".format(type(exc).__name__, exc),
                file=sys.stderr,
            )
        _MOUNT_PARSE_FAILED = True
        return None

    # Windows / Git Bash / unknown -- skip detection entirely.
    _MOUNT_POINTS_CACHE = []  # cache-as-empty so we don't re-probe
    _LINUX_AUTOFS_ROOTS_CACHE = []
    return _MOUNT_POINTS_CACHE


def _is_under_unmounted_volume(path: str) -> bool:
    """Pure-string mount-parent check on a lexical path.

    Returns True iff:
        * The platform is macOS AND the path is under
          ``/Volumes/<name>`` AND ``/Volumes/<name>`` is NOT in the
          cached mount-point list.
        * OR the platform is Linux AND any ancestor of the path
          appears in ``/proc/mounts`` with an autofs / unresponsive
          fuse filesystem and is NOT currently in the mount list.

    ``False`` covers all of: non-Volumes paths on macOS, paths whose
    parent volume IS mounted, every path on Windows, and the
    "best-effort treat as OK" fallback when ``mount`` parsing failed.
    """
    sysname = platform.system()
    if sysname not in ("Darwin", "Linux"):
        return False

    mount_points = _load_mount_points()
    if mount_points is None:
        # Parse failed -- bias toward "let it through" per the locked
        # best-effort policy.
        return False

    if sysname == "Darwin":
        if not path.startswith("/Volumes/"):
            return False
        # Scope: ``/Volumes/<name>`` parent. Anything deeper is
        # considered "under the same volume root" and is rejected iff
        # ``<name>`` is missing from the live mount list.
        rest = path[len("/Volumes/") :]
        # Take the first segment; absent any '/' the whole thing is
        # the volume name.
        slash = rest.find("/")
        volume_name = rest if slash < 0 else rest[:slash]
        if not volume_name:
            return False
        candidate = "/Volumes/" + volume_name
        return candidate not in mount_points

    # Linux: detect paths whose parent autofs root is in /proc/mounts
    # but whose immediate sub-mount under that root is NOT. Mirrors
    # the macOS ``/Volumes/<name>`` semantics:
    #
    #     autofs_root = /net   (in /proc/mounts as fstype=autofs)
    #     path        = /net/host/share
    #     candidate   = /net/host
    #     reject iff candidate is NOT a live mount point
    #
    # Paths NOT under any autofs root skip detection entirely. This
    # narrow scope keeps the false-positive blast radius small.
    autofs_roots = _LINUX_AUTOFS_ROOTS_CACHE or []
    for root in autofs_roots:
        if not root:
            continue
        if path == root:
            return False
        if path.startswith(root + "/"):
            rest = path[len(root) + 1 :]
            slash = rest.find("/")
            sub_name = rest if slash < 0 else rest[:slash]
            if not sub_name:
                return False
            candidate = root + "/" + sub_name
            if candidate not in mount_points:
                return True
    return False


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def _read_symlink_target(child: str) -> Optional[str]:
    """Return the lexically-normalized symlink target for ``child``.

    Returns ``None`` when ``child`` is not a symlink (or when reading
    the link failed -- treated as "not a symlink" so the caller falls
    through to the regular ``isdir`` probe).
    """
    try:
        if not os.path.islink(child):
            return None
        target = os.readlink(child)
    except OSError:
        return None
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(os.path.dirname(child), target))
    else:
        target = os.path.normpath(target)
    return target


def _child_is_present_dir(child: str) -> bool:
    """Per-child probe used by the predicate.

    Sequence (locked):
        1. ``islink(child)`` (lstat -- safe even on dead symlinks).
        2. If symlink: read the target lexically, then run mount-parent
           detection on the target string. If unmounted, treat the
           child as absent (do not count toward predicate tally).
        3. If not a symlink (or target is on a mounted volume), call
           ``isdir(child)``. ``isdir`` follows symlinks but cannot hang
           because we have already verified the target volume.
    """
    target = _read_symlink_target(child)
    if target is not None and _is_under_unmounted_volume(target):
        return False
    try:
        return os.path.isdir(child)
    except OSError:
        return False


def is_valid_world_root(path) -> bool:
    """Canonical predicate: does ``path`` look like a world root?

    True iff ``path`` is a directory AND
    (``path/.alive`` exists as a directory OR
    at least 2 of ``WORLD_ROOT_DOMAIN_DIRS`` exist as direct children).

    Symlinked children are probed without hanging on unmounted
    targets; see ``_child_is_present_dir``.
    """
    try:
        normalized = lexical_normalize_path(path)
    except (TypeError, ValueError):
        return False

    # Mount-parent check on the candidate path itself before any fs
    # touches.
    if _is_under_unmounted_volume(normalized):
        return False

    # If the path itself is a symlink, mount-detect the target before
    # touching ``isdir``.
    target = _read_symlink_target(normalized)
    if target is not None and _is_under_unmounted_volume(target):
        return False

    if not os.path.isdir(normalized):
        return False

    alive_marker = os.path.join(normalized, ".alive")
    if _child_is_present_dir(alive_marker):
        return True

    count = 0
    for domain in WORLD_ROOT_DOMAIN_DIRS:
        if _child_is_present_dir(os.path.join(normalized, domain)):
            count += 1
            if count >= 2:
                return True
    return False


def validate_world_root(path) -> WorldRootStatus:
    """Diagnose the failure mode for a candidate world-root path.

    Returns:
        OK -- predicate passes.
        UNMOUNTED_VOLUME -- path (or its symlink target) lives under
            an unmounted ``/Volumes/<name>`` and would hang ``stat``.
        MISSING_DIR -- path is not a directory.
        MISSING_MARKER -- path is a directory but lacks both a
            ``.alive/`` marker and >=2 domain children.
    """
    try:
        normalized = lexical_normalize_path(path)
    except (TypeError, ValueError):
        return WorldRootStatus.MISSING_DIR

    if _is_under_unmounted_volume(normalized):
        return WorldRootStatus.UNMOUNTED_VOLUME

    target = _read_symlink_target(normalized)
    if target is not None and _is_under_unmounted_volume(target):
        return WorldRootStatus.UNMOUNTED_VOLUME

    if not os.path.isdir(normalized):
        return WorldRootStatus.MISSING_DIR

    alive_marker = os.path.join(normalized, ".alive")
    if _child_is_present_dir(alive_marker):
        return WorldRootStatus.OK

    count = 0
    for domain in WORLD_ROOT_DOMAIN_DIRS:
        if _child_is_present_dir(os.path.join(normalized, domain)):
            count += 1
            if count >= 2:
                return WorldRootStatus.OK
    return WorldRootStatus.MISSING_MARKER


def describe_world_root(path) -> Tuple[WorldRootStatus, str]:
    """World-root status plus a human-readable explanation.

    Returns ``(status, message)`` where ``message`` is suitable for
    surfacing to the human in setup, doctor, or bridge flows. The
    historical name was ``validate_path_choice`` (T1); fn-15-la5.2
    repurposes that identifier for the system-path policy validator,
    so this diagnostic helper lives under its own clearer name.
    """
    status = validate_world_root(path)
    try:
        normalized = lexical_normalize_path(path)
    except (TypeError, ValueError):
        normalized = str(path)

    if status is WorldRootStatus.OK:
        msg = "{} looks like a world root".format(normalized)
    elif status is WorldRootStatus.UNMOUNTED_VOLUME:
        msg = (
            "{} lives on an unmounted volume; mount the disk and retry"
            .format(normalized)
        )
    elif status is WorldRootStatus.MISSING_DIR:
        msg = "{} is not a directory".format(normalized)
    else:
        msg = (
            "{} exists but is not a world root "
            "(no .alive/ marker and fewer than 2 domain dirs)"
            .format(normalized)
        )
    return status, msg


# ---------------------------------------------------------------------------
# System-path policy (fn-15-la5.2)
# ---------------------------------------------------------------------------


class PathDecision(NamedTuple):
    """Structured decision returned by ``validate_path_choice``.

    * ``decision`` -- one of ``"allow"``, ``"deny"``, ``"confirm_required"``.
    * ``category`` -- machine-readable category string. ``""`` for
      ``allow`` (no category needed when the path is fine). For
      ``deny``: ``"filesystem_root"`` (bare ``/``) or ``"system_root"``
      (``/tmp``, ``/etc``, ``/Volumes`` exact, etc). For
      ``confirm_required``: ``"home"`` (bare ``$HOME``) or ``"cloud"``
      (iCloud / Dropbox / GDrive subtrees).
    * ``message`` -- human-readable explanation suitable for surfacing
      via AskUserQuestion type-back loops or doctor `exit 2` errors.
    * ``normalized`` -- the lexically-normalized form of the input
      path (or the raw input when normalization rejected it). Setup
      and doctor flows store this rather than the raw user input.
    """

    decision: str
    category: str
    message: str
    normalized: str


# Hard-deny system root subtrees. ``/`` is NOT in this set -- bare ``/``
# is the only filesystem-root entry and matches EXACTLY (descendants of
# ``/`` are not denied because that would deny every absolute path).
# ``/Volumes`` is also NOT in this set -- it matches exact-only so that
# ``/Volumes/MyDisk`` (a mounted disk) can fall through to ``allow``.
#
# Windows / Git Bash forms: MSYS / Git-Bash mounts ``C:\Windows`` as
# ``/c/Windows`` (lowercase) and ``/C/Windows`` (uppercase) in the
# normalized POSIX path; both are listed below. Native Windows paths
# (``C:\Windows``, ``C:\\Windows``, ``C:/Windows``) are matched
# separately by ``_matches_windows_system_root`` so the policy reaches
# every shape Windows callers can produce, regardless of normalization.
_DENY_SUBTREE_ROOTS: Tuple[str, ...] = (
    "/tmp",
    "/etc",
    "/var",
    "/usr",
    "/bin",
    "/sbin",
    "/opt",
    "/private",
    "/Library",  # the SYSTEM /Library, not user ~/Library
    "/System",
    "/Applications",
    "/c/Windows",
    "/C/Windows",
    "/c/Program Files",
    "/C/Program Files",
)


def _matches_windows_system_root(raw_input: str) -> bool:
    """True iff ``raw_input`` is a native-Windows system root.

    Matches ``C:\\Windows``, ``C:/Windows``, ``C:\\Program Files``,
    and their subtrees, in either case (drive letter is
    case-insensitive on Windows). Operates on the RAW input rather
    than the lexically-normalized form because ``lexical_normalize_path``
    targets POSIX shapes and would not produce the same canonical form
    on Windows. The bash sibling skips this check (Git Bash callers
    hit the ``/c/Windows`` MSYS forms above instead).
    """
    if not raw_input or len(raw_input) < 3:
        return False
    # Tolerate a leading drive letter regardless of case.
    drive = raw_input[0]
    if not (drive.isalpha() and raw_input[1] == ":"):
        return False
    sep = raw_input[2]
    if sep not in ("\\", "/"):
        return False
    rest = raw_input[3:]
    # Normalize remaining separators to "/" for the prefix check.
    rest_unified = rest.replace("\\", "/")
    # Strip a leading "/" from rest_unified if present so we compare
    # against a single canonical form. (Windows allows ``C:\Windows``
    # and ``C:Windows``; only the former rooted form is a system path.)
    rest_stripped = rest_unified.lstrip("/")
    for forbidden in ("Windows", "Program Files"):
        if rest_stripped == forbidden:
            return True
        if rest_stripped.startswith(forbidden + "/"):
            return True
    return False

# Confirm-required cloud-sync subtrees. ``$HOME`` is filled in at call
# time. iCloud / Dropbox / GDrive are common cases on macOS; the
# Linux equivalents (``~/Dropbox``) match the same user-relative
# path. The Google Drive entry uses a prefix because the directory
# name embeds the user's email (``GoogleDrive-foo@example.com``); we
# match anything starting with that prefix.
def _cloud_subtree_roots(home: str) -> Tuple[Tuple[str, str], ...]:
    """Return (root_path, sublabel) tuples for cloud-sync detection.

    Match semantics are subtree (path-segment prefix-or-equal). Note
    Google Drive is NOT included here: its on-disk directory name
    embeds the user's email
    (``~/Library/CloudStorage/GoogleDrive-foo@example.com``), which
    is matched separately by ``_matches_gdrive_prefix`` -- a literal
    subtree match would have to enumerate live filenames, and a
    blanket subtree match on ``~/Library/CloudStorage`` would
    incorrectly flag non-Google providers (OneDrive, ProtonDrive,
    etc) that share the same parent directory.
    """
    if not home:
        return ()
    return (
        (home + "/Library/Mobile Documents", "iCloud"),
        (home + "/Dropbox", "Dropbox"),
    )


# Google Drive lives at ``~/Library/CloudStorage/GoogleDrive-<email>``.
# Match by checking whether the path is at-or-under any
# ``~/Library/CloudStorage/GoogleDrive-<*>`` directory, where ``<*>``
# is a single path segment. We avoid both: a wildcard subtree match
# on ``~/Library/CloudStorage`` (would catch OneDrive, ProtonDrive,
# Box, etc), AND enumerating live filenames (we never touch the
# filesystem in this validator).
_GDRIVE_DIR_PREFIX = "/Library/CloudStorage/GoogleDrive-"


def _matches_gdrive_prefix(path: str, home: str) -> bool:
    """True iff ``path`` is under ``~/Library/CloudStorage/GoogleDrive-<*>``.

    Pure string match against a path-segment boundary -- never a
    substring or glob. The bash sibling implements the equivalent
    logic via parameter expansion + ``case``.
    """
    if not home:
        return False
    base = home + _GDRIVE_DIR_PREFIX
    if not path.startswith(base):
        return False
    # Everything after the prefix up to the next '/' is the
    # GoogleDrive-<email> segment; whatever comes after is fine.
    rest = path[len(base):]
    if rest == "":
        # Empty suffix is impossible here because the prefix ends
        # in a hyphen and a path can't end with a hyphen-suffixed
        # segment of zero length, but be defensive.
        return False
    # Confirm the segment shape: the very next character must be a
    # non-empty, non-slash run, optionally followed by '/'-and-more.
    # In practice this just means: the prefix is a non-zero suffix
    # before any slash. Reject the degenerate "just a slash" case.
    first_char = rest[0]
    if first_char == "/":
        return False
    return True


def _is_subtree(path: str, root: str) -> bool:
    """``path == root`` OR ``path`` starts with ``root + "/"``.

    Pure string match -- both inputs MUST be lexically normalized
    before reaching this helper (no trailing slash, no ``..``, etc).
    Substring matching is explicitly NOT used; this is a path-segment
    boundary check.
    """
    if path == root:
        return True
    return path.startswith(root + "/")


def validate_path_choice(path, home: Optional[str] = None) -> PathDecision:
    """System-path policy validator (fn-15-la5.2).

    Returns a ``PathDecision`` describing whether the candidate path
    is safe to use as a world root. Per the locked policy table:

    * ``deny`` for filesystem root and system roots (``/``, ``/tmp``,
      ``/etc``, ..., and ``/Volumes`` itself but NOT ``/Volumes/<name>``).
    * ``confirm_required`` for ``$HOME`` exactly and for cloud-sync
      subtrees (iCloud, Dropbox, GDrive).
    * ``allow`` for everything else.

    Match algorithm (locked):
        1. Hard-deny exact (``/``, ``/Volumes``).
        2. Hard-deny subtree (``/tmp``, ``/etc``, ...).
        3. Confirm-required exact (``$HOME``).
        4. Confirm-required subtree (cloud-sync roots).
        5. Allow.

    First match wins. Subtree means ``path == root`` or
    ``path.startswith(root + "/")`` after BOTH have been lexically
    normalized -- never via substring match or glob.

    Args:
        path: candidate path. Tilde expansion + lexical normalization
            applied before matching. Inputs that fail to normalize
            (relative, ascend-past-root, ``~user``) return
            ``deny / system_root`` with a normalization-error message.
        home: explicit home dir (test seam). Defaults to
            ``os.path.expanduser("~")`` -- production callers should
            leave this unset.

    Returns:
        ``PathDecision`` with ``decision``, ``category``, ``message``,
        and ``normalized`` fields. ``category`` is ``""`` for
        ``allow``.
    """
    raw = "" if path is None else str(path)

    # Native-Windows system roots are matched against the RAW input
    # because lexical_normalize_path is POSIX-shaped. We do this before
    # normalization so a `C:\Windows`-style input doesn't slip through
    # to the relative-path rejection branch.
    if _matches_windows_system_root(raw):
        return PathDecision(
            decision="deny",
            category="system_root",
            message=(
                "{} lives under a Windows system root; pick a path "
                "outside C:\\Windows / C:\\Program Files."
                .format(raw)
            ),
            normalized=raw,
        )

    try:
        normalized = lexical_normalize_path(path)
    except (TypeError, ValueError) as exc:
        # Inputs that don't normalize cannot be classified by the
        # policy table -- treat as a hard deny so the caller surfaces
        # the underlying ValueError to the human.
        return PathDecision(
            decision="deny",
            category="system_root",
            message=(
                "{!r} is not a valid path: {}".format(raw, exc)
            ),
            normalized=raw,
        )

    if home is None:
        home = os.path.expanduser("~")
    # Resolve $HOME through the same lexical pipeline so comparisons
    # against the candidate path are apples-to-apples (a $HOME like
    # "/Users/me/" or "/Users/me/." would otherwise miss).
    home_norm = ""
    if home:
        try:
            home_norm = lexical_normalize_path(home)
        except (TypeError, ValueError):
            home_norm = ""

    # 1. Hard-deny exact paths: bare "/" and bare "/Volumes".
    if normalized == "/":
        return PathDecision(
            decision="deny",
            category="filesystem_root",
            message=(
                "{} is the filesystem root and cannot host a world."
                .format(normalized)
            ),
            normalized=normalized,
        )
    if normalized == "/Volumes":
        return PathDecision(
            decision="deny",
            category="system_root",
            message=(
                "{} is the volumes mount directory; pick a specific "
                "volume like /Volumes/<name>/alive instead."
                .format(normalized)
            ),
            normalized=normalized,
        )

    # 2. Hard-deny subtree.
    for root in _DENY_SUBTREE_ROOTS:
        if _is_subtree(normalized, root):
            return PathDecision(
                decision="deny",
                category="system_root",
                message=(
                    "{} lives under the system root {}; pick a path "
                    "in your home or a mounted volume instead."
                    .format(normalized, root)
                ),
                normalized=normalized,
            )

    # 3. Confirm-required exact: bare $HOME.
    if home_norm and normalized == home_norm:
        return PathDecision(
            decision="confirm_required",
            category="home",
            message=(
                "{} is your home directory. Setting up a world here "
                "scatters domain folders across your home -- type the "
                "path back exactly to confirm, or pick a subdirectory "
                "like {}/alive instead."
                .format(normalized, normalized)
            ),
            normalized=normalized,
        )

    # 4. Confirm-required subtree: cloud-sync roots.
    for root, label in _cloud_subtree_roots(home_norm):
        if _is_subtree(normalized, root):
            return PathDecision(
                decision="confirm_required",
                category="cloud",
                message=(
                    "{} is inside a cloud-sync directory ({}). "
                    "Cloud sync can corrupt atomic writes and replicate "
                    "private context. Type the path back exactly to "
                    "confirm, or pick a non-synced location."
                    .format(normalized, label)
                ),
                normalized=normalized,
            )

    # Google Drive lives under ``~/Library/CloudStorage/GoogleDrive-<email>``.
    # Matched separately so we don't blanket-flag every other provider
    # (OneDrive, ProtonDrive, Box) sharing ``~/Library/CloudStorage``.
    if _matches_gdrive_prefix(normalized, home_norm):
        return PathDecision(
            decision="confirm_required",
            category="cloud",
            message=(
                "{} is inside Google Drive (~/Library/CloudStorage/"
                "GoogleDrive-<email>). Cloud sync can corrupt atomic "
                "writes and replicate private context. Type the path "
                "back exactly to confirm, or pick a non-synced location."
                .format(normalized)
            ),
            normalized=normalized,
        )

    # 5. Allow.
    return PathDecision(
        decision="allow",
        category="",
        message="{} is allowed.".format(normalized),
        normalized=normalized,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _resolve_config_path(env_path: str) -> str:
    """Expand ``~`` in a config-path constant. Pure string op."""
    return os.path.expanduser(env_path)


def _read_persisted_path_strict(file_path: str) -> Optional[str]:
    """Read and validate the on-disk content of a config file.

    Returns the normalized path string on success, ``None`` if the
    file is missing. Raises ``ValueError`` for corrupt content
    (multi-line, empty after strip, relative).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    # Strip leading/trailing whitespace ONLY; internal whitespace is
    # preserved because paths can legitimately contain spaces.
    stripped = raw.strip()
    if not stripped:
        raise ValueError(
            "world-root config file is empty after strip: {}".format(file_path)
        )

    non_empty_lines = [
        line for line in stripped.splitlines() if line.strip() != ""
    ]
    if len(non_empty_lines) != 1:
        raise ValueError(
            "world-root config file must contain exactly one path: {} "
            "(found {} non-empty lines)".format(file_path, len(non_empty_lines))
        )

    candidate = non_empty_lines[0]
    # Tilde is tolerated on read (hand-edits). ``lexical_normalize_path``
    # rejects ``~user`` and pure relatives.
    return lexical_normalize_path(candidate)


def read_world_root_file() -> Optional[Path]:
    """Tier-2 read with legacy walnut-config migration.

    Order:
        1. ``~/.config/alive/world-root``: if present, parse + return
           when ``validate_world_root`` is OK; return ``None`` when the
           stored path is invalid (missing dir, missing marker,
           unmounted volume).
        2. Else ``~/.config/walnut/world-root``: if present, attempt
           to migrate to (1) via ``write_world_root_file``.
           - On migration WRITE SUCCESS: return the legacy path
             (already validated).
           - On migration WRITE FAILURE: surface
             ``WORLD_ROOT_FAIL_REASON=migration_write_failed`` via
             the env var (advisory only -- caller still gets the
             legacy path back).
        3. Else: return ``None`` so the resolver falls through to
           tier-3 bootstrap.

    Raises ``ValueError`` on corrupt content (delegated from
    ``_read_persisted_path_strict``). Returns ``None`` on missing or
    invalid stored path.
    """
    # Always clear the fail reason before each call so stale state
    # from a prior process or test does not leak.
    os.environ.pop("WORLD_ROOT_FAIL_REASON", None)

    alive_path = _resolve_config_path(ALIVE_CONFIG_PATH)
    primary = _read_persisted_path_strict(alive_path)
    if primary is not None:
        if validate_world_root(primary) == WorldRootStatus.OK:
            return Path(primary)
        # Stored path is no longer a world root -- treat as missing
        # so the resolver can fall through to subsequent tiers. Tests
        # that care about the failure mode use ``validate_world_root``
        # directly.
        return None

    legacy_path = _resolve_config_path(LEGACY_WALNUT_CONFIG_PATH)
    legacy = _read_persisted_path_strict(legacy_path)
    if legacy is None:
        return None

    # Even if the legacy path no longer validates (e.g. unmounted
    # disk), we still attempt the migration write so the alive-config
    # path is populated for next boot. The return value, however,
    # follows the same "validate or None" rule as primary.
    legacy_status = validate_world_root(legacy)

    # Attempt migration. On failure, surface the advisory.
    migration_failed = False
    try:
        write_world_root_file(Path(legacy))
    except OSError:
        migration_failed = True

    if migration_failed:
        os.environ["WORLD_ROOT_FAIL_REASON"] = "migration_write_failed"
        if legacy_status == WorldRootStatus.OK:
            return Path(legacy)
        return None

    if legacy_status == WorldRootStatus.OK:
        return Path(legacy)
    return None


def write_world_root_file(path) -> None:
    """Atomically persist ``path`` to ``~/.config/alive/world-root``.

    Validation rules:
        * The stored content is the LEXICALLY-NORMALIZED path
          (``expanduser`` + ``abspath`` + ``normpath``). Tilde is
          rejected on write (writers expand first); relative paths are
          rejected.
        * Trailing newline is added on write.

    Atomic-write protocol is delegated to
    ``_atomic_io.atomic_write_text`` (mode 0600, parent_mode 0700).
    """
    normalized = lexical_normalize_path(path)
    if normalized.startswith("~"):
        # Defensive: ``lexical_normalize_path`` never returns this
        # shape for inputs we accept, but a future change could make
        # it slip through. Rejecting at the storage boundary is cheap.
        raise ValueError("stored content must not begin with '~': {!r}".format(path))
    if not os.path.isabs(normalized):
        raise ValueError("stored content must be absolute: {!r}".format(path))

    target = _resolve_config_path(ALIVE_CONFIG_PATH)
    atomic_write_text(target, normalized + "\n", mode=0o600, parent_mode=0o700)


# ---------------------------------------------------------------------------
# Test helpers (private, surfaced for hermetic tests)
# ---------------------------------------------------------------------------


def _domain_iter() -> Iterable[str]:
    """Iterator over ``WORLD_ROOT_DOMAIN_DIRS``. Test convenience."""
    return iter(WORLD_ROOT_DOMAIN_DIRS)
