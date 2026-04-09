#!/usr/bin/env python3
"""ALIVE Context System -- v3 P2P sharing layer (foundations).

Cross-platform stdlib-only library and CLI for the ALIVE v3 P2P sharing layer.
This file is the layout-agnostic foundation half of the v3 rewrite (epic
fn-7-7cw): hashing, tar I/O, atomic JSON state, OpenSSL detection, base64,
YAML frontmatter parsing, package manifest parser, signature signing /
verification, generic file staging helpers, and package extraction.

The v3-aware halves -- staging dispatch for flat-bundle walnuts, manifest
generation with the ``source_layout`` hint, top-level ``create_package``,
``validate_manifest`` accepting any 2.x format version, and the user-facing CLI
-- land in subsequent fn-7-7cw tasks (.4 and .5). This file deliberately stops
short of those so the foundation can be reviewed in isolation.

Designed for macOS (BSD tar, LibreSSL) and Linux (GNU tar, OpenSSL). Honors
``COPYFILE_DISABLE=1`` to suppress macOS resource forks. Uses the openssl CLI
(NOT Python ``cryptography``) per the walnut-authoritative crypto decision and
LD5 of the epic spec (LibreSSL pbkdf2 detection + ``-md sha256`` legacy
fallback for v2 packages).

Python floor: 3.9. Type hints use the ``typing`` module (``Optional``,
``List``, ``Dict``, ``Tuple``, ``Any``); PEP 604 unions and PEP 585 builtin
generics are NOT used (LD22).
"""

import base64
import datetime
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

# v3 walnut path helpers (vendored per LD10 to avoid importing underscored
# privates from tasks.py / project.py). The import is wrapped so the file can
# still be byte-compiled in environments where ``walnut_paths`` is not yet on
# the path -- the v3-aware tasks (.4 / .5) will rely on the symbol existing.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import walnut_paths  # noqa: F401  (referenced by v3 staging in task .4)
except ImportError:  # pragma: no cover -- defensive only
    walnut_paths = None  # type: ignore


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha256_file(path):
    # type: (str) -> str
    """Return hex SHA-256 digest of a file. Cross-platform, no subprocess."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Tar operations
# ---------------------------------------------------------------------------

# Files and patterns to exclude from archives
_TAR_EXCLUDES = {".DS_Store", "Thumbs.db", "Icon\r", "__MACOSX"}


def _is_excluded(name):
    # type: (str) -> bool
    """Check whether a tar entry name should be excluded."""
    base = os.path.basename(name)
    if base in _TAR_EXCLUDES:
        return True
    # macOS resource fork files
    if base.startswith("._"):
        return True
    return False


def _resolve_path(base, name):
    # type: (str, str) -> Optional[str]
    """Resolve *name* relative to *base* and check it stays inside *base*.

    Returns the resolved absolute path, or None if the entry escapes.
    """
    # Reject absolute paths outright
    if os.path.isabs(name):
        return None
    target = os.path.normpath(os.path.join(base, name))
    # Must start with base (use trailing sep to avoid prefix tricks)
    if not (target == base or target.startswith(base + os.sep)):
        return None
    return target


def safe_tar_create(source_dir, output_path, strip_prefix=None):
    # type: (str, str, Optional[str]) -> None
    """Create a tar.gz archive from *source_dir*.

    - Sets ``COPYFILE_DISABLE=1`` to suppress macOS resource forks.
    - Excludes ``.DS_Store``, ``Thumbs.db``, ``._*`` files.
    - Rejects symlinks that resolve outside *source_dir*.
    - Optional *strip_prefix* removes a leading path component from entries.
    """
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        raise FileNotFoundError("Source directory not found: {0}".format(source_dir))

    # Suppress macOS resource forks (affects C-level tar inside python too)
    os.environ["COPYFILE_DISABLE"] = "1"

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with tarfile.open(output_path, "w:gz") as tar:
        for root, dirs, files in os.walk(source_dir):
            # Skip excluded directories in-place
            dirs[:] = [
                d for d in dirs
                if d not in _TAR_EXCLUDES and not d.startswith("._")
            ]

            for name in sorted(files):
                if _is_excluded(name):
                    continue

                full_path = os.path.join(root, name)

                # Reject symlinks that escape source_dir
                if os.path.islink(full_path):
                    real = os.path.realpath(full_path)
                    if not (real == source_dir
                            or real.startswith(source_dir + os.sep)):
                        raise ValueError(
                            "Symlink escapes source: {0} -> {1}".format(full_path, real)
                        )

                arcname = os.path.relpath(full_path, source_dir)
                if strip_prefix:
                    if arcname.startswith(strip_prefix):
                        arcname = arcname[len(strip_prefix):]
                        arcname = arcname.lstrip(os.sep)

                tar.add(full_path, arcname=arcname)

            # Also add directories that are symlinks (check safety)
            for d in dirs:
                dir_path = os.path.join(root, d)
                if os.path.islink(dir_path):
                    real = os.path.realpath(dir_path)
                    if not (real == source_dir
                            or real.startswith(source_dir + os.sep)):
                        raise ValueError(
                            "Symlink escapes source: {0} -> {1}".format(dir_path, real)
                        )


# LD22 caps. Member count cap is high enough for the largest realistic walnut
# (~5000 files in our worst-case fixture) but low enough to bound memory use
# when validating a hostile tar.
_LD22_MAX_MEMBERS = 10000
_LD22_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB

# Tar metadata member types that don't write filesystem entries: PAX headers
# and GNU longname/longlink. Tolerated and skipped during pre-validation.
_LD22_METADATA_TYPES = frozenset(
    t for t in (
        getattr(tarfile, "XHDTYPE", None),
        getattr(tarfile, "XGLTYPE", None),
        getattr(tarfile, "GNUTYPE_LONGNAME", None),
        getattr(tarfile, "GNUTYPE_LONGLINK", None),
    )
    if t is not None
)


def _ld22_validate_members(members, dest_abs):
    # type: (List[tarfile.TarInfo], str) -> None
    """Pre-validate every tar member per LD22. Raises ValueError on any
    rejection. Performs no filesystem writes.

    Rules (in order):
        - Member count cap (10000)
        - Skip PAX / GNU long-name metadata members
        - Reject symlinks and hardlinks outright (any target)
        - Reject device / fifo / block members
        - Allowlist regular files and directories only
        - Cap cumulative regular file size (500 MB)
        - Reject backslashes in member names
        - Normalize ``./`` prefix and reject empty / pure-slash names
        - Reject ``..`` segments and intermediate ``.`` segments
        - Reject absolute POSIX paths and Windows drive letters
        - Reject duplicate effective member paths
        - Reject post-normalisation paths that escape ``dest_abs``
    """
    if len(members) > _LD22_MAX_MEMBERS:
        raise ValueError(
            "Tar has {0} members; cap is {1}".format(
                len(members), _LD22_MAX_MEMBERS
            )
        )

    total = 0
    seen_effective = set()  # type: Set[str]

    for m in members:
        # Skip PAX / GNU long-name metadata members; they don't materialise
        # as filesystem entries.
        if m.type in _LD22_METADATA_TYPES:
            continue

        # Reject filesystem-writing dangerous types outright (LD22 v10).
        if m.issym() or m.islnk():
            raise ValueError(
                "Symlink/hardlink not allowed: {0!r}".format(m.name)
            )
        if m.ischr() or m.isblk() or m.isfifo():
            raise ValueError(
                "Device or fifo member: {0!r}".format(m.name)
            )

        # Allowlist: only regular files and directories from here on (LD22 v13).
        if not (m.isfile() or m.isdir()):
            raise ValueError(
                "Unsupported tar member type for {0!r}".format(m.name)
            )

        if m.isfile():
            total += m.size
            if total > _LD22_MAX_TOTAL_BYTES:
                raise ValueError(
                    "Tar expands to > {0} bytes".format(_LD22_MAX_TOTAL_BYTES)
                )

        # Reject backslashes (LD22 v12).
        if "\\" in m.name:
            raise ValueError(
                "Backslash in member name: {0!r}".format(m.name)
            )

        # Normalize: strip leading ``./`` (legitimate tar convention).
        normalized = m.name
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized or normalized.strip("/") == "":
            raise ValueError(
                "Empty or invalid member name: {0!r}".format(m.name)
            )

        # Reject ``..`` segments and intermediate ``.`` segments (LD22 v12).
        parts = normalized.split("/")
        for part in parts:
            if part == "..":
                raise ValueError(
                    "Parent-dir segment: {0!r}".format(m.name)
                )
            if part == ".":
                raise ValueError(
                    "Intermediate dot-segment: {0!r}".format(m.name)
                )

        # Reject absolute POSIX paths and Windows drive letters (LD22 v9).
        if normalized.startswith("/") or (
            len(normalized) >= 2
            and normalized[1] == ":"
            and normalized[0].isalpha()
        ):
            raise ValueError(
                "Absolute path member: {0!r}".format(m.name)
            )

        # Reject duplicate effective member paths (LD22 v12).
        # Normalise trailing slashes so ``foo`` and ``foo/`` collide.
        effective = normalized.rstrip("/")
        if effective in seen_effective:
            raise ValueError(
                "Duplicate effective member path: {0!r}".format(m.name)
            )
        seen_effective.add(effective)

        # Final defence: post-normalisation join must stay inside dest.
        joined = os.path.normpath(os.path.join(dest_abs, normalized))
        if not (joined == dest_abs or joined.startswith(dest_abs + os.sep)):
            raise ValueError(
                "Path traversal member: {0!r}".format(m.name)
            )


def safe_tar_extract(archive_path, output_dir):
    # type: (str, str) -> None
    """Extract a tar.gz archive with LD22 pre-validation safety.

    Pre-validates ALL members before any extraction. Zero filesystem writes
    on rejection. Implements the LD22 acceptance contract:

    - Rejects path traversal (``../``)
    - Rejects absolute POSIX paths and Windows drive letters
    - Rejects ANY symlink or hardlink member outright
    - Rejects device / fifo / block members
    - Rejects member types other than regular file or directory
    - Rejects backslashes in member names
    - Rejects duplicate effective member paths (e.g. ``foo`` + ``./foo``)
    - Rejects ``..`` and intermediate ``.`` path segments
    - Caps cumulative file size at 500 MB
    - Caps member count at 10000
    - Tolerates PAX header and GNU long-name metadata members (skipped)

    Extraction goes through an inner staging dir on the same filesystem so
    a mid-extract failure leaves ``output_dir`` empty.
    """
    archive_path = os.path.abspath(archive_path)
    output_dir = os.path.abspath(output_dir)

    if not os.path.isfile(archive_path):
        raise FileNotFoundError("Archive not found: {0}".format(archive_path))

    os.makedirs(output_dir, exist_ok=True)

    # Inner staging dir on the same filesystem so the post-validate move is a
    # cheap rename. The staging dir is always cleaned up in ``finally``.
    parent = os.path.dirname(output_dir)
    staging = tempfile.mkdtemp(dir=parent, prefix=".p2p-extract-")

    try:
        try:
            tar = tarfile.open(archive_path, "r:*")
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise ValueError(
                "Corrupt or unreadable tar archive at {0}: {1}".format(
                    archive_path, exc
                )
            )
        with tar:
            try:
                members = tar.getmembers()
            except (tarfile.TarError, EOFError) as exc:
                raise ValueError(
                    "Corrupt tar archive at {0}: {1}".format(archive_path, exc)
                )

            # LD22 pre-validation: zero writes on any rejection.
            _ld22_validate_members(members, staging)

            # All members passed pre-validation. Now extract.
            # Python 3.12+ supports extractall(filter='data'); use it as
            # additional defence-in-depth when available.
            import inspect
            try:
                sig = inspect.signature(tar.extractall)
                supports_filter = "filter" in sig.parameters
            except (TypeError, ValueError):
                supports_filter = False
            try:
                if supports_filter:
                    tar.extractall(path=staging, filter="data")
                else:
                    tar.extractall(path=staging)
            except (tarfile.TarError, EOFError) as exc:
                raise ValueError(
                    "Corrupt tar archive at {0}: {1}".format(
                        archive_path, exc
                    )
                )

        # Move contents from inner staging into output_dir.
        for item in os.listdir(staging):
            src = os.path.join(staging, item)
            dst = os.path.join(output_dir, item)
            if os.path.exists(dst):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            os.replace(src, dst)

    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


# Public LD22 alias used by docstrings and external callers. Identical
# behaviour to ``safe_tar_extract``; the alias matches the LD22 spec name.
safe_extractall = safe_tar_extract


def tar_list_entries(archive_path):
    # type: (str) -> List[str]
    """Return a list of entry names in a tar archive."""
    archive_path = os.path.abspath(archive_path)
    if not os.path.isfile(archive_path):
        raise FileNotFoundError("Archive not found: {0}".format(archive_path))

    with tarfile.open(archive_path, "r:*") as tar:
        return [m.name for m in tar.getmembers()]


# ---------------------------------------------------------------------------
# JSON state files (atomic read/write)
# ---------------------------------------------------------------------------

def atomic_json_write(path, data):
    # type: (str, Any) -> None
    """Write *data* as JSON to *path* atomically (temp + fsync + replace).

    The temp file is created in the same directory as *path* so that
    ``os.replace()`` is a same-filesystem atomic rename on POSIX and a safe
    cross-process replace on Windows.
    """
    path = os.path.abspath(path)
    target_dir = os.path.dirname(path)
    os.makedirs(target_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_json_read(path):
    # type: (str) -> Dict[str, Any]
    """Read JSON from *path*. Returns empty dict on missing or corrupt file."""
    path = os.path.abspath(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError, OSError):
        return {}


# ---------------------------------------------------------------------------
# OpenSSL detection
# ---------------------------------------------------------------------------

def detect_openssl():
    # type: () -> Dict[str, Any]
    """Detect the system openssl binary and its capabilities.

    Returns a dict::

        {
            "binary": "openssl",        # path or name
            "version": "LibreSSL 3.3.6",
            "is_libressl": True,
            "supports_pbkdf2": True,
            "supports_pkeyutl": True,
        }

    Returns None values on detection failure (openssl not found). Per LD5,
    receiver paths use a fallback chain when ``-pbkdf2`` is unavailable so
    legacy v2 packages still decrypt; this function reports capability,
    callers decide which fallback to attempt.
    """
    result = {
        "binary": None,
        "version": None,
        "is_libressl": None,
        "supports_pbkdf2": None,
        "supports_pkeyutl": None,
    }  # type: Dict[str, Any]

    # Find openssl binary
    for candidate in ("openssl", "/usr/bin/openssl", "/usr/local/bin/openssl"):
        try:
            proc = subprocess.run(
                [candidate, "version"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                result["binary"] = candidate
                result["version"] = proc.stdout.strip()
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    if result["binary"] is None:
        return result

    version_str = result["version"] or ""
    result["is_libressl"] = "LibreSSL" in version_str

    # Detect -pbkdf2 support.
    # LibreSSL < 3.1 and OpenSSL < 1.1.1 lack -pbkdf2.
    if result["is_libressl"]:
        # Parse LibreSSL version: "LibreSSL X.Y.Z"
        m = re.search(r"LibreSSL\s+(\d+)\.(\d+)\.(\d+)", version_str)
        if m:
            major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
            result["supports_pbkdf2"] = (major, minor, patch) >= (3, 1, 0)
        else:
            result["supports_pbkdf2"] = False
    else:
        # OpenSSL: "OpenSSL X.Y.Zp" or "OpenSSL X.Y.Z"
        m = re.search(r"OpenSSL\s+(\d+)\.(\d+)\.(\d+)", version_str)
        if m:
            major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
            result["supports_pbkdf2"] = (major, minor, patch) >= (1, 1, 1)
        else:
            result["supports_pbkdf2"] = False

    # Detect pkeyutl support (needed for RSA-OAEP).
    try:
        proc = subprocess.run(
            [result["binary"], "pkeyutl", "-help"],
            capture_output=True, text=True, timeout=5,
        )
        # pkeyutl -help returns 0 on OpenSSL, 1 on some versions -- both mean
        # it exists. If the command is truly missing, FileNotFoundError or
        # returncode != 0 with "unknown command" in stderr.
        stderr = proc.stderr.lower()
        result["supports_pkeyutl"] = "unknown command" not in stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result["supports_pkeyutl"] = False

    return result


# ---------------------------------------------------------------------------
# Base64
# ---------------------------------------------------------------------------

def b64_encode_file(path):
    # type: (str) -> str
    """Return strict base64 encoding of a file (no line breaks).

    Uses ``openssl base64 -A`` for cross-platform portability
    (works on both LibreSSL and OpenSSL).
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("File not found: {0}".format(path))

    ssl = detect_openssl()
    if ssl["binary"] is None:
        raise RuntimeError("openssl not found on this system")

    proc = subprocess.run(
        [ssl["binary"], "base64", "-A", "-in", path],
        capture_output=True, text=True, timeout=30,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            "openssl base64 failed (rc={0}): {1}".format(proc.returncode, proc.stderr)
        )

    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# YAML frontmatter parsing
# ---------------------------------------------------------------------------

def parse_yaml_frontmatter(content):
    # type: (str) -> Dict[str, Any]
    """Parse YAML frontmatter from markdown content.

    Hand-rolled parser matching the pattern in generate-index.py.
    No PyYAML dependency. Handles:
    - Scalar values (strings, numbers, booleans)
    - Inline lists: ``[a, b, c]``
    - Multi-line lists (items starting with ``  - ``)
    - Quoted strings (single and double)

    Returns an empty dict if no frontmatter is found.
    """
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}

    fm = {}  # type: Dict[str, Any]
    lines = match.group(1).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        kv = re.match(r"^(\w[\w-]*)\s*:\s*(.*)", line)
        if kv:
            key = kv.group(1)
            val = kv.group(2).strip()

            # Check for multi-line list (next lines start with "  - ")
            if val == "" or val == "[]":
                items = []  # type: List[str]
                j = i + 1
                while j < len(lines) and re.match(r"^\s+-\s", lines[j]):
                    item_match = re.match(r"^\s+-\s+(.*)", lines[j])
                    if item_match:
                        items.append(item_match.group(1).strip())
                    j += 1
                if items:
                    fm[key] = items
                    i = j
                    continue
                else:
                    fm[key] = val
            elif val.startswith("[") and val.endswith("]"):
                # Inline list: [a, b, c]
                inner = val[1:-1]
                fm[key] = [
                    x.strip().strip('"').strip("'")
                    for x in inner.split(",")
                    if x.strip()
                ]
            else:
                # Remove surrounding quotes
                if ((val.startswith('"') and val.endswith('"'))
                        or (val.startswith("'") and val.endswith("'"))):
                    val = val[1:-1]

                # Coerce booleans and numbers
                lower = val.lower()
                if lower == "true":
                    fm[key] = True
                elif lower == "false":
                    fm[key] = False
                elif lower == "null" or lower == "~":
                    fm[key] = None
                else:
                    # Try integer
                    try:
                        fm[key] = int(val)
                    except ValueError:
                        # Try float
                        try:
                            fm[key] = float(val)
                        except ValueError:
                            fm[key] = val
        i += 1
    return fm


# ---------------------------------------------------------------------------
# Package format constants
# ---------------------------------------------------------------------------

FORMAT_VERSION = "2.1.0"

# Size threshold for pre-flight warning (35 MB -- GitHub Contents API limit
# with base64 overhead is ~50 MB, but 35 MB leaves margin).
SIZE_WARN_BYTES = 35 * 1024 * 1024


def _strip_active_sessions(content):
    # type: (str) -> str
    """Remove ``active_sessions:`` blocks from manifest YAML content."""
    lines = content.split("\n")
    result = []
    in_active_sessions = False
    for line in lines:
        if re.match(r"^active_sessions\s*:", line):
            in_active_sessions = True
            continue
        if in_active_sessions:
            # Keep going while indented (continuation of active_sessions block)
            if line and (line[0] == " " or line[0] == "\t"):
                continue
            in_active_sessions = False
        result.append(line)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Package manifest parsing (NOT walnut context.manifest.yaml)
# ---------------------------------------------------------------------------
#
# The functions below parse the manifest.yaml that lives INSIDE a .walnut
# package archive. This is a different schema from the bundle-level
# context.manifest.yaml; do not conflate. The bundle parser lives in
# walnut_paths._parse_manifest_minimal and project.py::parse_manifest.

def _yaml_escape(s):
    # type: (str) -> str
    """Escape a string for embedding in double-quoted YAML values."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _yaml_unquote(val):
    # type: (str) -> Any
    """Remove surrounding quotes from a YAML value string and coerce primitives."""
    if not val:
        return val
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    # Coerce booleans/numbers
    lower = val.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def parse_manifest(manifest_content):
    # type: (str) -> Dict[str, Any]
    """Parse a package ``manifest.yaml`` string into a dict.

    Hand-rolled line-oriented parser. Handles top-level scalars, the nested
    ``source:`` / ``relay:`` / ``signature:`` blocks, the ``files:`` array
    (each entry has ``path`` / ``sha256`` / ``size``), and the ``bundles:``
    list. No PyYAML dependency.

    Returns a dict with keys: ``format_version``, ``source``, ``scope``,
    ``created``, ``encrypted``, ``description``, ``files``, ``bundles``,
    ``note``, ``relay``, ``signature``.
    """
    manifest = {}  # type: Dict[str, Any]
    lines = manifest_content.strip().split("\n")
    i = 0
    current_section = None  # 'source', 'relay', 'signature', 'files', 'bundles'
    current_file = None  # type: Optional[Dict[str, Any]]
    files_list = []  # type: List[Dict[str, Any]]
    bundles_list = []  # type: List[str]

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Detect indentation level
        indent = len(line) - len(line.lstrip())

        # Top-level key: value pairs (indent 0)
        if indent == 0:
            kv = re.match(r"^(\w[\w_-]*)\s*:\s*(.*)", line)
            if kv:
                key = kv.group(1)
                val = kv.group(2).strip()

                if key == "files" and (val == "" or val == "[]"):
                    current_section = "files"
                    current_file = None
                elif key == "bundles" and (val == "" or val == "[]"):
                    current_section = "bundles"
                elif key == "source" and val == "":
                    current_section = "source"
                    manifest["source"] = {}
                elif key == "relay" and val == "":
                    current_section = "relay"
                    manifest["relay"] = {}
                elif key == "signature" and val == "":
                    current_section = "signature"
                    manifest["signature"] = {}
                else:
                    current_section = None
                    manifest[key] = _yaml_unquote(val)
            i += 1
            continue

        # Indented content belongs to current_section
        if current_section == "source" and indent >= 2:
            kv = re.match(r"^\s+(\w[\w_-]*)\s*:\s*(.*)", line)
            if kv:
                manifest.setdefault("source", {})[kv.group(1)] = _yaml_unquote(
                    kv.group(2).strip()
                )
        elif current_section == "relay" and indent >= 2:
            kv = re.match(r"^\s+(\w[\w_-]*)\s*:\s*(.*)", line)
            if kv:
                manifest.setdefault("relay", {})[kv.group(1)] = _yaml_unquote(
                    kv.group(2).strip()
                )
        elif current_section == "signature" and indent >= 2:
            kv = re.match(r"^\s+(\w[\w_-]*)\s*:\s*(.*)", line)
            if kv:
                manifest.setdefault("signature", {})[kv.group(1)] = _yaml_unquote(
                    kv.group(2).strip()
                )
        elif current_section == "files":
            if stripped.startswith("- path:"):
                # Start of a new file entry
                if current_file:
                    files_list.append(current_file)
                path_val = stripped[len("- path:"):].strip()
                current_file = {"path": _yaml_unquote(path_val)}
            elif current_file and indent >= 4:
                kv = re.match(r"^\s+(\w[\w_-]*)\s*:\s*(.*)", line)
                if kv:
                    val = _yaml_unquote(kv.group(2).strip())
                    # Coerce size to int
                    if kv.group(1) == "size":
                        try:
                            val = int(val)
                        except (ValueError, TypeError):
                            pass
                    current_file[kv.group(1)] = val
        elif current_section == "bundles":
            if stripped.startswith("- "):
                bundles_list.append(stripped[2:].strip())

        i += 1

    # Flush last file entry
    if current_file:
        files_list.append(current_file)

    if files_list:
        manifest["files"] = files_list
    if bundles_list:
        manifest["bundles"] = bundles_list

    # Coerce booleans
    if "encrypted" in manifest:
        if isinstance(manifest["encrypted"], str):
            manifest["encrypted"] = manifest["encrypted"].lower() == "true"

    return manifest


# ---------------------------------------------------------------------------
# Manifest-driven verification
# ---------------------------------------------------------------------------

def verify_checksums(manifest, base_dir):
    # type: (Dict[str, Any], str) -> Tuple[bool, List[Dict[str, Any]]]
    """Verify SHA-256 checksums for all files listed in the manifest.

    Returns ``(ok, failures)`` where failures is a list of dicts describing
    each mismatch or missing file.
    """
    failures = []  # type: List[Dict[str, Any]]
    for entry in manifest.get("files", []):
        rel_path = entry["path"]
        expected = entry["sha256"]
        full_path = os.path.join(base_dir, rel_path.replace("/", os.sep))

        if not os.path.isfile(full_path):
            failures.append({
                "path": rel_path,
                "error": "file_missing",
                "expected": expected,
            })
            continue

        actual = sha256_file(full_path)
        if actual != expected:
            failures.append({
                "path": rel_path,
                "error": "checksum_mismatch",
                "expected": expected,
                "actual": actual,
            })

    return (len(failures) == 0, failures)


def check_unlisted_files(manifest, base_dir):
    # type: (Dict[str, Any], str) -> List[str]
    """Return relative paths of files in *base_dir* that are not in the manifest.

    The manifest.yaml itself is excluded from this check.
    """
    listed = {entry["path"] for entry in manifest.get("files", [])}
    listed.add("manifest.yaml")

    unlisted = []  # type: List[str]
    for root, _dirs, filenames in os.walk(base_dir):
        for fname in filenames:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, base_dir).replace(os.sep, "/")
            if rel not in listed:
                unlisted.append(rel)

    return unlisted


# ---------------------------------------------------------------------------
# Generic file staging helpers
# ---------------------------------------------------------------------------
#
# These are layout-agnostic copy primitives. The v3-aware staging dispatch
# (full / bundle / snapshot scope) lives in task .4.

def _copy_file(src, dst):
    # type: (str, str) -> None
    """Copy a file, creating parent dirs as needed.

    Strips ``active_sessions:`` blocks from YAML/manifest files in transit.
    Binary files go through ``shutil.copy2`` so mtimes survive packaging.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    base = os.path.basename(src)
    if base.endswith(".yaml") or base.endswith(".yml"):
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        content = _strip_active_sessions(content)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
    else:
        shutil.copy2(src, dst)


def _stage_tree(src_dir, dst_dir):
    # type: (str, str) -> None
    """Recursively copy a directory tree, applying basic safety exclusions.

    Drops ``._*`` resource forks and ``.DS_Store``. Does NOT apply v3 package
    exclusion rules -- that policy lives in the v3 staging dispatcher (task .4)
    so this primitive stays general-purpose.
    """
    src_dir = os.path.abspath(src_dir)
    skip_names = {".DS_Store", "Thumbs.db", "desktop.ini"}
    for root, dirs, files in os.walk(src_dir):
        # Filter excluded directories in-place
        dirs[:] = [
            d for d in dirs
            if not d.startswith("._") and d not in skip_names
        ]

        for fname in files:
            if fname in skip_names or fname.startswith("._"):
                continue
            full = os.path.join(root, fname)
            dst = os.path.join(dst_dir, os.path.relpath(full, src_dir))
            _copy_file(full, dst)


# ---------------------------------------------------------------------------
# v3 staging layer (LD8, LD9, LD26, LD27)
# ---------------------------------------------------------------------------
#
# The functions below implement the v3-aware staging for the create pipeline.
# They sit above the layout-agnostic primitives (``_copy_file``, ``_stage_tree``)
# and below the user-facing CLI (task .5). Staging is a read-only operation on
# the source walnut: nothing is written under ``walnut_path``.
#
# Package layout is ALWAYS flat (LD8): no ``bundles/`` container, no
# ``_core/_capsules/`` container. v2 and v1 source walnuts are migrated on the
# fly at create time. The only exception is ``--source-layout v2`` testing mode
# (task .5 / .7), which bypasses these helpers.


# Exact file paths (POSIX) that are ALWAYS excluded from any v3 package.
# Matches LD26 "Excluded from package" for full scope. Applies to bundle and
# snapshot scopes as a safety net even though their required file set does not
# include these paths.
_PACKAGE_EXCLUDES = {
    "_kernel/now.json",
    "_kernel/_generated",
    "_kernel/history",
    "_kernel/links.yaml",
    "_kernel/people.yaml",
    "_kernel/imports.json",
    ".alive/_squirrels",
    "desktop.ini",
}

# Filename-only exclusions (matched anywhere in the tree).
_PACKAGE_EXCLUDE_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}

# Directories that belong to the kernel, legacy containers, build artefacts, or
# archives. They are skipped when enumerating live context for full scope.
# Mirrors LD27's live context definition. ``bundles`` and ``_core`` are on the
# list because bundles are staged separately via ``walnut_paths.find_bundles``.
_LIVE_CONTEXT_SKIP_DIRS = {
    "_kernel",
    ".alive",
    ".git",
    "__pycache__",
    "node_modules",
    "raw",
    "dist",
    "build",
    ".next",
    "target",
    "_archive",
    "_references",
    "01_Archive",
    "bundles",
    "_core",
}

# Standard bundle containers for LD8 top-level detection. Values are
# POSIX-normalized relpaths.
STANDARD_CONTAINERS = {"bundles", "_core/_capsules"}


# ---------------------------------------------------------------------------
# LD9 stub constants
# ---------------------------------------------------------------------------
#
# These strings are byte-stable. Tests mock ``now_utc_iso`` and
# ``resolve_session_id`` so the emitted stub output is deterministic given the
# walnut name and sender handle.

STUB_LOG_MD = """\
---
walnut: {walnut_name}
stubbed_at: {iso_timestamp}
stubbed_by: squirrel:{session_id}
reason: Default share exclusion -- full log not shared; ask sender for access
entry-count: 0
---

This is a placeholder. The original log.md was excluded by the sender's default
share baseline. Contact {sender} directly for access to the full history.
"""

STUB_INSIGHTS_MD = """\
---
walnut: {walnut_name}
stubbed_at: {iso_timestamp}
reason: Default share exclusion
---

## Strategy
(stubbed)

## Technical
(stubbed)
"""


# ---------------------------------------------------------------------------
# Mockable environment helpers (LD9)
# ---------------------------------------------------------------------------

def now_utc_iso():
    # type: () -> str
    """Return the current UTC time as an ISO 8601 string.

    Wrapped in a function so tests can monkeypatch a fixed timestamp without
    touching ``datetime`` globally. Format matches the stub constants.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def resolve_session_id():
    # type: () -> str
    """Return the current ALIVE session id, or ``"manual"`` for CLI runs."""
    return os.environ.get("ALIVE_SESSION_ID", "manual")


def resolve_sender():
    # type: () -> str
    """Return the current sender handle (GitHub login), or ``"unknown"``.

    Reads ``GH_USER`` from the environment. ``gh api user`` fallback lives in
    the CLI layer (task .5) so this helper stays pure and test-friendly.
    """
    return os.environ.get("GH_USER", "unknown")


def render_stub_log(walnut_name, sender, session_id):
    # type: (str, str, str) -> str
    """Render ``STUB_LOG_MD`` with the current timestamp and identity fields."""
    return STUB_LOG_MD.format(
        walnut_name=walnut_name,
        iso_timestamp=now_utc_iso(),
        session_id=session_id,
        sender=sender,
    )


def render_stub_insights(walnut_name):
    # type: (str) -> str
    """Render ``STUB_INSIGHTS_MD`` with the current timestamp."""
    return STUB_INSIGHTS_MD.format(
        walnut_name=walnut_name,
        iso_timestamp=now_utc_iso(),
    )


# ---------------------------------------------------------------------------
# LD8 top-level bundle helper
# ---------------------------------------------------------------------------

def is_top_level_bundle(bundle_relpath):
    # type: (str) -> bool
    """Return True if a POSIX relpath identifies a top-level bundle.

    A bundle is "top-level" if its relpath is either a single path component
    (v3 flat, e.g. ``shielding-review``) OR lives directly under a standard
    container (``bundles/foo`` or ``_core/_capsules/foo``). Bundles buried in
    arbitrary intermediate dirs (e.g. ``archive/old/bundle-a``) are NOT
    shareable via P2P and return False.

    The function is defensive about input: OS-native backslashes are converted
    to forward slashes before the check, so a caller that forgot to normalize
    still gets the right answer.
    """
    if not bundle_relpath:
        return False
    relpath = bundle_relpath.replace("\\", "/")
    if "/" not in relpath:
        return True
    for container in STANDARD_CONTAINERS:
        prefix = container + "/"
        if relpath.startswith(prefix):
            remainder = relpath[len(prefix):]
            if "/" not in remainder:
                return True
    return False


def _should_exclude_package(rel_path):
    # type: (str) -> bool
    """Return True if a POSIX relpath matches the system exclude list.

    Only the hardcoded system excludes from ``_PACKAGE_EXCLUDES`` /
    ``_PACKAGE_EXCLUDE_NAMES`` are applied here. User-supplied ``--exclude``
    glob patterns and preset exclusions live in LD11's create CLI contract and
    are applied by the CLI layer (task .5 / .7), after this helper returns
    False.
    """
    if not rel_path:
        return False
    rel = rel_path.replace("\\", "/")

    # Exact path prefix matches (covers both files and dir prefixes).
    for pattern in _PACKAGE_EXCLUDES:
        if rel == pattern or rel.startswith(pattern + "/"):
            return True

    # Name-only filter. Applies to any segment of the relpath, not just the
    # leaf -- ``.DS_Store`` buried inside a bundle should still be excluded.
    parts = rel.split("/")
    for segment in parts:
        if segment in _PACKAGE_EXCLUDE_NAMES:
            return True
        if segment.startswith("._"):
            return True
    return False


# ---------------------------------------------------------------------------
# Staging helpers -- scope implementations
# ---------------------------------------------------------------------------

def _write_text(path, content):
    # type: (str, str) -> None
    """Write UTF-8 text, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _walnut_name(walnut_path):
    # type: (str) -> str
    """Return the walnut's directory basename.

    Used to populate stub templates. Does NOT parse ``_kernel/key.md`` -- that
    level of detail belongs in the manifest generator (task .5).
    """
    return os.path.basename(os.path.abspath(walnut_path.rstrip(os.sep)))


def _copy_kernel_optional(walnut_path, staging, fname):
    # type: (str, str, str) -> bool
    """Copy ``_kernel/{fname}`` if it exists. Returns True on copy."""
    src = os.path.join(walnut_path, "_kernel", fname)
    if os.path.isfile(src):
        _copy_file(src, os.path.join(staging, "_kernel", fname))
        return True
    return False


def _copy_kernel_or_default(walnut_path, staging, fname, default_content):
    # type: (str, str, str, str) -> None
    """Copy ``_kernel/{fname}`` if present; otherwise write ``default_content``."""
    src = os.path.join(walnut_path, "_kernel", fname)
    dst = os.path.join(staging, "_kernel", fname)
    if os.path.isfile(src):
        _copy_file(src, dst)
    else:
        _write_text(dst, default_content)


def _stage_top_level_bundles(walnut_path, staging, warnings):
    # type: (str, str, List[str]) -> List[Tuple[str, str]]
    """Stage all top-level bundles flat at the staging root.

    Iterates ``walnut_paths.find_bundles``, filters with ``is_top_level_bundle``,
    and copies each surviving bundle to ``{staging}/{leaf_name}/``. Nested
    bundles append a warning. Leaf name collisions (two bundles with the same
    basename at different standard locations) are flagged but the first wins
    -- LD27 says ``create --scope full`` REFUSES on leaf collisions, so this
    helper expects the CLI (task .5) to have already gated that case. The
    warning exists as defence-in-depth in case something slips past.

    Returns the list of successfully staged ``(relpath, leaf_name)`` pairs.
    """
    if walnut_paths is None:  # pragma: no cover -- defensive only
        raise RuntimeError(
            "walnut_paths module not available; cannot enumerate bundles"
        )

    staged = []  # type: List[Tuple[str, str]]
    seen_leaves = {}  # type: Dict[str, str]
    nested_relpaths = []  # type: List[str]

    for relpath, abs_path in walnut_paths.find_bundles(walnut_path):
        if not is_top_level_bundle(relpath):
            nested_relpaths.append(relpath)
            continue
        leaf = relpath.split("/")[-1]
        if leaf in seen_leaves:
            warnings.append(
                "Bundle leaf '{0}' appears at multiple locations ({1}, {2}); "
                "keeping the first. Run /alive:system-cleanup to resolve.".format(
                    leaf, seen_leaves[leaf], relpath
                )
            )
            continue
        seen_leaves[leaf] = relpath
        dst_dir = os.path.join(staging, leaf)
        _stage_tree(abs_path, dst_dir)
        staged.append((relpath, leaf))

    if nested_relpaths:
        warnings.append(
            "Excluded nested (non-top-level) bundles from package: {0}".format(
                ", ".join(sorted(nested_relpaths))
            )
        )
    return staged


def _stage_live_context(walnut_path, staging):
    # type: (str, str) -> None
    """Copy live context files into ``staging`` at the walnut root.

    Live context is every file or directory at the walnut root that is NOT:
      - part of ``_kernel/`` / ``.alive/`` / ``.git``
      - a legacy bundle container (``bundles/`` or ``_core/``)
      - an archive / build dir (see ``_LIVE_CONTEXT_SKIP_DIRS``)
      - a bundle directory (has ``context.manifest.yaml`` at its root)
      - a dotfile / dotdir
      - on the system exclude list (``_should_exclude_package``)

    Bundles inside ``bundles/``, ``_core/``, or nested under other directories
    are staged by ``_stage_top_level_bundles`` -- this helper deliberately
    skips them.
    """
    if not os.path.isdir(walnut_path):
        return
    for item in sorted(os.listdir(walnut_path)):
        if item in _LIVE_CONTEXT_SKIP_DIRS:
            continue
        if item.startswith("."):
            continue
        if item in _PACKAGE_EXCLUDE_NAMES:
            continue
        if _should_exclude_package(item):
            continue
        src = os.path.join(walnut_path, item)
        if os.path.isdir(src):
            # A directory with ``context.manifest.yaml`` at its root is a v3
            # flat bundle; it is already staged by the bundle pass.
            if os.path.isfile(os.path.join(src, "context.manifest.yaml")):
                continue
            _stage_tree(src, os.path.join(staging, item))
        elif os.path.isfile(src):
            _copy_file(src, os.path.join(staging, item))


def _stage_full(
    walnut_path,
    staging,
    sender=None,
    session_id=None,
    stub_kernel_history=True,
    warnings=None,
):
    # type: (str, str, Optional[str], Optional[str], bool, Optional[List[str]]) -> List[str]
    """Stage a full-scope package per LD26.

    Copies the required ``_kernel/*`` files, stages all top-level bundles flat,
    and copies live context. Returns the accumulated warnings list (also
    mutated in place if the caller passed one).

    Parameters:
        walnut_path: absolute path to the source walnut
        staging: absolute path to an empty staging directory
        sender: GitHub handle used in stub log.md rendering; falls back to
            ``resolve_sender()``
        session_id: ALIVE session id used in stub log.md rendering; falls back
            to ``resolve_session_id()``
        stub_kernel_history: when True (the LD9 default), log.md and
            insights.md are replaced with stub content regardless of source
            state. When False (``--include-full-history``), the real files are
            copied if present.
        warnings: optional pre-existing list to append warnings onto
    """
    if warnings is None:
        warnings = []
    if sender is None:
        sender = resolve_sender()
    if session_id is None:
        session_id = resolve_session_id()

    walnut_name = _walnut_name(walnut_path)

    # ---- required _kernel files -------------------------------------------------
    # key.md -- always ship, always real
    key_src = os.path.join(walnut_path, "_kernel", "key.md")
    if not os.path.isfile(key_src):
        raise FileNotFoundError(
            "walnut missing _kernel/key.md: {0}".format(walnut_path)
        )
    _copy_file(key_src, os.path.join(staging, "_kernel", "key.md"))

    # log.md -- stubbed unless --include-full-history
    if stub_kernel_history:
        _write_text(
            os.path.join(staging, "_kernel", "log.md"),
            render_stub_log(walnut_name, sender, session_id),
        )
    else:
        log_src = os.path.join(walnut_path, "_kernel", "log.md")
        if os.path.isfile(log_src):
            _copy_file(log_src, os.path.join(staging, "_kernel", "log.md"))
        else:
            # Still required -- fall back to stub even in include-full mode.
            _write_text(
                os.path.join(staging, "_kernel", "log.md"),
                render_stub_log(walnut_name, sender, session_id),
            )
            warnings.append(
                "Source walnut has no _kernel/log.md; shipping stub instead."
            )

    # insights.md -- same rules as log.md
    if stub_kernel_history:
        _write_text(
            os.path.join(staging, "_kernel", "insights.md"),
            render_stub_insights(walnut_name),
        )
    else:
        ins_src = os.path.join(walnut_path, "_kernel", "insights.md")
        if os.path.isfile(ins_src):
            _copy_file(ins_src, os.path.join(staging, "_kernel", "insights.md"))
        else:
            _write_text(
                os.path.join(staging, "_kernel", "insights.md"),
                render_stub_insights(walnut_name),
            )
            warnings.append(
                "Source walnut has no _kernel/insights.md; shipping stub instead."
            )

    # tasks.json -- copy or synthesize empty skeleton
    _copy_kernel_or_default(
        walnut_path, staging, "tasks.json", '{"tasks": []}\n'
    )
    # completed.json -- same
    _copy_kernel_or_default(
        walnut_path, staging, "completed.json", '{"completed": []}\n'
    )

    # config.yaml -- optional, copy only if present
    _copy_kernel_optional(walnut_path, staging, "config.yaml")

    # ---- bundles (flat at staging root) -----------------------------------------
    _stage_top_level_bundles(walnut_path, staging, warnings)

    # ---- live context -----------------------------------------------------------
    _stage_live_context(walnut_path, staging)

    return warnings


def _stage_bundle(walnut_path, staging, bundle_names, warnings=None):
    # type: (str, str, List[str], Optional[List[str]]) -> List[str]
    """Stage a bundle-scope package per LD26.

    Ships ``_kernel/key.md`` (for identity verification on receive per LD18)
    plus every bundle requested via ``bundle_names`` (LEAF names only).
    Resolution policy matches LD8 enforcement: v3 flat and standard containers
    are accepted; nested-only locations are rejected with an actionable error.

    Raises:
        FileNotFoundError if any requested bundle cannot be resolved.
        ValueError if a requested bundle exists only at non-top-level locations
            or at multiple standard locations (mixed-layout collision).
    """
    if warnings is None:
        warnings = []
    if walnut_paths is None:  # pragma: no cover -- defensive only
        raise RuntimeError(
            "walnut_paths module not available; cannot enumerate bundles"
        )
    if not bundle_names:
        raise ValueError("_stage_bundle requires at least one bundle name")

    # key.md is required for LD18 identity check
    key_src = os.path.join(walnut_path, "_kernel", "key.md")
    if not os.path.isfile(key_src):
        raise FileNotFoundError(
            "walnut missing _kernel/key.md: {0}".format(walnut_path)
        )
    _copy_file(key_src, os.path.join(staging, "_kernel", "key.md"))

    # Build a one-shot leaf-name index from find_bundles so we can surface
    # nested matches when resolve_bundle_path returns None.
    all_bundles = walnut_paths.find_bundles(walnut_path)
    leaf_index = {}  # type: Dict[str, List[Tuple[str, str]]]
    for relpath, abs_path in all_bundles:
        leaf = relpath.split("/")[-1]
        leaf_index.setdefault(leaf, []).append((relpath, abs_path))

    seen_leaves = set()  # type: set
    for name in bundle_names:
        if "/" in name or "\\" in name:
            raise ValueError(
                "Bundle names must be leaf names (no path separators): "
                "'{0}'".format(name)
            )
        if name in seen_leaves:
            raise ValueError(
                "Duplicate bundle name in request: '{0}'".format(name)
            )
        seen_leaves.add(name)

        # Collision detection: both v3 flat AND a standard container hold a
        # bundle with this leaf name. Mirror LD27 policy and refuse.
        matches = leaf_index.get(name, [])
        top_level_matches = [
            (rp, ap) for rp, ap in matches if is_top_level_bundle(rp)
        ]
        if len(top_level_matches) > 1:
            relpaths = sorted(rp for rp, _ in top_level_matches)
            raise ValueError(
                "Bundle name collision: '{0}' exists at {1}. "
                "Resolve via /alive:system-cleanup before sharing.".format(
                    name, relpaths
                )
            )

        if top_level_matches:
            # Prefer resolve_bundle_path's ordering for the single-match case
            # (v3 wins over v2 wins over v1) to match LD8 create enforcement.
            resolved = walnut_paths.resolve_bundle_path(walnut_path, name)
            if resolved and any(
                os.path.abspath(ap) == resolved
                for _, ap in top_level_matches
            ):
                bundle_abs = resolved
            else:
                bundle_abs = top_level_matches[0][1]
        else:
            # Not found at standard locations. If it exists nested, reject
            # with an actionable message listing where. Otherwise report not
            # found.
            if matches:
                nested = sorted(rp for rp, _ in matches)
                raise ValueError(
                    "Bundle '{0}' exists at non-standard location(s): {1}. "
                    "Only top-level bundles (v3 flat or v2/v1 container) are "
                    "shareable via P2P. Move the bundle to the walnut root "
                    "or an archive before sharing.".format(name, nested)
                )
            raise FileNotFoundError(
                "Bundle '{0}' not found in walnut.".format(name)
            )

        dst = os.path.join(staging, name)
        _stage_tree(bundle_abs, dst)

    return warnings


def _stage_snapshot(walnut_path, staging, warnings=None):
    # type: (str, str, Optional[List[str]]) -> List[str]
    """Stage a snapshot-scope package per LD26.

    Contents are EXACTLY ``_kernel/key.md`` (real) and ``_kernel/insights.md``
    (stubbed per LD9). Snapshot scope intentionally has no history, no tasks,
    no bundles, no live context.
    """
    if warnings is None:
        warnings = []
    key_src = os.path.join(walnut_path, "_kernel", "key.md")
    if not os.path.isfile(key_src):
        raise FileNotFoundError(
            "walnut missing _kernel/key.md: {0}".format(walnut_path)
        )
    _copy_file(key_src, os.path.join(staging, "_kernel", "key.md"))

    walnut_name = _walnut_name(walnut_path)
    _write_text(
        os.path.join(staging, "_kernel", "insights.md"),
        render_stub_insights(walnut_name),
    )
    return warnings


def _stage_files(
    walnut_path,
    scope,
    bundle_names=None,
    sender=None,
    session_id=None,
    stub_kernel_history=True,
    staging_dir=None,
    warnings=None,
    source_layout="v3",
):
    # type: (str, str, Optional[List[str]], Optional[str], Optional[str], bool, Optional[str], Optional[List[str]], str) -> str
    """Dispatch to the per-scope staging routine and return the staging dir.

    Creates a temp staging directory (unless ``staging_dir`` is provided) and
    calls the matching ``_stage_*`` function. Leaves the staging dir in place
    on success; callers are responsible for packaging it and cleaning up. On
    failure the staging dir is removed to avoid leaving orphan temp files.

    Parameters:
        walnut_path: absolute path to the source walnut
        scope: ``"full"``, ``"bundle"``, or ``"snapshot"``
        bundle_names: required when scope == "bundle"; ignored otherwise
        sender / session_id: forwarded to ``_stage_full`` for stub rendering
        stub_kernel_history: forwarded to ``_stage_full``
        staging_dir: if provided, stage into this existing empty directory
            instead of creating a temp dir
        warnings: optional list for accumulating warnings
        source_layout: ``"v3"`` (default, the only production value) or ``"v2"``
            (testing only -- bypasses migration and produces a v2-shaped package
            with the legacy ``bundles/`` container, per LD11). The staging
            helpers themselves are layout-agnostic; the parameter is accepted
            here so callers can plumb it through to ``generate_manifest`` and
            so the dispatcher can validate the value early.
    """
    if scope not in ("full", "bundle", "snapshot"):
        raise ValueError(
            "Unknown staging scope '{0}'; expected full|bundle|snapshot".format(
                scope
            )
        )
    if source_layout not in ("v2", "v3"):
        raise ValueError(
            "Unknown source_layout '{0}'; expected v2|v3".format(source_layout)
        )

    walnut_path = os.path.abspath(walnut_path)
    if not os.path.isdir(walnut_path):
        raise FileNotFoundError("walnut path not found: {0}".format(walnut_path))

    if staging_dir is None:
        staging_dir = tempfile.mkdtemp(prefix="walnut-stage-")
    else:
        os.makedirs(staging_dir, exist_ok=True)

    if warnings is None:
        warnings = []

    try:
        if scope == "full":
            _stage_full(
                walnut_path,
                staging_dir,
                sender=sender,
                session_id=session_id,
                stub_kernel_history=stub_kernel_history,
                warnings=warnings,
            )
        elif scope == "bundle":
            if not bundle_names:
                raise ValueError(
                    "_stage_files with scope=bundle requires bundle_names"
                )
            _stage_bundle(walnut_path, staging_dir, bundle_names, warnings)
        else:  # snapshot
            _stage_snapshot(walnut_path, staging_dir, warnings)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return staging_dir


# ---------------------------------------------------------------------------
# v3 manifest generation, validation, and stdlib YAML I/O (LD6, LD20)
# ---------------------------------------------------------------------------
#
# These functions implement the format 2.1.0 manifest contract: canonical JSON
# bytes for ``import_id`` and signature computation, exact byte-reproducible
# payload checksums, generation of the on-disk YAML manifest with the
# ``source_layout`` hint, and a stdlib-only YAML reader/writer for the manifest
# subset (no PyYAML dependency, matching v3 main's regex-only approach).
#
# The receive pipeline (task .8) and the share CLI wiring (task .7) consume
# these helpers; this task lands them with their unit tests but does not yet
# wire them into the user-facing CLI.

# Default minimum plugin version receivers should advertise to senders. Bumped
# alongside ``FORMAT_VERSION`` whenever the manifest schema changes in a way
# that older receivers cannot handle. Advisory only.
MIN_PLUGIN_VERSION = "3.1.0"

# Regex for the receiver-side ``format_version`` accept rule (LD6). Matches
# ``2.x`` and ``2.x.y`` only; explicitly rejects ``3.x``.
_FORMAT_VERSION_RE = re.compile(r"^2\.\d+(\.\d+)?$")

# Allowed scopes for the ``scope:`` field (LD20).
_VALID_SCOPES = ("full", "bundle", "snapshot")

# Allowed values for the ``source_layout:`` field. Receivers tolerate unknown
# values with a warning (LD7 fall-through), but generators must use one of
# these two strings explicitly.
_VALID_SOURCE_LAYOUTS = ("v2", "v3")

# Field order for the on-disk YAML manifest. The canonical (JSON) form
# re-sorts keys alphabetically; this order is for human readability of the
# generated YAML only.
_MANIFEST_FIELD_ORDER = (
    "format_version",
    "source_layout",
    "min_plugin_version",
    "created",
    "scope",
    "source",
    "sender",
    "description",
    "note",
    "exclusions_applied",
    "substitutions_applied",
    "bundles",
    "payload_sha256",
    "files",
    "encryption",
    "signature",
)


def _validate_safe_string(value, field_name):
    # type: (Any, str) -> None
    """Reject free-form strings that would corrupt the hand-rolled YAML emitter.

    The manifest YAML writer below emits single-line scalars only. Newlines,
    carriage returns, or unescaped double quotes inside ``description``,
    ``note``, ``sender``, and similar fields would either break the parser on
    the receive side or, worse, smuggle additional YAML keys into the manifest
    via injection. Reject them up front with a specific error.

    Backslashes are tolerated; the writer escapes them. Single quotes are
    tolerated since the writer always uses double quotes for scalars.
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(
            "Field '{0}' must be a string, got {1}".format(
                field_name, type(value).__name__
            )
        )
    if "\n" in value or "\r" in value:
        raise ValueError(
            "Field '{0}' must be single-line (no newlines): {1!r}".format(
                field_name, value
            )
        )
    if '"' in value:
        raise ValueError(
            "Field '{0}' must not contain unescaped double quotes: {1!r}. "
            "Use single quotes or strip the value before passing it in.".format(
                field_name, value
            )
        )


def canonical_manifest_bytes(manifest_dict):
    # type: (Dict[str, Any]) -> bytes
    """Produce deterministic bytes for ``import_id`` and signature per LD20.

    Algorithm:
    1. Drop the ``signature`` field (signing is computed over the unsigned
       canonical form, so re-signing the same content is idempotent).
    2. Sort all order-sensitive list fields:
       - ``files`` by ``path``
       - ``bundles`` lexicographic
       - ``exclusions_applied`` lexicographic
       - ``substitutions_applied`` by ``path``
       Lists not enumerated here are left in their original order; the schema
       does not currently include any others.
    3. Serialize via ``json.dumps`` with ``sort_keys=True`` and the strict
       ``(",", ":")`` separators, then encode UTF-8.

    The ``recipients`` field is intentionally NOT touched: it lives in the
    separate ``rsa-envelope-v1.json`` (LD21), not in ``manifest.yaml``.
    Touching it would couple ``import_id`` to encryption envelope contents,
    which would break the goal of stable identity across re-encryption for
    different peers.

    The output of this function is the authoritative byte stream that
    ``import_id`` and the RSA-PSS signature are computed over. Any change to
    the algorithm is a format version bump.
    """
    d = dict(manifest_dict)
    d.pop("signature", None)

    if "files" in d and isinstance(d["files"], list):
        d["files"] = sorted(d["files"], key=lambda f: f.get("path", ""))
    if "bundles" in d and isinstance(d["bundles"], list):
        d["bundles"] = sorted(d["bundles"])
    if "exclusions_applied" in d and isinstance(d["exclusions_applied"], list):
        d["exclusions_applied"] = sorted(d["exclusions_applied"])
    if "substitutions_applied" in d and isinstance(d["substitutions_applied"], list):
        d["substitutions_applied"] = sorted(
            d["substitutions_applied"], key=lambda s: s.get("path", "")
        )

    return json.dumps(
        d,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_payload_sha256(files):
    # type: (List[Dict[str, Any]]) -> str
    """Return the LD20 ``payload_sha256`` hex digest for a ``files[]`` list.

    Construction (exact byte stream, sorted by path so reordering the input
    list does not change the output):

        for each file in sorted(files, key=path):
            sha256.update(path_utf8 + NUL + sha256_ascii + NUL + size_decimal_ascii + NL)

    NUL delimiters prevent path-vs-sha ambiguity (no path can contain a NUL
    byte on POSIX/Windows). The trailing NL prevents cross-entry collisions
    where two files might otherwise share a boundary (e.g. ``a`` + ``b`` vs
    ``ab``).

    Receivers recompute this digest from the actual file list and reject the
    package on mismatch -- this catches manifest-vs-files divergence that
    per-file checks alone might miss (e.g. a missing entry in the manifest).
    """
    sorted_files = sorted(files, key=lambda f: f["path"])
    h = hashlib.sha256()
    for f in sorted_files:
        h.update(f["path"].encode("utf-8"))
        h.update(b"\x00")
        h.update(f["sha256"].encode("ascii"))
        h.update(b"\x00")
        h.update(str(f["size"]).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _walk_staging_files(staging_dir):
    # type: (str) -> List[Dict[str, Any]]
    """Walk a staging directory and return ``files[]`` entries per LD20.

    Each entry is ``{"path": <posix>, "sha256": <hex>, "size": <int>}``. The
    ``manifest.yaml`` itself (if it happens to exist already) is excluded --
    the manifest cannot list its own checksum because writing the manifest
    changes its contents. Directory members are skipped; only regular files
    are hashed.

    Paths are POSIX-normalized so the manifest is portable across operating
    systems with different native separators.
    """
    staging_dir = os.path.abspath(staging_dir)
    entries = []  # type: List[Dict[str, Any]]
    for root, _dirs, files in os.walk(staging_dir):
        for fname in files:
            full = os.path.join(root, fname)
            if not os.path.isfile(full) or os.path.islink(full):
                # Skip symlinks even if they point at regular files: tar safety
                # forbids symlinks in packages, and including a symlink in the
                # manifest would let a malicious sender pre-claim a path that
                # later resolves to something else on the receiver.
                continue
            rel = os.path.relpath(full, staging_dir).replace(os.sep, "/")
            if rel == "manifest.yaml":
                continue
            entries.append({
                "path": rel,
                "sha256": sha256_file(full),
                "size": os.path.getsize(full),
            })
    return entries


def generate_manifest(
    staging_dir,
    scope,
    walnut_name,
    bundles=None,
    description="",
    note="",
    session_id="",
    engine="",
    plugin_version="3.1.0",
    sender="unknown",
    exclusions_applied=None,
    substitutions_applied=None,
    source_layout="v3",
    min_plugin_version=None,
    created=None,
):
    # type: (str, str, str, Optional[List[str]], str, str, str, str, str, str, Optional[List[str]], Optional[List[Dict[str, Any]]], str, Optional[str], Optional[str]) -> Dict[str, Any]
    """Generate a v3 manifest for a staged package and write it to disk.

    Walks the staging tree, computes per-file SHA-256 + size, builds the
    manifest dict per the LD20 schema, and writes it to
    ``{staging_dir}/manifest.yaml`` via the hand-rolled stdlib YAML emitter.
    Returns the manifest dict for callers that need to compute ``import_id``
    or sign it.

    The manifest's ``encryption`` field defaults to ``"none"``; the encrypt
    pipeline (task .7+) overwrites it after wrapping the payload. The
    ``signature`` field is intentionally absent; the sign pipeline appends it
    after canonicalization.

    NOTE: the legacy v2 helper ``_update_manifest_encrypted`` (used by the
    pre-v3 ``encrypt_package`` / ``decrypt_package`` paths) regex-edits an
    ``encrypted: bool`` field, not the LD20 ``encryption: none|passphrase|rsa``
    string field. That helper is part of the v2 encryption pipeline that task
    .7 will rewrite to operate on v3 manifests. Until then, calling
    ``encrypt_package`` on a v3 manifest produced by this function will leave
    ``encryption: "none"`` unchanged in the YAML -- a known cross-task gap
    documented here so receivers do not get a stale or contradictory hint.

    Parameters:
        staging_dir: absolute path to the staging directory (already populated
            by ``_stage_files``)
        scope: ``"full"``, ``"bundle"``, or ``"snapshot"``
        walnut_name: human-readable walnut identifier (basename of the source
            walnut directory, normally)
        bundles: required when ``scope == "bundle"``; ignored otherwise
        description: short single-line description for the package preview
        note: optional personal note (single-line)
        session_id, engine, plugin_version: ``source:`` block fields per LD20
        sender: GitHub-style handle for the originator (LD23 signer model)
        exclusions_applied: glob patterns the sender used to omit files
            entirely from the package (audit trail per LD11)
        substitutions_applied: list of ``{"path": ..., "reason": ...}`` dicts
            for files present in the package but stubbed (LD9 baseline stubs
            and user-specified substitutions)
        source_layout: ``"v2"`` or ``"v3"``; defaults to ``"v3"``
        min_plugin_version: receiver advisory; defaults to ``MIN_PLUGIN_VERSION``
        created: override ISO timestamp; defaults to ``now_utc_iso()``
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            "Unknown scope '{0}'; expected one of {1}".format(scope, _VALID_SCOPES)
        )
    if source_layout not in _VALID_SOURCE_LAYOUTS:
        raise ValueError(
            "Unknown source_layout '{0}'; expected one of {1}".format(
                source_layout, _VALID_SOURCE_LAYOUTS
            )
        )
    if scope == "bundle" and not bundles:
        raise ValueError(
            "scope=bundle requires a non-empty bundles list"
        )

    # Validate free-form fields up front so we never produce a malformed YAML
    # that the receiver would reject.
    _validate_safe_string(description, "description")
    _validate_safe_string(note, "note")
    _validate_safe_string(walnut_name, "source.walnut")
    _validate_safe_string(session_id, "source.session_id")
    _validate_safe_string(engine, "source.engine")
    _validate_safe_string(plugin_version, "source.plugin_version")
    _validate_safe_string(sender, "sender")

    staging_dir = os.path.abspath(staging_dir)
    if not os.path.isdir(staging_dir):
        raise FileNotFoundError(
            "staging directory not found: {0}".format(staging_dir)
        )

    files_list = _walk_staging_files(staging_dir)
    payload_sha = compute_payload_sha256(files_list)

    if min_plugin_version is None:
        min_plugin_version = MIN_PLUGIN_VERSION
    if created is None:
        created = now_utc_iso()
    _validate_safe_string(min_plugin_version, "min_plugin_version")
    _validate_safe_string(created, "created")

    manifest = {
        "format_version": FORMAT_VERSION,
        "source_layout": source_layout,
        "min_plugin_version": min_plugin_version,
        "created": created,
        "scope": scope,
        "source": {
            "walnut": walnut_name,
            "session_id": session_id,
            "engine": engine,
            "plugin_version": plugin_version,
        },
        "sender": sender,
        "description": description,
        "note": note,
        "exclusions_applied": list(exclusions_applied or []),
        "substitutions_applied": [dict(s) for s in (substitutions_applied or [])],
        "payload_sha256": payload_sha,
        "files": files_list,
        "encryption": "none",
    }  # type: Dict[str, Any]

    # ``bundles`` only appears for scope=bundle. Snapshot/full omit the field
    # entirely so the canonical bytes do not include an empty list.
    if scope == "bundle":
        manifest["bundles"] = list(bundles or [])

    # Defensive substitution string validation -- callers may pass arbitrary
    # reasons that should not break the YAML.
    for entry in manifest["substitutions_applied"]:
        _validate_safe_string(entry.get("path", ""), "substitutions_applied.path")
        _validate_safe_string(entry.get("reason", ""), "substitutions_applied.reason")
    for excl in manifest["exclusions_applied"]:
        _validate_safe_string(excl, "exclusions_applied[]")

    # Write the manifest to disk LAST, so any validation error above leaves
    # the staging directory unchanged.
    write_manifest_yaml(manifest, os.path.join(staging_dir, "manifest.yaml"))
    return manifest


def validate_manifest(manifest):
    # type: (Dict[str, Any]) -> Tuple[bool, List[str]]
    """Validate a parsed manifest dict against the LD6 + LD20 contract.

    Returns ``(ok, errors)``. ``ok`` is True iff every required field is
    present, ``format_version`` matches the ``2.x`` regex, ``scope`` is one of
    the three known values, and per-scope rules pass (``bundle`` requires a
    non-empty ``bundles`` list, ``source_layout`` if present must be ``v2`` or
    ``v3`` -- but unknown values are warnings, not hard errors, per LD7 rule
    6 fall-through).

    Hard-fails:
    - ``format_version`` starts with ``3.`` -> the package was produced by a
      newer plugin and the receiver cannot guarantee correct interpretation
    - any required field missing
    - ``scope`` not in ``{full, bundle, snapshot}``
    - ``bundle`` scope with empty / missing ``bundles`` list
    - ``files`` not a list, or any file entry missing required keys
    """
    errors = []  # type: List[str]

    if not isinstance(manifest, dict):
        return (False, ["manifest must be a dict, got {0}".format(
            type(manifest).__name__
        )])

    # Required fields per LD20.
    required = ("format_version", "scope", "created", "files", "source",
                "payload_sha256")
    for field in required:
        if field not in manifest:
            errors.append("missing required field: {0}".format(field))

    # If we are missing the format version we cannot meaningfully continue.
    fv = manifest.get("format_version")
    if fv is not None:
        if not isinstance(fv, str):
            errors.append(
                "format_version must be a string, got {0}".format(
                    type(fv).__name__
                )
            )
        else:
            if fv.startswith("3."):
                errors.append(
                    "Package uses format_version {0}; this receiver only "
                    "supports 2.x. Upgrade the ALIVE plugin or request an "
                    "older sender.".format(fv)
                )
            elif not _FORMAT_VERSION_RE.match(fv):
                errors.append(
                    "Unsupported format_version '{0}'; expected 2.x".format(fv)
                )

    scope = manifest.get("scope")
    if scope is not None and scope not in _VALID_SCOPES:
        errors.append(
            "Invalid scope '{0}'; expected one of {1}".format(scope, _VALID_SCOPES)
        )

    if scope == "bundle":
        bundles = manifest.get("bundles")
        if not bundles or not isinstance(bundles, list):
            errors.append(
                "scope=bundle requires a non-empty bundles list"
            )

    # source_layout: tolerate unknown for forward compat; LD7 inference will
    # take over on the receive side.
    sl = manifest.get("source_layout")
    if sl is not None and sl not in _VALID_SOURCE_LAYOUTS:
        # Warning only -- not appended to errors. We could surface it via a
        # second return value, but the LD7 fall-through already handles
        # unknown layouts on the receive side.
        pass

    # Validate the files[] entries shape.
    files_field = manifest.get("files")
    if files_field is not None:
        if not isinstance(files_field, list):
            errors.append("files must be a list")
        else:
            for i, entry in enumerate(files_field):
                if not isinstance(entry, dict):
                    errors.append(
                        "files[{0}] must be a dict, got {1}".format(
                            i, type(entry).__name__
                        )
                    )
                    continue
                for key in ("path", "sha256", "size"):
                    if key not in entry:
                        errors.append(
                            "files[{0}] missing required key '{1}'".format(i, key)
                        )

    # source: must be a dict if present (we already required it above).
    src = manifest.get("source")
    if src is not None and not isinstance(src, dict):
        errors.append(
            "source must be a dict, got {0}".format(type(src).__name__)
        )

    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Stdlib-only manifest YAML reader / writer (LD20)
# ---------------------------------------------------------------------------
#
# The hand-rolled writer emits the exact subset of YAML the manifest schema
# uses: string scalars (always double-quoted for safety), string lists, one
# level of nested dicts (``source``, ``signature``), and a list of dicts
# (``files``, ``substitutions_applied``). No multi-line block style, no
# anchors, no flow style. Keys are emitted in ``_MANIFEST_FIELD_ORDER``; any
# unknown keys are appended in alphabetical order so forward-compat fields
# survive a round-trip.
#
# The reader is a regex-driven line scanner that handles the same subset and
# tolerates unknown top-level scalar fields (preserved in the dict for
# forward compat). Anything outside the subset raises ``ValueError`` so the
# parser cannot silently mis-parse a malformed file.

def _yaml_quote(value):
    # type: (str) -> str
    """Quote a string for the YAML writer (always double quotes).

    Backslashes and double quotes are escaped. Newlines are forbidden by
    ``_validate_safe_string`` upstream, but the escape is included for
    defence in depth.
    """
    if value is None:
        return '""'
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return '"{0}"'.format(s)


def _emit_scalar(key, value, indent):
    # type: (str, Any, int) -> str
    """Emit a single ``key: value`` line for the YAML writer."""
    pad = " " * indent
    if isinstance(value, bool):
        return "{0}{1}: {2}\n".format(pad, key, "true" if value else "false")
    if isinstance(value, (int, float)):
        return "{0}{1}: {2}\n".format(pad, key, value)
    if value is None:
        return "{0}{1}: \"\"\n".format(pad, key)
    return "{0}{1}: {2}\n".format(pad, key, _yaml_quote(value))


def _emit_string_list(key, items, indent):
    # type: (str, List[Any], int) -> str
    """Emit ``key:`` followed by ``- item`` lines for a list of scalars.

    Empty lists serialize as ``key: []`` so the field is preserved across a
    round trip without ambiguity.
    """
    pad = " " * indent
    if not items:
        return "{0}{1}: []\n".format(pad, key)
    out = "{0}{1}:\n".format(pad, key)
    for item in items:
        out += "{0}  - {1}\n".format(pad, _yaml_quote(item))
    return out


def _emit_dict_block(key, d, indent):
    # type: (str, Dict[str, Any], int) -> str
    """Emit a nested dict block (one level only).

    Used for ``source:``, ``signature:``, and any other future single-level
    nested dict. Keys are emitted in alphabetical order for stability.
    """
    pad = " " * indent
    out = "{0}{1}:\n".format(pad, key)
    for k in sorted(d.keys()):
        out += _emit_scalar(k, d[k], indent + 2)
    return out


def _emit_list_of_dicts(key, items, indent):
    # type: (str, List[Dict[str, Any]], int) -> str
    """Emit a list of dicts (e.g. ``files:`` and ``substitutions_applied:``).

    Each item is emitted as a ``- key: value`` block. Keys within an item are
    emitted in a fixed order: ``path`` first, then alphabetical for the rest
    so the path is the visual anchor for each entry.
    """
    pad = " " * indent
    if not items:
        return "{0}{1}: []\n".format(pad, key)
    out = "{0}{1}:\n".format(pad, key)
    for item in items:
        keys = list(item.keys())
        if "path" in keys:
            keys.remove("path")
            keys = ["path"] + sorted(keys)
        else:
            keys = sorted(keys)
        first = True
        for k in keys:
            if first:
                out += "{0}  - {1}: {2}\n".format(
                    pad, k, _yaml_quote(item[k]) if isinstance(item[k], str)
                    else item[k]
                )
                first = False
            else:
                out += "{0}    {1}: {2}\n".format(
                    pad, k, _yaml_quote(item[k]) if isinstance(item[k], str)
                    else item[k]
                )
    return out


def write_manifest_yaml(manifest_dict, output_path):
    # type: (Dict[str, Any], str) -> None
    """Serialize a manifest dict to YAML and write it atomically.

    Field order follows ``_MANIFEST_FIELD_ORDER`` for the known fields and
    appends any unknown top-level fields in alphabetical order so forward-
    compat additions survive a round trip.

    The writer dispatches per field type:
    - ``source`` and ``signature`` -> nested dict block
    - ``exclusions_applied`` and ``bundles`` -> string list
    - ``substitutions_applied`` and ``files`` -> list of dicts
    - everything else -> scalar
    """
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    out = ""
    written = set()
    known_order = list(_MANIFEST_FIELD_ORDER)
    extra_fields = sorted(
        k for k in manifest_dict.keys() if k not in known_order
    )
    for key in known_order + extra_fields:
        if key not in manifest_dict:
            continue
        val = manifest_dict[key]
        if key in ("source", "signature"):
            if isinstance(val, dict):
                out += _emit_dict_block(key, val, 0)
            else:
                out += _emit_scalar(key, val, 0)
        elif key in ("exclusions_applied", "bundles"):
            if isinstance(val, list):
                out += _emit_string_list(key, val, 0)
            else:
                out += _emit_scalar(key, val, 0)
        elif key in ("substitutions_applied", "files"):
            if isinstance(val, list):
                out += _emit_list_of_dicts(key, val, 0)
            else:
                out += _emit_scalar(key, val, 0)
        else:
            out += _emit_scalar(key, val, 0)
        written.add(key)

    # Atomic write so a crash mid-write does not leave a half-manifest behind.
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(output_path), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _yaml_unquote_strict(val):
    # type: (str) -> Any
    """Decode a single YAML scalar produced by the writer.

    Handles double-quoted strings (with backslash escapes), single-quoted
    strings, integer literals, float literals, ``true``/``false``, and bare
    strings. Used by ``read_manifest_yaml`` only.
    """
    if val == "" or val == "[]":
        return val
    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
        s = val[1:-1]
        # Decode escapes in reverse order of how they were applied.
        s = s.replace("\\r", "\r").replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return s
    if val.startswith("'") and val.endswith("'") and len(val) >= 2:
        return val[1:-1]
    lower = val.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    # Try int, then float, then bare string.
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def read_manifest_yaml(path):
    # type: (str) -> Dict[str, Any]
    """Parse a manifest YAML file (written by ``write_manifest_yaml``).

    The parser handles the exact subset the writer emits:
    - Top-level scalar lines (``key: value``)
    - Top-level ``key:`` followed by indented ``- item`` lines (string list)
    - Top-level ``key:`` followed by indented ``key: value`` pairs (nested
      dict, one level only)
    - Top-level ``key:`` followed by indented ``- key: value`` blocks (list
      of dicts)
    - ``key: []`` for empty lists

    Unknown top-level scalar fields are preserved in the result dict so
    forward-compat additions survive a round trip. Anything that does not
    match the subset raises ``ValueError`` -- silent mis-parsing would be
    worse than failing fast.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("manifest not found: {0}".format(path))

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Strip trailing newline so the line counter is exact.
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    result = {}  # type: Dict[str, Any]
    i = 0
    n = len(lines)

    top_kv_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
    indented_dash_kv_re = re.compile(r"^(\s+)-\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
    indented_dash_scalar_re = re.compile(r"^(\s+)-\s+(.*)$")
    indented_kv_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")

    while i < n:
        line = lines[i]
        if line.strip() == "" or line.lstrip().startswith("#"):
            i += 1
            continue

        m = top_kv_re.match(line)
        if not m:
            raise ValueError(
                "Malformed manifest line {0}: {1!r}".format(i + 1, line)
            )

        key = m.group(1)
        raw_val = m.group(2).strip()

        if raw_val == "[]":
            result[key] = []
            i += 1
            continue

        if raw_val == "":
            # Block follows: nested dict OR list (string list / list of dicts).
            j = i + 1
            block_lines = []  # type: List[str]
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "":
                    block_lines.append(nxt)
                    j += 1
                    continue
                # Indented? Then it belongs to this block.
                if nxt[:1] in (" ", "\t"):
                    block_lines.append(nxt)
                    j += 1
                    continue
                break

            if not block_lines or all(b.strip() == "" for b in block_lines):
                # Empty block -> empty value (treat as empty string).
                result[key] = ""
                i = j
                continue

            # Decide block type from the first non-blank child.
            first_nonblank = next(b for b in block_lines if b.strip() != "")
            stripped = first_nonblank.lstrip()
            if stripped.startswith("- "):
                # Either a string list or a list of dicts.
                # Inspect: is the first dash followed by ``word:`` or by a
                # bare scalar?
                dash_kv = indented_dash_kv_re.match(first_nonblank)
                if dash_kv:
                    # List of dicts.
                    items = _parse_list_of_dicts_block(block_lines, key, i)
                    result[key] = items
                else:
                    items = []  # type: List[Any]
                    for b in block_lines:
                        if b.strip() == "":
                            continue
                        dm = indented_dash_scalar_re.match(b)
                        if not dm:
                            raise ValueError(
                                "Malformed list item in '{0}' block: {1!r}".format(
                                    key, b
                                )
                            )
                        items.append(_yaml_unquote_strict(dm.group(2).strip()))
                    result[key] = items
            else:
                # Nested dict block.
                nested = {}  # type: Dict[str, Any]
                for b in block_lines:
                    if b.strip() == "":
                        continue
                    km = indented_kv_re.match(b)
                    if not km:
                        raise ValueError(
                            "Malformed nested dict line in '{0}' block: {1!r}".format(
                                key, b
                            )
                        )
                    nested[km.group(2)] = _yaml_unquote_strict(km.group(3).strip())
                result[key] = nested

            i = j
            continue

        # Inline scalar.
        result[key] = _yaml_unquote_strict(raw_val)
        i += 1

    return result


def _parse_list_of_dicts_block(block_lines, parent_key, start_index):
    # type: (List[str], str, int) -> List[Dict[str, Any]]
    """Parse a list-of-dicts block produced by ``_emit_list_of_dicts``.

    Each entry begins with ``  - key: value`` and is followed by zero or more
    ``    key: value`` continuation lines (deeper indent). The function
    builds a list of dicts and raises ``ValueError`` on any line that does
    not match the expected pattern.
    """
    items = []  # type: List[Dict[str, Any]]
    current = None  # type: Optional[Dict[str, Any]]
    dash_re = re.compile(r"^(\s+)-\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
    cont_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")

    dash_indent = None  # type: Optional[int]

    for raw in block_lines:
        if raw.strip() == "":
            continue
        dash = dash_re.match(raw)
        if dash:
            if current is not None:
                items.append(current)
            indent_len = len(dash.group(1))
            if dash_indent is None:
                dash_indent = indent_len
            elif indent_len != dash_indent:
                raise ValueError(
                    "Inconsistent dash indent in '{0}' list (line {1})".format(
                        parent_key, start_index + 1
                    )
                )
            current = {dash.group(2): _yaml_unquote_strict(dash.group(3).strip())}
            continue
        cont = cont_re.match(raw)
        if cont and current is not None:
            indent_len = len(cont.group(1))
            if dash_indent is None or indent_len <= dash_indent:
                raise ValueError(
                    "Continuation line not deeper than dash in '{0}' list "
                    "(line {1}): {2!r}".format(
                        parent_key, start_index + 1, raw
                    )
                )
            current[cont.group(2)] = _yaml_unquote_strict(cont.group(3).strip())
            continue
        raise ValueError(
            "Malformed entry in '{0}' list (line {1}): {2!r}".format(
                parent_key, start_index + 1, raw
            )
        )

    if current is not None:
        items.append(current)
    return items


# ---------------------------------------------------------------------------
# Package extraction (layout-agnostic)
# ---------------------------------------------------------------------------

def extract_package(input_path, output_dir=None):
    # type: (str, Optional[str]) -> Dict[str, Any]
    """Extract and validate a .walnut package.

    Extracts to a staging directory, parses and verifies the manifest,
    verifies SHA-256 checksums for every listed file, and reports any
    unlisted files as warnings.

    NOTE: this function does NOT call ``validate_manifest`` -- that lives in
    task .5 (it needs to accept any 2.x format version, including the v3
    ``2.1.0`` packages this branch will produce). Callers that need schema
    validation should pair this with the v3 validator when it lands.

    Parameters:
        input_path: path to the .walnut file
        output_dir: extraction target (temp dir if None)

    Returns a dict with:
        manifest: parsed manifest dict
        staging_path: path to the extracted files
        warnings: list of warning strings
    """
    input_path = os.path.abspath(input_path)
    warnings = []  # type: List[str]

    if not os.path.isfile(input_path):
        raise FileNotFoundError("Package not found: {0}".format(input_path))

    # Create output directory
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix=".walnut-extract-")
    else:
        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)

    # Extract archive
    safe_tar_extract(input_path, output_dir)

    # Find and parse manifest
    manifest_path = os.path.join(output_dir, "manifest.yaml")
    if not os.path.isfile(manifest_path):
        raise ValueError("Package missing manifest.yaml")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = parse_manifest(f.read())

    # Verify checksums
    ok, failures = verify_checksums(manifest, output_dir)
    if not ok:
        details = []
        for fail in failures:
            if fail["error"] == "file_missing":
                details.append("  missing: {0}".format(fail["path"]))
            else:
                details.append(
                    "  mismatch: {0} (expected {1}..., got {2}...)".format(
                        fail["path"],
                        fail["expected"][:12],
                        fail["actual"][:12],
                    )
                )
        raise ValueError(
            "Checksum verification failed:\n" + "\n".join(details)
        )

    # Check for unlisted files
    unlisted = check_unlisted_files(manifest, output_dir)
    if unlisted:
        warnings.append(
            "Package contains {0} unlisted file(s): {1}".format(
                len(unlisted), ", ".join(unlisted[:5])
            )
        )

    return {
        "manifest": manifest,
        "staging_path": output_dir,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# LD6/LD7 v2 -> v3 layout migration (receive pipeline helper)
# ---------------------------------------------------------------------------
#
# ``migrate_v2_layout`` is called by the receive pipeline (task .9) AFTER the
# package has been extracted and checksum-verified, and AFTER layout inference
# has determined the staging tree is v2-shaped. It transforms the staging
# directory in place into v3 shape so the downstream transactional swap can
# treat every package identically. The function never touches the target
# walnut -- it only reshapes the staging sandbox.
#
# Transforms, in order:
#   1. Drop ``_kernel/_generated/`` entirely (v2 projection dir, regenerated
#      on the receiver side via ``project.py`` post-swap).
#   2. Flatten ``bundles/{name}/`` -> ``{name}/`` at the staging root, with
#      collision handling: if ``{name}/`` already exists as live context at
#      the staging root, append ``-imported`` suffix.
#   3. Convert each migrated bundle's ``tasks.md`` markdown checklist into a
#      ``tasks.json`` entry list via an inline parser (no subprocess, no
#      tasks.py import). Delete the original ``tasks.md`` on success.
#
# Idempotency: a v3-shaped staging dir (no ``bundles/`` container, no
# ``_kernel/_generated/``) returns a single no-op action and empty result
# lists. Running the function twice on the same staging dir is safe.

_V2_TASKS_MD_LINE = re.compile(r"^- \[([ ~x])\]\s+(.+?)(?:\s+@(\S+))?\s*$")


def _parse_v2_tasks_md(content, bundle_name, iso_timestamp, session_id):
    # type: (str, str, str, str) -> List[Dict[str, Any]]
    """Parse a v2 ``tasks.md`` markdown checklist into v3 task dicts.

    Accepts any mix of ``- [ ]`` / ``- [~]`` / ``- [x]`` lines with optional
    trailing ``@session`` attribution. Ignores headings, blank lines, frontmatter,
    and any line that does not match the checkbox pattern. IDs are assigned
    sequentially as ``t-001``, ``t-002``, ... scoped to the parsed bundle --
    these are fresh IDs because v2 markdown tasks carry no structured identity.

    Parameters:
        content: raw ``tasks.md`` text
        bundle_name: the bundle leaf name (stored as the task's ``bundle`` field)
        iso_timestamp: migration timestamp (stored as ``created``)
        session_id: session id for attribution (used when the line has no ``@``)

    Returns a list of task dicts shaped for ``{bundle}/tasks.json``::

        [{"id": "t-001", "title": "...", "status": "active|done",
          "priority": "normal|high", "assignee": None, "due": None,
          "tags": [], "created": iso_timestamp, "session": session_id,
          "bundle": bundle_name}, ...]
    """
    tasks = []  # type: List[Dict[str, Any]]
    seq = 0

    # Strip optional YAML frontmatter so ``- [ ]`` bullets inside don't parse.
    lines = content.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break

    for raw in lines:
        m = _V2_TASKS_MD_LINE.match(raw)
        if not m:
            continue
        mark, title, session_attrib = m.group(1), m.group(2), m.group(3)
        title = title.strip()
        if not title:
            continue

        if mark == " ":
            status = "active"
            priority = "normal"
        elif mark == "~":
            status = "active"
            priority = "high"
        else:  # mark == "x"
            status = "done"
            priority = "normal"

        seq += 1
        task = {
            "id": "t-{0:03d}".format(seq),
            "title": title,
            "status": status,
            "priority": priority,
            "assignee": None,
            "due": None,
            "tags": [],
            "created": iso_timestamp,
            "session": session_attrib or session_id,
            "bundle": bundle_name,
        }
        tasks.append(task)

    return tasks


def _write_tasks_json(path, tasks):
    # type: (str, List[Dict[str, Any]]) -> None
    """Write a ``{"tasks": [...]}`` dict to ``path`` via atomic replace."""
    dir_path = os.path.dirname(path)
    if dir_path and not os.path.isdir(dir_path):
        os.makedirs(dir_path, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def migrate_v2_layout(staging_dir):
    # type: (str) -> Dict[str, Any]
    """Transform a v2 package staging directory into v3 shape in place.

    Applied to a staging dir that has ALREADY been extracted from the tar and
    validated. Does NOT touch the target walnut -- operates on staging only.

    The receive pipeline (task .9) calls this when layout inference (LD7)
    reports ``source_layout == "v2"``. Idempotent: running twice on the same
    staging dir is a no-op second time.

    Transforms (in order):
        1. Drop ``_kernel/_generated/`` entirely if present.
        2. Flatten ``bundles/{name}/`` -> ``{name}/`` at staging root, with
           ``-imported`` suffix on collision with an existing live-context
           dir of the same name.
        3. Convert each migrated bundle's ``tasks.md`` -> ``tasks.json`` via
           ``_parse_v2_tasks_md`` + ``_write_tasks_json``. Delete the original
           ``tasks.md`` after successful conversion.

    Parameters:
        staging_dir: absolute path to the extracted staging tree

    Returns a dict with keys:
        actions: List[str]          -- human-readable transform log, in order
        warnings: List[str]         -- non-fatal issues (e.g. tasks.md +
                                        tasks.json both present -> kept json)
        bundles_migrated: List[str] -- final leaf names of flattened bundles
                                        (with any ``-imported`` suffix applied)
        tasks_converted: int        -- total count of task entries written
                                        across every migrated bundle's tasks.json
        errors: List[str]           -- non-fatal errors captured per-bundle
                                        (e.g. unreadable tasks.md); the
                                        migration continues across the rest
    """
    staging_dir = os.path.abspath(staging_dir)
    result = {
        "actions": [],
        "warnings": [],
        "bundles_migrated": [],
        "tasks_converted": 0,
        "errors": [],
    }  # type: Dict[str, Any]

    if not os.path.isdir(staging_dir):
        result["errors"].append(
            "staging dir does not exist: {0}".format(staging_dir)
        )
        return result

    generated_dir = os.path.join(staging_dir, "_kernel", "_generated")
    bundles_container = os.path.join(staging_dir, "bundles")

    has_generated = os.path.isdir(generated_dir)
    if os.path.isdir(bundles_container):
        has_bundles = any(
            os.path.isdir(os.path.join(bundles_container, name))
            for name in os.listdir(bundles_container)
        )
    else:
        has_bundles = False

    # Idempotency short-circuit: already v3 shape.
    if not has_generated and not has_bundles:
        result["actions"].append("no-op (already v3 layout)")
        return result

    # --- Step 1: drop _kernel/_generated/ --------------------------------
    if has_generated:
        shutil.rmtree(generated_dir)
        result["actions"].append("Dropped _kernel/_generated/")

    # --- Step 2: flatten bundles/{name}/ -> {name}/ -----------------------
    flattened = []  # type: List[Tuple[str, str]]  # (final_name, bundle_dir)
    if os.path.isdir(bundles_container):
        # Sort for deterministic behaviour across filesystems.
        child_names = sorted(os.listdir(bundles_container))
        for name in child_names:
            src = os.path.join(bundles_container, name)
            if not os.path.isdir(src):
                # Stray files inside bundles/ are a protocol oddity; warn
                # and leave them where they are (they'll be dropped when we
                # rmtree the empty container below, so preserve instead).
                result["warnings"].append(
                    "non-directory entry in bundles/: {0}".format(name)
                )
                continue

            final_name = name
            dst = os.path.join(staging_dir, final_name)
            if os.path.exists(dst):
                final_name = "{0}-imported".format(name)
                dst = os.path.join(staging_dir, final_name)
                # Guard against a second-order collision (extremely rare:
                # both ``name`` and ``name-imported`` already exist).
                if os.path.exists(dst):
                    result["errors"].append(
                        "cannot flatten bundles/{0}: both {0} and "
                        "{0}-imported already exist at staging root".format(
                            name
                        )
                    )
                    continue

            shutil.move(src, dst)
            flattened.append((final_name, dst))
            if final_name == name:
                result["actions"].append(
                    "Flattened bundles/{0} -> {0}".format(name)
                )
            else:
                result["actions"].append(
                    "Flattened bundles/{0} -> {1} (collision suffix)".format(
                        name, final_name
                    )
                )

        # Remove empty bundles/ container.
        try:
            remaining = os.listdir(bundles_container)
        except OSError:
            remaining = []
        if not remaining:
            try:
                os.rmdir(bundles_container)
            except OSError as exc:
                result["warnings"].append(
                    "could not remove empty bundles/ dir: {0}".format(exc)
                )
        else:
            result["warnings"].append(
                "bundles/ container not empty after flatten; "
                "{0} entries remain".format(len(remaining))
            )

    result["bundles_migrated"] = [name for name, _ in flattened]

    # --- Step 3: convert {bundle}/tasks.md -> tasks.json ------------------
    iso_timestamp = now_utc_iso()
    session_id = resolve_session_id()

    for final_name, bundle_dir in flattened:
        tasks_md = os.path.join(bundle_dir, "tasks.md")
        tasks_json = os.path.join(bundle_dir, "tasks.json")

        if not os.path.isfile(tasks_md):
            continue  # bundle had no markdown tasks; nothing to convert

        if os.path.isfile(tasks_json):
            # Both present -- prefer the existing JSON, warn, leave tasks.md
            # in place for the human to reconcile post-import.
            result["warnings"].append(
                "bundle '{0}' has both tasks.md and tasks.json; "
                "kept tasks.json, left tasks.md untouched".format(final_name)
            )
            continue

        try:
            with open(tasks_md, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            result["errors"].append(
                "failed to read {0}/tasks.md: {1}".format(final_name, exc)
            )
            continue

        parsed = _parse_v2_tasks_md(
            content, final_name, iso_timestamp, session_id
        )

        try:
            _write_tasks_json(tasks_json, parsed)
        except OSError as exc:
            result["errors"].append(
                "failed to write {0}/tasks.json: {1}".format(final_name, exc)
            )
            continue

        try:
            os.remove(tasks_md)
        except OSError as exc:
            result["warnings"].append(
                "converted {0}/tasks.md but could not remove original: "
                "{1}".format(final_name, exc)
            )

        result["tasks_converted"] += len(parsed)
        result["actions"].append(
            "Converted {0}/tasks.md -> tasks.json ({1} tasks)".format(
                final_name, len(parsed)
            )
        )

    return result


# ---------------------------------------------------------------------------
# Encryption / Decryption
# ---------------------------------------------------------------------------

def _get_openssl():
    # type: () -> Dict[str, Any]
    """Get the openssl binary path, raising RuntimeError if not found."""
    ssl = detect_openssl()
    if ssl["binary"] is None:
        raise RuntimeError("openssl not found on this system")
    return ssl


def encrypt_package(package_path, output_path=None, mode="passphrase",
                    recipient_pubkey=None):
    # type: (str, Optional[str], str, Optional[str]) -> str
    """Encrypt a .walnut package.

    Two modes:

    - ``passphrase`` -- AES-256-CBC with PBKDF2 (600k iterations). Passphrase
      is read from the ``WALNUT_PASSPHRASE`` env var.
    - ``rsa`` -- random 256-bit AES key, encrypt payload with AES, wrap key
      with RSA-OAEP-SHA256 via ``pkeyutl``. The AES key is random, not
      password-derived, so PBKDF2 is unnecessary on the AES step.

    The output is a new .walnut file containing:

    - ``manifest.yaml`` (cleartext, updated with ``encrypted: true``)
    - ``payload.enc`` (encrypted inner tar.gz)
    - ``payload.key`` (RSA mode only -- wrapped AES key)

    Parameters:
        package_path: path to the unencrypted .walnut file
        output_path: path for the encrypted .walnut file (auto-derived if None)
        mode: ``"passphrase"`` or ``"rsa"``
        recipient_pubkey: path to recipient's RSA public key (rsa mode)

    Returns the path to the encrypted .walnut file.
    """
    package_path = os.path.abspath(package_path)
    ssl = _get_openssl()

    if mode == "passphrase":
        passphrase = os.environ.get("WALNUT_PASSPHRASE", "")
        if not passphrase:
            raise ValueError(
                "WALNUT_PASSPHRASE environment variable not set. "
                "Set it before encrypting: "
                "export WALNUT_PASSPHRASE='your passphrase'"
            )
        if not ssl["supports_pbkdf2"]:
            raise RuntimeError(
                "OpenSSL {0} does not support -pbkdf2. "
                "Upgrade to LibreSSL >= 3.1 or OpenSSL >= 1.1.1".format(ssl["version"])
            )
    elif mode == "rsa":
        if not recipient_pubkey:
            raise ValueError("RSA mode requires recipient_pubkey path")
        recipient_pubkey = os.path.abspath(recipient_pubkey)
        if not os.path.isfile(recipient_pubkey):
            raise FileNotFoundError(
                "Recipient public key not found: {0}".format(recipient_pubkey)
            )
        if not ssl["supports_pkeyutl"]:
            raise RuntimeError(
                "OpenSSL {0} does not support pkeyutl".format(ssl["version"])
            )
    else:
        raise ValueError("Unknown encryption mode: {0}".format(mode))

    # Extract the package to get manifest and payload
    work_dir = tempfile.mkdtemp(prefix=".walnut-encrypt-")

    try:
        # Extract original package
        safe_tar_extract(package_path, work_dir)

        # Read manifest
        manifest_path = os.path.join(work_dir, "manifest.yaml")
        if not os.path.isfile(manifest_path):
            raise ValueError("Package missing manifest.yaml")

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_content = f.read()

        manifest = parse_manifest(manifest_content)

        # Create inner tar.gz of all files except manifest
        inner_dir = tempfile.mkdtemp(prefix=".walnut-inner-", dir=work_dir)
        for entry in manifest.get("files", []):
            src = os.path.join(work_dir, entry["path"].replace("/", os.sep))
            dst = os.path.join(inner_dir, entry["path"].replace("/", os.sep))
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

        inner_tar = os.path.join(work_dir, "inner.tar.gz")
        safe_tar_create(inner_dir, inner_tar)

        # Build encrypted output staging directory
        enc_staging = tempfile.mkdtemp(prefix=".walnut-enc-stage-", dir=work_dir)
        payload_enc = os.path.join(enc_staging, "payload.enc")

        if mode == "passphrase":
            # AES-256-CBC with PBKDF2, 600k iterations
            proc = subprocess.run(
                [ssl["binary"], "enc", "-aes-256-cbc", "-salt",
                 "-pbkdf2", "-iter", "600000",
                 "-in", inner_tar, "-out", payload_enc,
                 "-pass", "env:WALNUT_PASSPHRASE"],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "WALNUT_PASSPHRASE": passphrase},
            )

            if proc.returncode != 0:
                raise RuntimeError(
                    "Passphrase encryption failed: {0}".format(proc.stderr)
                )

        elif mode == "rsa":
            # Generate random 256-bit AES key
            aes_key_path = os.path.join(work_dir, "aes.key")
            proc = subprocess.run(
                [ssl["binary"], "rand", "-out", aes_key_path, "32"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "Failed to generate random key: {0}".format(proc.stderr)
                )

            # Read the raw key bytes for use as hex passphrase
            with open(aes_key_path, "rb") as f:
                aes_key_bytes = f.read()
            aes_key_hex = aes_key_bytes.hex()

            # Generate random IV.
            iv_proc = subprocess.run(
                [ssl["binary"], "rand", "-hex", "16"],
                capture_output=True, text=True, timeout=10,
            )
            if iv_proc.returncode != 0:
                raise RuntimeError(
                    "Failed to generate IV: {0}".format(iv_proc.stderr)
                )
            iv_hex = iv_proc.stdout.strip()

            # Encrypt inner tar with AES using the random key. Use -K (hex
            # key) and -iv instead of -pass to avoid PBKDF2 overhead on a
            # random key.
            proc = subprocess.run(
                [ssl["binary"], "enc", "-aes-256-cbc",
                 "-K", aes_key_hex, "-iv", iv_hex,
                 "-in", inner_tar, "-out", payload_enc],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "AES encryption failed: {0}".format(proc.stderr)
                )

            # Wrap AES key + IV with RSA-OAEP-SHA256.
            # Pack key (32 bytes) + iv (16 bytes hex -> 16 bytes raw).
            iv_bytes = bytes.fromhex(iv_hex)
            key_material = aes_key_bytes + iv_bytes
            key_material_path = os.path.join(work_dir, "key_material.bin")
            with open(key_material_path, "wb") as f:
                f.write(key_material)

            payload_key_path = os.path.join(enc_staging, "payload.key")
            proc = subprocess.run(
                [ssl["binary"], "pkeyutl", "-encrypt",
                 "-pubin", "-inkey", recipient_pubkey,
                 "-pkeyopt", "rsa_padding_mode:oaep",
                 "-pkeyopt", "rsa_oaep_md:sha256",
                 "-in", key_material_path, "-out", payload_key_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "RSA key wrapping failed: {0}".format(proc.stderr)
                )

            # Securely clean up key material
            _secure_delete(aes_key_path)
            _secure_delete(key_material_path)

        # Update manifest to indicate encryption
        updated_manifest = _update_manifest_encrypted(manifest_content, True)
        with open(os.path.join(enc_staging, "manifest.yaml"), "w",
                  encoding="utf-8") as f:
            f.write(updated_manifest)

        # Create output .walnut
        if output_path is None:
            base, ext = os.path.splitext(package_path)
            output_path = base + "-encrypted" + ext
        output_path = os.path.abspath(output_path)

        safe_tar_create(enc_staging, output_path)
        return output_path

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def decrypt_package(encrypted_path, output_path=None, private_key=None):
    # type: (str, Optional[str], Optional[str]) -> str
    """Decrypt a .walnut package.

    Auto-detects mode based on archive contents:

    - ``payload.key`` present -> RSA mode (requires ``private_key`` path)
    - ``payload.enc`` only    -> passphrase mode (reads ``WALNUT_PASSPHRASE``)

    Per LD5, passphrase decrypt walks a fallback chain when the primary
    pbkdf2/iter combo fails so legacy v2 (and earlier) packages still open
    transparently:

        1. ``-md sha256 -pbkdf2 -iter 600000``  (v2.1.0 sender, default)
        2. ``-md sha256 -pbkdf2 -iter 100000``  (early v2 sender)
        3. ``-md sha256 -pbkdf2``               (defaults, no explicit iter)
        4. ``-md md5``                          (v1 / pre-pbkdf2 legacy)

    All four failing yields a hard error with manual-debug guidance.
    """
    encrypted_path = os.path.abspath(encrypted_path)
    ssl = _get_openssl()

    work_dir = tempfile.mkdtemp(prefix=".walnut-decrypt-")

    try:
        # Extract encrypted package
        safe_tar_extract(encrypted_path, work_dir)

        payload_enc = os.path.join(work_dir, "payload.enc")
        payload_key = os.path.join(work_dir, "payload.key")
        manifest_path = os.path.join(work_dir, "manifest.yaml")

        if not os.path.isfile(payload_enc):
            raise ValueError("Package is not encrypted (no payload.enc)")
        if not os.path.isfile(manifest_path):
            raise ValueError("Package missing manifest.yaml")

        inner_tar = os.path.join(work_dir, "inner.tar.gz")

        if os.path.isfile(payload_key):
            # RSA mode
            if not private_key:
                raise ValueError(
                    "RSA-encrypted package requires --private-key path"
                )
            private_key = os.path.abspath(private_key)
            if not os.path.isfile(private_key):
                raise FileNotFoundError(
                    "Private key not found: {0}".format(private_key)
                )

            # Unwrap AES key + IV with RSA
            key_material_path = os.path.join(work_dir, "key_material.bin")
            proc = subprocess.run(
                [ssl["binary"], "pkeyutl", "-decrypt",
                 "-inkey", private_key,
                 "-pkeyopt", "rsa_padding_mode:oaep",
                 "-pkeyopt", "rsa_oaep_md:sha256",
                 "-in", payload_key, "-out", key_material_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "RSA key unwrapping failed: {0}".format(proc.stderr)
                )

            # Extract key (32 bytes) + IV (16 bytes)
            with open(key_material_path, "rb") as f:
                key_material = f.read()
            if len(key_material) < 48:
                raise ValueError(
                    "Invalid key material length: {0} (expected 48 bytes)".format(
                        len(key_material)
                    )
                )

            aes_key_hex = key_material[:32].hex()
            iv_hex = key_material[32:48].hex()

            # Decrypt with AES
            proc = subprocess.run(
                [ssl["binary"], "enc", "-d", "-aes-256-cbc",
                 "-K", aes_key_hex, "-iv", iv_hex,
                 "-in", payload_enc, "-out", inner_tar],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "AES decryption failed: {0}".format(proc.stderr)
                )

            _secure_delete(key_material_path)

        else:
            # Passphrase mode -- LD5 fallback chain.
            passphrase = os.environ.get("WALNUT_PASSPHRASE", "")
            if not passphrase:
                raise ValueError(
                    "WALNUT_PASSPHRASE environment variable not set"
                )

            fallbacks = [
                # (description, extra openssl args)
                ("v2.1.0 default (pbkdf2, iter=600000)",
                 ["-md", "sha256", "-pbkdf2", "-iter", "600000"]),
                ("early v2 (pbkdf2, iter=100000)",
                 ["-md", "sha256", "-pbkdf2", "-iter", "100000"]),
                ("v2 defaults (pbkdf2, no iter)",
                 ["-md", "sha256", "-pbkdf2"]),
                ("v1 legacy (md5, no pbkdf2)",
                 ["-md", "md5"]),
            ]

            last_err = ""
            success = False
            for desc, extra in fallbacks:
                proc = subprocess.run(
                    [ssl["binary"], "enc", "-d", "-aes-256-cbc",
                     *extra,
                     "-in", payload_enc, "-out", inner_tar,
                     "-pass", "env:WALNUT_PASSPHRASE"],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ, "WALNUT_PASSPHRASE": passphrase},
                )
                if proc.returncode == 0:
                    success = True
                    break
                last_err = "{0}: {1}".format(desc, proc.stderr.strip())

            if not success:
                raise RuntimeError(
                    "Cannot decrypt package -- wrong passphrase or "
                    "unsupported format. Try `openssl enc -d` manually to "
                    "debug. Last error: {0}".format(last_err)
                )

        # Build decrypted output staging directory
        dec_staging = tempfile.mkdtemp(prefix=".walnut-dec-stage-", dir=work_dir)

        # Extract inner tar to staging
        safe_tar_extract(inner_tar, dec_staging)

        # Copy manifest (update encrypted flag)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_content = f.read()
        updated = _update_manifest_encrypted(manifest_content, False)
        with open(os.path.join(dec_staging, "manifest.yaml"), "w",
                  encoding="utf-8") as f:
            f.write(updated)

        # Create output .walnut
        if output_path is None:
            base, ext = os.path.splitext(encrypted_path)
            # Strip -encrypted suffix if present
            if base.endswith("-encrypted"):
                base = base[:-len("-encrypted")]
            output_path = base + "-decrypted" + ext
        output_path = os.path.abspath(output_path)

        safe_tar_create(dec_staging, output_path)
        return output_path

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _update_manifest_encrypted(manifest_content, encrypted):
    # type: (str, bool) -> str
    """Update the ``encrypted:`` field in manifest YAML content."""
    val = "true" if encrypted else "false"
    updated = re.sub(
        r"^(encrypted:\s*).*$",
        "encrypted: {0}".format(val),
        manifest_content,
        count=1,
        flags=re.MULTILINE,
    )
    return updated


def _secure_delete(path):
    # type: (str) -> None
    """Overwrite file with zeros before deleting (best-effort)."""
    try:
        size = os.path.getsize(path)
        with open(path, "wb") as f:
            f.write(b"\x00" * size)
            f.flush()
            os.fsync(f.fileno())
        os.unlink(path)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# LD23 -- Peer keyring helpers
# ---------------------------------------------------------------------------
#
# Per LD23, peer public keys live in ``$HOME/.alive/relay/keys/peers/`` and the
# pubkey_id -> peer name index lives in ``$HOME/.alive/relay/keys/index.json``.
# These helpers expose three operations: register, lookup-by-name, and
# lookup-by-pubkey_id. They are deliberately minimal; the keyring is config
# data the user owns, so the helpers never invent files or rewrite the index
# without explicit caller intent.
#
# pubkey_id derivation: the SHA-256 of the DER encoding of the public key,
# truncated to 16 hex characters (64 bits). Plain hex, never base64. Stable
# across PEM reformatting because DER is canonical.

def _alive_relay_keys_dir():
    # type: () -> str
    """Return the absolute path of the local relay keys directory."""
    return os.path.expanduser(os.path.join("~", ".alive", "relay", "keys"))


def _alive_relay_index_path():
    # type: () -> str
    """Return the absolute path of the keyring index.json."""
    return os.path.join(_alive_relay_keys_dir(), "index.json")


def compute_pubkey_id(pem_path):
    # type: (str) -> str
    """Return the 16-char hex pubkey_id derived from a PEM file per LD23.

    Algorithm:
        1. Convert the PEM public key to DER via ``openssl pkey -pubin``.
        2. SHA-256 the DER bytes.
        3. Hex-encode and truncate to the first 16 characters (64 bits).

    Stable across PEM reformatting because DER encoding is canonical. The
    truncation matches LD23 examples (e.g. ``a1b2c3d4e5f67890``) and gives
    the user a CLI-friendly identifier free of ``+`` / ``/`` characters that
    would force quoting.
    """
    pem_path = os.path.abspath(pem_path)
    if not os.path.isfile(pem_path):
        raise FileNotFoundError("PEM file not found: {0}".format(pem_path))
    ssl = _get_openssl()
    proc = subprocess.run(
        [ssl["binary"], "pkey", "-pubin", "-in", pem_path, "-outform", "DER"],
        capture_output=True, timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "compute_pubkey_id: openssl pkey -pubin failed for {0}: {1}".format(
                pem_path, proc.stderr.decode("utf-8", errors="replace").strip(),
            )
        )
    der_bytes = proc.stdout
    if not der_bytes:
        raise RuntimeError(
            "compute_pubkey_id: openssl produced no DER output for {0}".format(
                pem_path
            )
        )
    return hashlib.sha256(der_bytes).hexdigest()[:16]


def resolve_peer_pubkey_path(peer_name, keys_dir=None):
    # type: (str, Optional[str]) -> Optional[str]
    """Look up a peer's PEM file by handle. Returns the absolute path or None.

    Reads from ``$HOME/.alive/relay/keys/peers/{peer_name}.pem`` by default.
    The ``keys_dir`` override is provided for tests so they can point at a
    sandbox without touching the real user home.
    """
    if not peer_name:
        return None
    base = keys_dir if keys_dir is not None else _alive_relay_keys_dir()
    candidate = os.path.join(base, "peers", "{0}.pem".format(peer_name))
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return None


def resolve_pubkey_id_lookup(pubkey_id, keys_dir=None):
    # type: (str, Optional[str]) -> Optional[Tuple[str, str]]
    """Look up a peer record by pubkey_id. Returns ``(peer_name, abs_path)``
    or None when the pubkey_id is unknown.

    Reads ``index.json``. Missing index file -> empty index, lookup returns
    None. Malformed index file -> hard error so the user notices the corrupt
    config rather than silently failing every signature verification.
    """
    if not pubkey_id:
        return None
    base = keys_dir if keys_dir is not None else _alive_relay_keys_dir()
    index_path = os.path.join(base, "index.json")
    if not os.path.isfile(index_path):
        return None
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (IOError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "resolve_pubkey_id_lookup: keyring index is malformed: {0} ({1})".format(
                index_path, exc,
            )
        )
    if not isinstance(data, dict):
        raise RuntimeError(
            "resolve_pubkey_id_lookup: keyring index is malformed (not a dict): "
            "{0}".format(index_path)
        )
    pubkeys = data.get("pubkeys") or {}
    if not isinstance(pubkeys, dict):
        return None
    record = pubkeys.get(pubkey_id)
    if not isinstance(record, dict):
        return None
    peer_name = record.get("peer_name")
    rel_path = record.get("path")
    if not isinstance(peer_name, str) or not isinstance(rel_path, str):
        return None
    abs_path = os.path.join(base, rel_path)
    if not os.path.isabs(abs_path):
        abs_path = os.path.abspath(abs_path)
    return (peer_name, abs_path)


def register_peer_pubkey(peer_name, pem_content, keys_dir=None,
                         added_by="manual"):
    # type: (str, bytes, Optional[str], str) -> str
    """Write a peer's PEM and update ``index.json``. Returns the pubkey_id.

    Idempotent: rewrites the PEM and updates the index entry if the peer
    already exists. The on-disk format matches LD23 (``peers/{name}.pem`` +
    ``index.json`` with ``pubkeys`` map and optional ``local_pubkey_id``).

    ``added_by`` should be ``"manual"`` for direct user placement or
    ``"relay-accept"`` for the ``/alive:relay accept`` flow.

    Tests pass an explicit ``keys_dir`` so the helper does not touch the
    user's actual ``$HOME/.alive/relay/keys/`` tree.
    """
    if not peer_name or not isinstance(peer_name, str):
        raise ValueError("register_peer_pubkey: peer_name must be a non-empty string")
    if any(ch in peer_name for ch in ("/", "\\", "..")):
        raise ValueError(
            "register_peer_pubkey: invalid peer_name {0!r}".format(peer_name)
        )
    if not isinstance(pem_content, (bytes, bytearray)):
        raise TypeError(
            "register_peer_pubkey: pem_content must be bytes, got {0}".format(
                type(pem_content).__name__
            )
        )
    base = keys_dir if keys_dir is not None else _alive_relay_keys_dir()
    peers_dir = os.path.join(base, "peers")
    os.makedirs(peers_dir, exist_ok=True)
    pem_path = os.path.join(peers_dir, "{0}.pem".format(peer_name))
    with open(pem_path, "wb") as f:
        f.write(pem_content)

    pubkey_id = compute_pubkey_id(pem_path)

    index_path = os.path.join(base, "index.json")
    if os.path.isfile(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (IOError, OSError, ValueError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    pubkeys = data.get("pubkeys")
    if not isinstance(pubkeys, dict):
        pubkeys = {}
    pubkeys[pubkey_id] = {
        "peer_name": peer_name,
        "path": "peers/{0}.pem".format(peer_name),
        "added_at": now_utc_iso(),
        "added_by": added_by,
    }
    data["pubkeys"] = pubkeys
    atomic_json_write(index_path, data)
    return pubkey_id


# ---------------------------------------------------------------------------
# LD21 -- RSA hybrid encryption envelope (canonical, format 2.1.0)
# ---------------------------------------------------------------------------
#
# The LD21 RSA hybrid envelope is an OUTER UNCOMPRESSED tar containing exactly
# two members at the tar root:
#
#     rsa-envelope-v1.json    -- recipients[] with RSA-OAEP-wrapped AES keys
#     payload.enc             -- AES-256-CBC encrypted inner-payload.tar.gz
#
# The inner payload (after AES decryption) is a STANDARD gzipped tarball with
# the same internal structure as the unencrypted case (manifest.yaml + _kernel
# + bundle dirs). There is NO outer manifest -- the only manifest is the one
# inside the decrypted inner tar. ``import_id``, signature verification, and
# checksum verification all run against that inner manifest.
#
# Multi-recipient: ``recipients[]`` holds one entry per peer; every entry
# wraps the SAME random AES key with that peer's public key. Decryption tries
# every recipient with the local private key and uses the first that succeeds.

_RSA_ENVELOPE_FILENAME = "rsa-envelope-v1.json"
_RSA_PAYLOAD_FILENAME = "payload.enc"
_RSA_ENVELOPE_VERSION = 1
_RSA_PAYLOAD_ALGO = "aes-256-cbc"


def encrypt_rsa_hybrid(payload_tar_gz_bytes, recipient_pubkey_pems,
                       aes_mode="aes-256-cbc"):
    # type: (bytes, List[str], str) -> bytes
    """Build an LD21 RSA hybrid envelope tar from a plaintext inner payload.

    Parameters:
        payload_tar_gz_bytes: bytes of an already-built inner ``payload.tar.gz``.
            Callers (``create_package``) build the gzipped tar in a temp file
            and pass the bytes here.
        recipient_pubkey_pems: list of paths to recipient PEM files. Each PEM
            is wrapped via RSA-OAEP-SHA256 and added to ``recipients[]``.
        aes_mode: payload cipher; only ``aes-256-cbc`` is currently supported.

    Returns:
        Bytes of the outer uncompressed tarball containing exactly
        ``rsa-envelope-v1.json`` and ``payload.enc``.

    Raises:
        ValueError -- on bad arguments (no recipients, unsupported cipher,
            empty payload)
        FileNotFoundError -- if any recipient PEM is missing
        RuntimeError -- on any openssl failure (encryption, key wrapping)
    """
    if aes_mode != _RSA_PAYLOAD_ALGO:
        raise ValueError(
            "encrypt_rsa_hybrid: unsupported aes_mode {0!r}; "
            "only {1!r} is supported".format(aes_mode, _RSA_PAYLOAD_ALGO)
        )
    if not recipient_pubkey_pems:
        raise ValueError(
            "encrypt_rsa_hybrid: at least one recipient PEM is required"
        )
    if not payload_tar_gz_bytes:
        raise ValueError("encrypt_rsa_hybrid: payload_tar_gz_bytes is empty")

    ssl = _get_openssl()
    if not ssl["supports_pkeyutl"]:
        raise RuntimeError(
            "encrypt_rsa_hybrid: openssl {0} does not support pkeyutl".format(
                ssl["version"]
            )
        )

    work_dir = tempfile.mkdtemp(prefix=".walnut-rsa-hybrid-enc-")
    try:
        # 1. Generate a random 32-byte AES key via ``openssl rand`` (RAW
        #    bytes, not hex -- the LD21 spec is explicit on this).
        aes_key_path = os.path.join(work_dir, "aes.key")
        proc = subprocess.run(
            [ssl["binary"], "rand", "-out", aes_key_path, "32"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "encrypt_rsa_hybrid: aes key generation failed: {0}".format(
                    proc.stderr.strip()
                )
            )
        with open(aes_key_path, "rb") as f:
            aes_key_bytes = f.read()
        if len(aes_key_bytes) != 32:
            raise RuntimeError(
                "encrypt_rsa_hybrid: openssl produced {0} bytes (expected 32)".format(
                    len(aes_key_bytes)
                )
            )
        aes_key_hex = aes_key_bytes.hex()

        # 2. Generate a random 16-byte IV. Use raw bytes (not hex output) so
        #    the IV can be base64-encoded into the envelope verbatim.
        iv_path = os.path.join(work_dir, "aes.iv")
        proc = subprocess.run(
            [ssl["binary"], "rand", "-out", iv_path, "16"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "encrypt_rsa_hybrid: iv generation failed: {0}".format(
                    proc.stderr.strip()
                )
            )
        with open(iv_path, "rb") as f:
            iv_bytes = f.read()
        if len(iv_bytes) != 16:
            raise RuntimeError(
                "encrypt_rsa_hybrid: openssl produced {0} iv bytes "
                "(expected 16)".format(len(iv_bytes))
            )
        iv_hex = iv_bytes.hex()
        iv_b64 = base64.b64encode(iv_bytes).decode("ascii")

        # 3. AES-256-CBC the inner payload to ``payload.enc`` using -K/-iv
        #    so we skip PBKDF2 (the AES key is already random, not password
        #    derived).
        inner_path = os.path.join(work_dir, "inner.tar.gz")
        with open(inner_path, "wb") as f:
            f.write(payload_tar_gz_bytes)
        payload_enc_path = os.path.join(work_dir, _RSA_PAYLOAD_FILENAME)
        proc = subprocess.run(
            [ssl["binary"], "enc", "-aes-256-cbc",
             "-K", aes_key_hex, "-iv", iv_hex,
             "-in", inner_path, "-out", payload_enc_path],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "encrypt_rsa_hybrid: aes encryption failed: {0}".format(
                    proc.stderr.strip()
                )
            )

        # 4. For each recipient pubkey: wrap the AES key with RSA-OAEP-SHA256
        #    and append to recipients[]. The pubkey_id is computed from the
        #    DER bytes of the recipient's public key.
        recipients = []  # type: List[Dict[str, str]]
        for pem_path in recipient_pubkey_pems:
            pem_path = os.path.abspath(pem_path)
            if not os.path.isfile(pem_path):
                raise FileNotFoundError(
                    "encrypt_rsa_hybrid: recipient pubkey not found: {0}".format(
                        pem_path
                    )
                )
            wrapped_path = os.path.join(
                work_dir, "wrapped-{0}.bin".format(len(recipients))
            )
            proc = subprocess.run(
                [ssl["binary"], "pkeyutl", "-encrypt",
                 "-pubin", "-inkey", pem_path,
                 "-pkeyopt", "rsa_padding_mode:oaep",
                 "-pkeyopt", "rsa_oaep_md:sha256",
                 "-in", aes_key_path, "-out", wrapped_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "encrypt_rsa_hybrid: rsa key wrap failed for {0}: {1}".format(
                        pem_path, proc.stderr.strip()
                    )
                )
            with open(wrapped_path, "rb") as f:
                wrapped_bytes = f.read()
            pubkey_id = compute_pubkey_id(pem_path)
            recipients.append({
                "pubkey_id": pubkey_id,
                "key_enc_b64": base64.b64encode(wrapped_bytes).decode("ascii"),
            })

        # 5. Build the envelope JSON. Field order matches LD21 for human
        #    inspection but receivers parse it order-independently.
        envelope = {
            "version": _RSA_ENVELOPE_VERSION,
            "recipients": recipients,
            "payload_algo": _RSA_PAYLOAD_ALGO,
            "payload_iv_b64": iv_b64,
        }
        envelope_bytes = json.dumps(
            envelope, indent=2, sort_keys=True,
        ).encode("utf-8") + b"\n"
        envelope_path = os.path.join(work_dir, _RSA_ENVELOPE_FILENAME)
        with open(envelope_path, "wb") as f:
            f.write(envelope_bytes)

        # 6. Build the OUTER uncompressed tar containing exactly the two
        #    members. We do NOT use safe_tar_create here because that helper
        #    writes a ``w:gz`` archive and we need an uncompressed tar so
        #    receivers can sniff it via tar magic.
        outer_path = os.path.join(work_dir, "outer.tar")
        with tarfile.open(outer_path, "w") as tar:
            tar.add(envelope_path, arcname=_RSA_ENVELOPE_FILENAME)
            tar.add(payload_enc_path, arcname=_RSA_PAYLOAD_FILENAME)
        with open(outer_path, "rb") as f:
            outer_bytes = f.read()

        # Best-effort cleanup of the AES key bytes on disk before the temp
        # dir is removed. ``_secure_delete`` is in-process best effort.
        _secure_delete(aes_key_path)
        _secure_delete(iv_path)
        return outer_bytes
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def decrypt_rsa_hybrid(outer_tar_bytes, private_key_path):
    # type: (bytes, str) -> bytes
    """Decrypt an LD21 RSA hybrid envelope. Returns inner payload bytes.

    Parameters:
        outer_tar_bytes: raw bytes of the outer uncompressed tar (the
            ``.walnut`` file's contents for an RSA-encrypted package).
        private_key_path: path to the local RSA private key PEM. Tries every
            recipient in the envelope; the first that decrypts wins.

    Returns:
        Bytes of the decrypted ``inner-payload.tar.gz`` -- a standard gzipped
        tar that the caller can write to disk and ``safe_extractall``.

    Raises:
        ValueError -- malformed envelope, missing members, version mismatch
        FileNotFoundError -- private key path missing
        RuntimeError -- no recipient matches the local key, openssl failure
    """
    if not isinstance(outer_tar_bytes, (bytes, bytearray)):
        raise TypeError(
            "decrypt_rsa_hybrid: outer_tar_bytes must be bytes, got {0}".format(
                type(outer_tar_bytes).__name__
            )
        )
    if not outer_tar_bytes:
        raise ValueError("decrypt_rsa_hybrid: outer_tar_bytes is empty")
    if not private_key_path:
        raise ValueError("decrypt_rsa_hybrid: private_key_path is required")
    private_key_path = os.path.abspath(private_key_path)
    if not os.path.isfile(private_key_path):
        raise FileNotFoundError(
            "decrypt_rsa_hybrid: private key not found: {0}".format(private_key_path)
        )

    ssl = _get_openssl()
    if not ssl["supports_pkeyutl"]:
        raise RuntimeError(
            "decrypt_rsa_hybrid: openssl {0} does not support pkeyutl".format(
                ssl["version"]
            )
        )

    work_dir = tempfile.mkdtemp(prefix=".walnut-rsa-hybrid-dec-")
    try:
        outer_path = os.path.join(work_dir, "outer.tar")
        with open(outer_path, "wb") as f:
            f.write(outer_tar_bytes)

        # 1. Open the outer tar uncompressed and assert exactly the two
        #    expected members. Reject anything else (extra members, missing
        #    members, wrong names) per LD21.
        try:
            with tarfile.open(outer_path, "r:*") as tar:
                members = {m.name: m for m in tar.getmembers() if m.isfile()}
                # Materialise both members to disk for openssl.
                for required in (_RSA_ENVELOPE_FILENAME, _RSA_PAYLOAD_FILENAME):
                    if required not in members:
                        raise ValueError(
                            "decrypt_rsa_hybrid: outer tar missing member "
                            "{0!r}".format(required)
                        )
                extra = sorted(set(members.keys()) - {
                    _RSA_ENVELOPE_FILENAME, _RSA_PAYLOAD_FILENAME,
                })
                if extra:
                    raise ValueError(
                        "decrypt_rsa_hybrid: outer tar has unexpected members "
                        "{0}".format(extra)
                    )
                envelope_path = os.path.join(work_dir, _RSA_ENVELOPE_FILENAME)
                payload_enc_path = os.path.join(work_dir, _RSA_PAYLOAD_FILENAME)
                env_member = tar.extractfile(members[_RSA_ENVELOPE_FILENAME])
                if env_member is None:
                    raise ValueError(
                        "decrypt_rsa_hybrid: cannot read {0} from outer tar".format(
                            _RSA_ENVELOPE_FILENAME
                        )
                    )
                with open(envelope_path, "wb") as f:
                    f.write(env_member.read())
                payload_member = tar.extractfile(members[_RSA_PAYLOAD_FILENAME])
                if payload_member is None:
                    raise ValueError(
                        "decrypt_rsa_hybrid: cannot read {0} from outer tar".format(
                            _RSA_PAYLOAD_FILENAME
                        )
                    )
                with open(payload_enc_path, "wb") as f:
                    f.write(payload_member.read())
        except (tarfile.TarError, OSError) as exc:
            raise ValueError(
                "decrypt_rsa_hybrid: outer tar is corrupt: {0}".format(exc)
            )

        # 2. Parse the envelope and validate version + algo.
        try:
            with open(envelope_path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
        except (IOError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "decrypt_rsa_hybrid: envelope JSON is malformed: {0}".format(exc)
            )
        if not isinstance(envelope, dict):
            raise ValueError(
                "decrypt_rsa_hybrid: envelope is not a JSON object"
            )
        version = envelope.get("version")
        if version != _RSA_ENVELOPE_VERSION:
            raise ValueError(
                "decrypt_rsa_hybrid: unsupported envelope version {0!r} "
                "(expected {1})".format(version, _RSA_ENVELOPE_VERSION)
            )
        payload_algo = envelope.get("payload_algo")
        if payload_algo != _RSA_PAYLOAD_ALGO:
            raise ValueError(
                "decrypt_rsa_hybrid: unsupported payload_algo {0!r} "
                "(expected {1!r})".format(payload_algo, _RSA_PAYLOAD_ALGO)
            )
        recipients = envelope.get("recipients") or []
        if not isinstance(recipients, list) or not recipients:
            raise ValueError(
                "decrypt_rsa_hybrid: envelope has no recipients"
            )
        iv_b64 = envelope.get("payload_iv_b64") or ""
        try:
            iv_bytes = base64.b64decode(iv_b64.encode("ascii"))
        except Exception as exc:
            raise ValueError(
                "decrypt_rsa_hybrid: payload_iv_b64 is not valid base64: "
                "{0}".format(exc)
            )
        if len(iv_bytes) != 16:
            raise ValueError(
                "decrypt_rsa_hybrid: iv length is {0} (expected 16)".format(
                    len(iv_bytes)
                )
            )
        iv_hex = iv_bytes.hex()

        # 3. Try every recipient with the local private key. First success
        #    wins. ALL openssl errors are caught silently per LD21 -- the
        #    user only sees the consolidated "no recipient" error if the
        #    whole loop fails.
        aes_key_bytes = None
        for idx, recipient in enumerate(recipients):
            if not isinstance(recipient, dict):
                continue
            wrapped_b64 = recipient.get("key_enc_b64") or ""
            try:
                wrapped_bytes = base64.b64decode(wrapped_b64.encode("ascii"))
            except Exception:
                continue
            wrapped_path = os.path.join(work_dir, "wrap-{0}.bin".format(idx))
            with open(wrapped_path, "wb") as f:
                f.write(wrapped_bytes)
            unwrapped_path = os.path.join(work_dir, "unwrap-{0}.bin".format(idx))
            proc = subprocess.run(
                [ssl["binary"], "pkeyutl", "-decrypt",
                 "-inkey", private_key_path,
                 "-pkeyopt", "rsa_padding_mode:oaep",
                 "-pkeyopt", "rsa_oaep_md:sha256",
                 "-in", wrapped_path, "-out", unwrapped_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                continue
            try:
                with open(unwrapped_path, "rb") as f:
                    candidate = f.read()
            except OSError:
                continue
            if len(candidate) == 32:
                aes_key_bytes = candidate
                break

        if aes_key_bytes is None:
            raise RuntimeError(
                "No private key matches any recipient in this package"
            )

        aes_key_hex = aes_key_bytes.hex()

        # 4. AES-256-CBC decrypt payload.enc with the recovered key + IV.
        inner_path = os.path.join(work_dir, "inner.tar.gz")
        proc = subprocess.run(
            [ssl["binary"], "enc", "-d", "-aes-256-cbc",
             "-K", aes_key_hex, "-iv", iv_hex,
             "-in", payload_enc_path, "-out", inner_path],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "decrypt_rsa_hybrid: aes decrypt failed: {0}".format(
                    proc.stderr.strip()
                )
            )
        with open(inner_path, "rb") as f:
            inner_bytes = f.read()
        if inner_bytes[:2] != _MAGIC_GZIP:
            raise RuntimeError(
                "decrypt_rsa_hybrid: decrypted inner payload is not gzip "
                "(corrupt or wrong key)"
            )
        return inner_bytes
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Manifest signing and verification
# ---------------------------------------------------------------------------

def sign_manifest(manifest_path, private_key_path):
    # type: (str, str) -> str
    """Sign a ``manifest.yaml`` with RSA-SHA256 using ``pkeyutl``.

    Reads the manifest, removes any existing signature block, signs the
    remaining content, and appends a new signature block. The signer
    identity is derived best-effort from the key path; v3 callers (task .5)
    will set it explicitly via the manifest before signing.

    Parameters:
        manifest_path: path to manifest.yaml to sign
        private_key_path: path to sender's RSA private key

    Returns the updated manifest content with signature block.
    """
    manifest_path = os.path.abspath(manifest_path)
    private_key_path = os.path.abspath(private_key_path)
    ssl = _get_openssl()

    if not ssl["supports_pkeyutl"]:
        raise RuntimeError(
            "OpenSSL {0} does not support pkeyutl".format(ssl["version"])
        )

    with open(manifest_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Remove any existing signature block
    content_to_sign = _strip_signature_block(content)

    # Write content to temp file for signing
    work_dir = tempfile.mkdtemp(prefix=".walnut-sign-")
    try:
        # First create a SHA-256 digest of the content
        data_path = os.path.join(work_dir, "manifest.data")
        with open(data_path, "w", encoding="utf-8") as f:
            f.write(content_to_sign)

        digest_path = os.path.join(work_dir, "manifest.dgst")
        sig_path = os.path.join(work_dir, "manifest.sig")

        # Hash the data
        proc = subprocess.run(
            [ssl["binary"], "dgst", "-sha256", "-binary",
             "-out", digest_path, data_path],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError("Digest failed: {0}".format(proc.stderr))

        # Sign the digest with RSA
        proc = subprocess.run(
            [ssl["binary"], "pkeyutl", "-sign",
             "-inkey", private_key_path,
             "-pkeyopt", "digest:sha256",
             "-in", digest_path, "-out", sig_path],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError("Signing failed: {0}".format(proc.stderr))

        # Read signature and base64 encode
        with open(sig_path, "rb") as f:
            sig_bytes = f.read()
        sig_b64 = base64.b64encode(sig_bytes).decode("ascii")

        # Derive signer name from the key path (best effort).
        # v3 callers should set this explicitly via the manifest before
        # signing; falls back to the current OS user when nothing else
        # works (use getpass.getuser() so Windows behaves the same as POSIX).
        signer = os.path.basename(os.path.dirname(
            os.path.dirname(private_key_path)
        ))
        if not signer or signer == ".":
            try:
                signer = getpass.getuser()
            except Exception:
                signer = "unknown"

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Append signature block
    signed_content = content_to_sign.rstrip("\n") + "\n"
    signed_content += "\nsignature:\n"
    signed_content += '  algorithm: "RSA-SHA256"\n'
    signed_content += '  signer: "{0}"\n'.format(signer)
    signed_content += '  value: "{0}"\n'.format(sig_b64)

    # Write signed manifest
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write(signed_content)

    return signed_content


def verify_manifest(manifest_path, public_key_path):
    # type: (str, str) -> Tuple[bool, Optional[str]]
    """Verify the RSA-SHA256 signature on a ``manifest.yaml``.

    Parameters:
        manifest_path: path to the signed manifest.yaml
        public_key_path: path to the signer's RSA public key

    Returns ``(verified, signer)``. ``verified`` is True iff the signature
    matches the canonicalized manifest body.
    """
    manifest_path = os.path.abspath(manifest_path)
    public_key_path = os.path.abspath(public_key_path)
    ssl = _get_openssl()

    if not ssl["supports_pkeyutl"]:
        raise RuntimeError(
            "OpenSSL {0} does not support pkeyutl".format(ssl["version"])
        )

    with open(manifest_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Parse manifest to get signature
    manifest = parse_manifest(content)
    sig_info = manifest.get("signature")
    if not sig_info:
        return (False, None)

    sig_b64 = sig_info.get("value", "")
    signer = sig_info.get("signer", "")

    if not sig_b64:
        return (False, signer)

    # Strip signature block to get the signed content
    content_to_verify = _strip_signature_block(content)

    # Decode signature
    try:
        sig_bytes = base64.b64decode(sig_b64)
    except Exception:
        return (False, signer)

    work_dir = tempfile.mkdtemp(prefix=".walnut-verify-")
    try:
        data_path = os.path.join(work_dir, "manifest.data")
        with open(data_path, "w", encoding="utf-8") as f:
            f.write(content_to_verify)

        digest_path = os.path.join(work_dir, "manifest.dgst")
        sig_path = os.path.join(work_dir, "manifest.sig")

        # Hash the data
        proc = subprocess.run(
            [ssl["binary"], "dgst", "-sha256", "-binary",
             "-out", digest_path, data_path],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return (False, signer)

        # Write signature to file
        with open(sig_path, "wb") as f:
            f.write(sig_bytes)

        # Verify with public key
        proc = subprocess.run(
            [ssl["binary"], "pkeyutl", "-verify",
             "-pubin", "-inkey", public_key_path,
             "-pkeyopt", "digest:sha256",
             "-in", digest_path, "-sigfile", sig_path],
            capture_output=True, text=True, timeout=10,
        )

        verified = proc.returncode == 0
        return (verified, signer)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _strip_signature_block(content):
    # type: (str) -> str
    """Remove the ``signature:`` block from manifest content.

    Returns the content without the signature section -- used both before
    signing (so re-signs are idempotent) and during verification.
    """
    lines = content.split("\n")
    result = []
    in_sig = False
    for line in lines:
        if re.match(r"^signature\s*:", line):
            in_sig = True
            continue
        if in_sig:
            if line and (line[0] == " " or line[0] == "\t"):
                continue
            in_sig = False
        result.append(line)

    # Remove trailing blank lines that were before the signature block
    while result and result[-1].strip() == "":
        result.pop()

    return "\n".join(result) + "\n"


# ---------------------------------------------------------------------------
# Glob pattern matcher (LD27)
# ---------------------------------------------------------------------------
#
# Exclusion patterns are translated to fully-anchored regular expressions and
# matched against POSIX-normalized paths relative to the package root. We
# avoid ``fnmatch`` (no ``**`` support) and ``pathlib.PurePosixPath.match``
# (suffix-oriented and surprising) so that the semantics are explicit and
# testable. The full algorithm is pinned in LD27 of the epic spec.

_GLOB_REGEX_CACHE = {}  # type: Dict[str, "re.Pattern"]


def _glob_to_regex(pattern):
    # type: (str) -> "re.Pattern"
    """Translate a glob pattern to a fully-anchored regex per LD27.

    Semantics:
        ``*``           matches within a single path segment (``[^/]*``)
        ``?``           single character, not ``/`` (``[^/]``)
        ``[abc]``       character class, copied verbatim
        ``**``          matches zero or more path segments including ``/``
        ``/**/`` form   collapses to ``(/.*)?/`` for recursive sub-trees

    Patterns WITHOUT ``/`` match the BASENAME at any depth (e.g. ``*.tmp``
    matches ``a.tmp``, ``foo/a.tmp``, and ``a/b/c.tmp``). Patterns WITH ``/``
    are anchored to the FULL path from package root.

    Compiled patterns are cached so repeated calls within a single create
    invocation do not re-compile the same regex over and over.
    """
    cached = _GLOB_REGEX_CACHE.get(pattern)
    if cached is not None:
        return cached

    has_slash = "/" in pattern
    out = []  # type: List[str]
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                # Swallow an optional following ``/`` so ``**/foo`` matches
                # ``foo`` at the root in addition to ``a/foo``.
                if i < n and pattern[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            # Copy character class verbatim until the matching ``]``. Bare
            # ``[`` with no closing bracket falls back to a literal ``[``.
            j = pattern.find("]", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(pattern[i:j + 1])
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1

    body = "".join(out)
    if has_slash:
        full = "^{0}$".format(body)
    else:
        # Basename-only patterns: prefix with optional dir component so the
        # pattern matches at any depth.
        full = "^(.*/)?{0}$".format(body)
    compiled = re.compile(full)
    _GLOB_REGEX_CACHE[pattern] = compiled
    return compiled


def matches_exclusion(path, patterns):
    # type: (str, List[str]) -> bool
    """Return True if a POSIX-normalized path matches any exclusion pattern.

    Backslashes are converted to forward slashes (defensive against Windows
    callers that forgot to normalize) and leading/trailing slashes are
    stripped before matching.
    """
    if not patterns:
        return False
    p_norm = path.replace("\\", "/").strip("/")
    for pat in patterns:
        if _glob_to_regex(pat).match(p_norm):
            return True
    return False


# ---------------------------------------------------------------------------
# World root + preferences loader (LD17, LD28)
# ---------------------------------------------------------------------------

# Files that the LD26 protected-path rule shields from exclusion entirely.
# Indexed by scope. ``manifest.yaml`` is implicit (it does not exist on the
# source walnut and is generated post-staging) so it is not listed here.
_PROTECTED_PATHS_BY_SCOPE = {
    "full": {
        "_kernel/key.md",
        "_kernel/log.md",
        "_kernel/insights.md",
        "_kernel/tasks.json",
        "_kernel/completed.json",
    },
    "bundle": {
        "_kernel/key.md",
    },
    "snapshot": {
        "_kernel/key.md",
        "_kernel/insights.md",
    },
}


def find_world_root(walnut_path):
    # type: (str) -> Optional[str]
    """Locate the ALIVE world root by walking UP from a walnut path.

    The world root is the first ancestor directory containing a ``.alive``
    subdirectory. Returns the absolute path or ``None`` if no marker is
    found before reaching the filesystem root.

    Algorithm matches LD28 exactly so callers in receive (task .8) can share
    the same lookup logic.
    """
    p = os.path.abspath(walnut_path)
    while True:
        if os.path.isdir(os.path.join(p, ".alive")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _read_simple_yaml_preferences(path):
    # type: (str) -> Dict[str, Any]
    """Parse a tiny subset of YAML used by ``.alive/preferences.yaml``.

    Stdlib only -- no PyYAML. Handles the following constructs only:
        - Top-level keys with scalar values
        - Top-level keys with nested dict values (block style)
        - Lists of strings under a key (``- foo``)
        - Comments (``#`` to end of line)
        - Boolean / null literals (true/false/null)

    The format is intentionally narrow: preferences files are hand-edited
    by humans, but only the ``p2p:`` section feeds the share pipeline so
    we keep the parser small enough to maintain by hand. If something is
    too exotic for this parser the resulting dict will simply be missing
    that field, and the LD17 safe defaults take over.
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except (IOError, OSError, UnicodeDecodeError):
        return {}

    # Strip line comments and trailing whitespace; preserve leading indent.
    lines = []  # type: List[Tuple[int, str]]
    for raw in raw_lines:
        # Drop a ``#`` comment that is NOT inside quotes. The preferences
        # YAML rarely uses inline comments after string scalars, so a naive
        # split is sufficient.
        idx = raw.find("#")
        if idx >= 0:
            raw = raw[:idx]
        stripped = raw.rstrip()
        if not stripped.strip():
            continue
        # Compute leading indent (in spaces; tabs counted as 4).
        indent = 0
        for ch in stripped:
            if ch == " ":
                indent += 1
            elif ch == "\t":
                indent += 4
            else:
                break
        lines.append((indent, stripped.strip()))

    def coerce_scalar(value):
        # type: (str) -> Any
        v = value.strip()
        if not v:
            return ""
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            return v[1:-1]
        low = v.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("null", "~"):
            return None
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            pass
        return v

    result = {}  # type: Dict[str, Any]

    # Recursive parser. Each invocation consumes lines belonging to a single
    # block (matched by indentation) and returns a (dict, next_index) pair.
    def parse_block(start, base_indent):
        # type: (int, int) -> Tuple[Dict[str, Any], int]
        block = {}  # type: Dict[str, Any]
        i = start
        while i < len(lines):
            indent, text = lines[i]
            if indent < base_indent:
                break
            if indent > base_indent:
                # Skip stray over-indented lines (parser is permissive).
                i += 1
                continue
            if text.startswith("- "):
                # A list item at base_indent terminates the dict block.
                break
            if ":" not in text:
                i += 1
                continue
            key, _, rest = text.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest:
                block[key] = coerce_scalar(rest)
                i += 1
                continue
            # Multi-line value: either a nested dict or a list. Inspect the
            # next non-empty line's indent to decide.
            j = i + 1
            if j >= len(lines):
                block[key] = None
                i = j
                continue
            child_indent, child_text = lines[j]
            if child_indent <= base_indent:
                block[key] = None
                i = j
                continue
            if child_text.startswith("- "):
                items = []  # type: List[Any]
                while j < len(lines):
                    ci, ct = lines[j]
                    if ci != child_indent or not ct.startswith("- "):
                        break
                    items.append(coerce_scalar(ct[2:]))
                    j += 1
                block[key] = items
                i = j
            else:
                nested, j = parse_block(j, child_indent)
                block[key] = nested
                i = j
        return block, i

    parsed, _ = parse_block(0, 0)
    if isinstance(parsed, dict):
        result = parsed
    return result


def _load_p2p_preferences(walnut_path):
    # type: (str) -> Dict[str, Any]
    """Load and normalize the ``p2p:`` block from ``.alive/preferences.yaml``.

    Walks UP from ``walnut_path`` to find the ALIVE world root via
    ``find_world_root``, then parses ``{world_root}/.alive/preferences.yaml``
    if present. Returns a dict with the LD17 schema and safe defaults for
    every field. Missing files / sections / keys fall back to defaults
    silently -- the share CLI surfaces a warning to the human when it
    detects that no preferences were found.

    Schema (with defaults):
        share_presets: {}                  # name -> {exclude_patterns: [...]}
        relay: {url: None, token_env: "GH_TOKEN"}
        auto_receive: False
        signing_key_path: ""
        require_signature: False
        discovery_hints: True              # top-level key, included for the
                                            # share skill convenience
    """
    defaults = {
        "share_presets": {},
        "relay": {"url": None, "token_env": "GH_TOKEN"},
        "auto_receive": False,
        "signing_key_path": "",
        "require_signature": False,
        "discovery_hints": True,
        "_world_root": None,
        "_preferences_found": False,
    }  # type: Dict[str, Any]

    world_root = find_world_root(walnut_path)
    if world_root is None:
        return defaults
    defaults["_world_root"] = world_root

    prefs_path = os.path.join(world_root, ".alive", "preferences.yaml")
    parsed = _read_simple_yaml_preferences(prefs_path)
    if not parsed:
        return defaults

    defaults["_preferences_found"] = True

    # Top-level discovery_hints lives outside the p2p: block per LD17.
    if "discovery_hints" in parsed:
        defaults["discovery_hints"] = bool(parsed["discovery_hints"])

    p2p = parsed.get("p2p")
    if not isinstance(p2p, dict):
        return defaults

    presets = p2p.get("share_presets")
    if isinstance(presets, dict):
        normalized_presets = {}  # type: Dict[str, Dict[str, Any]]
        for preset_name, preset_def in presets.items():
            if isinstance(preset_def, dict):
                excludes = preset_def.get("exclude_patterns", [])
                if not isinstance(excludes, list):
                    excludes = []
                normalized_presets[preset_name] = {
                    "exclude_patterns": [str(x) for x in excludes if x],
                }
        defaults["share_presets"] = normalized_presets

    relay = p2p.get("relay")
    if isinstance(relay, dict):
        defaults["relay"] = {
            "url": relay.get("url"),
            "token_env": relay.get("token_env") or "GH_TOKEN",
        }

    if "auto_receive" in p2p:
        defaults["auto_receive"] = bool(p2p["auto_receive"])
    if "signing_key_path" in p2p and p2p["signing_key_path"]:
        defaults["signing_key_path"] = str(p2p["signing_key_path"])
    if "require_signature" in p2p:
        defaults["require_signature"] = bool(p2p["require_signature"])

    return defaults


def _load_peer_exclusions(peer_name):
    # type: (str) -> List[str]
    """Read ``$HOME/.alive/relay/relay.json`` and return a peer's exclusion globs.

    Returns an empty list if relay.json is missing, malformed, or the peer
    has no ``exclude_patterns`` configured. Hard errors only when the named
    peer is missing entirely from the relay config -- the CLI surfaces an
    actionable error in that case.
    """
    relay_json = os.path.expanduser(
        os.path.join("~", ".alive", "relay", "relay.json")
    )
    if not os.path.isfile(relay_json):
        raise FileNotFoundError(
            "Relay not configured. Run /alive:relay setup before using "
            "--exclude-from."
        )
    try:
        with open(relay_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (IOError, OSError, ValueError) as exc:
        raise ValueError(
            "Cannot parse relay.json at {0}: {1}".format(relay_json, exc)
        )
    peers = data.get("peers") or {}
    if peer_name not in peers:
        raise KeyError(
            "Peer '{0}' not found in {1}. Known peers: {2}".format(
                peer_name, relay_json, sorted(peers.keys()) or "(none)"
            )
        )
    peer_def = peers[peer_name] or {}
    excludes = peer_def.get("exclude_patterns") or []
    if not isinstance(excludes, list):
        return []
    return [str(p) for p in excludes if p]


# ---------------------------------------------------------------------------
# Default output path resolver (LD11)
# ---------------------------------------------------------------------------

def resolve_default_output(walnut_name, scope):
    # type: (str, str) -> str
    """Compute the default ``--output`` path per LD11.

    Prefers ``~/Desktop`` if it exists (macOS default), otherwise the
    current working directory. The filename pattern is
    ``{walnut_name}-{scope}-{YYYY-MM-DD}.walnut``.
    """
    date = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = "{0}-{1}-{2}.walnut".format(walnut_name, scope, date)
    desktop = os.path.expanduser(os.path.join("~", "Desktop"))
    if os.path.isdir(desktop):
        return os.path.join(desktop, filename)
    return os.path.join(os.getcwd(), filename)


# ---------------------------------------------------------------------------
# create_package -- top-level orchestrator (LD11, LD26)
# ---------------------------------------------------------------------------

def _apply_exclusions_to_staging(staging_dir, exclusions, protected_paths):
    # type: (str, List[str], "set") -> List[str]
    """Walk a staged tree and delete files matching any exclusion pattern.

    Protected paths bypass exclusions entirely (LD26 rule). Empty
    directories are NOT pruned -- the tar packer ignores them anyway and
    leaving them in place keeps the manifest's ``files[]`` count stable
    against repeated runs.

    Returns the sorted list of relpaths that were actually removed (used
    for the manifest's ``exclusions_applied`` audit trail). The patterns
    themselves are stored as the audit list, but this list is useful for
    warnings about empty matches.
    """
    if not exclusions:
        return []
    removed = []  # type: List[str]
    for root, dirs, files in os.walk(staging_dir):
        for name in list(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, staging_dir).replace(os.sep, "/")
            # ``manifest.yaml`` is generated AFTER this pass runs, so it does
            # not exist on disk yet. Defensive guard for callers that may
            # invoke this helper after generation.
            if rel == "manifest.yaml":
                continue
            if rel in protected_paths:
                continue
            if matches_exclusion(rel, exclusions):
                try:
                    os.unlink(full)
                except OSError:
                    continue
                removed.append(rel)
    removed.sort()
    return removed


def create_package(
    walnut_path,
    scope,
    output_path=None,
    bundle_names=None,
    description="",
    note="",
    session_id=None,
    engine="unknown",
    plugin_version="3.1.0",
    sender=None,
    exclusions=None,
    preset=None,
    exclude_from_peer=None,
    include_full_history=False,
    encrypt_mode="none",
    passphrase_env=None,
    recipient_peers=None,
    sign=False,
    source_layout="v3",
    yes=False,
):
    # type: (str, str, Optional[str], Optional[List[str]], str, str, Optional[str], str, str, Optional[str], Optional[List[str]], Optional[str], Optional[str], bool, str, Optional[str], Optional[List[str]], bool, str, bool) -> Dict[str, Any]
    """Top-level orchestrator: stage -> manifest -> tar -> (encrypt) -> (sign).

    Ties the LD11 share CLI contract to the underlying staging (.4),
    manifest generation (.5), tar foundations (.3), encryption (.3), and
    signing (.3) primitives. Returns a dict the CLI uses for human output:

        {
            "package_path": "/abs/path/to/file.walnut",
            "size_bytes": 12345,
            "import_id": "<sha256 hex>",
            "manifest": {<the parsed manifest dict>},
            "warnings": [<list of strings>],
            "exclusions_applied": [<sorted patterns>],
            "preferences_found": True/False,
        }

    The temporary staging dir is always cleaned up. On any error before
    the final tar is created, no output file is written.

    See the docstrings of ``_stage_files``, ``generate_manifest``, and
    ``safe_tar_create`` for the lower-level contracts. Behaviour rules for
    the parameters live in the LD11 contract above.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            "Unknown scope '{0}'; expected one of {1}".format(scope, _VALID_SCOPES)
        )
    if scope == "bundle" and not bundle_names:
        raise ValueError("--scope bundle requires at least one --bundle NAME")
    if scope in ("full", "snapshot") and bundle_names:
        raise ValueError("--bundle is only valid with --scope bundle")
    if encrypt_mode not in ("none", "passphrase", "rsa"):
        raise ValueError(
            "Unknown encryption mode '{0}'; expected none|passphrase|rsa".format(
                encrypt_mode
            )
        )
    if encrypt_mode == "passphrase" and not passphrase_env:
        raise ValueError("--encrypt passphrase requires --passphrase-env ENV_VAR")
    if encrypt_mode == "rsa" and not recipient_peers:
        raise ValueError(
            "--encrypt rsa requires at least one --recipient peer-name"
        )

    walnut_path = os.path.abspath(walnut_path)
    if not os.path.isdir(walnut_path):
        raise FileNotFoundError("walnut path not found: {0}".format(walnut_path))

    # Resolve identity fields up front so manifest + warnings are consistent.
    if sender is None:
        sender = resolve_sender()
    if session_id is None:
        session_id = resolve_session_id()

    walnut_name = _walnut_name(walnut_path)

    # Resolve output path before any work happens so the user gets a
    # predictable error if the parent dir is missing.
    if output_path is None:
        output_path = resolve_default_output(walnut_name, scope)
    output_path = os.path.abspath(output_path)
    out_parent = os.path.dirname(output_path) or os.getcwd()
    if not os.path.isdir(out_parent):
        raise FileNotFoundError(
            "Output parent directory does not exist: {0}".format(out_parent)
        )

    # Load preferences (LD17). Errors here are warnings, not failures.
    prefs = _load_p2p_preferences(walnut_path)
    warnings = []  # type: List[str]
    if not prefs.get("_preferences_found"):
        warnings.append(
            "No p2p preferences found; using baseline stubs only."
        )

    # Validate signing prerequisite per LD11 flag rules.
    if sign:
        signing_key = prefs.get("signing_key_path") or ""
        if not signing_key:
            raise ValueError(
                "--sign requires p2p.signing_key_path in .alive/preferences.yaml. "
                "Configure it before signing packages."
            )
        signing_key_path = os.path.expanduser(signing_key)
        if not os.path.isfile(signing_key_path):
            raise FileNotFoundError(
                "Configured signing key not found: {0}".format(signing_key_path)
            )
    else:
        signing_key_path = None

    # Build the effective exclusion list: preset + --exclude + --exclude-from
    # peer. The order is irrelevant -- exclusions are evaluated as a set --
    # but we keep insertion order for the audit trail to make tests stable.
    effective_exclusions = []  # type: List[str]
    seen = set()  # type: set
    if preset:
        presets = prefs.get("share_presets") or {}
        if preset not in presets:
            known = sorted(presets.keys())
            raise KeyError(
                "Unknown preset '{0}'. Known presets: {1}".format(
                    preset, known or "(none configured)"
                )
            )
        for pat in presets[preset].get("exclude_patterns", []) or []:
            if pat and pat not in seen:
                effective_exclusions.append(pat)
                seen.add(pat)
    if exclusions:
        for pat in exclusions:
            if pat and pat not in seen:
                effective_exclusions.append(pat)
                seen.add(pat)
    if exclude_from_peer:
        peer_excludes = _load_peer_exclusions(exclude_from_peer)
        for pat in peer_excludes:
            if pat and pat not in seen:
                effective_exclusions.append(pat)
                seen.add(pat)

    protected = _PROTECTED_PATHS_BY_SCOPE.get(scope, set())

    # ---- Stage the package -------------------------------------------------
    staging = _stage_files(
        walnut_path,
        scope,
        bundle_names=bundle_names,
        sender=sender,
        session_id=session_id,
        stub_kernel_history=not include_full_history,
        warnings=warnings,
        source_layout=source_layout,
    )

    try:
        # Apply exclusions AFTER staging so the staging helpers stay layout-
        # aware. Protected paths (LD26) bypass exclusions entirely; the
        # helper enforces that for us.
        removed_paths = _apply_exclusions_to_staging(
            staging, effective_exclusions, protected
        )
        if effective_exclusions and not removed_paths:
            warnings.append(
                "Exclusion patterns matched zero files: {0}".format(
                    ", ".join(effective_exclusions)
                )
            )

        # Build substitutions_applied for LD9 baseline stubs unless the
        # caller asked for the real history.
        substitutions = []  # type: List[Dict[str, Any]]
        if scope == "full" and not include_full_history:
            substitutions.append({
                "path": "_kernel/log.md",
                "reason": "baseline-stub",
            })
            substitutions.append({
                "path": "_kernel/insights.md",
                "reason": "baseline-stub",
            })
        elif scope == "snapshot":
            substitutions.append({
                "path": "_kernel/insights.md",
                "reason": "baseline-stub",
            })

        # Generate the manifest. The function writes manifest.yaml into the
        # staging dir as its final step, so the file ends up included in
        # the tar archive.
        manifest = generate_manifest(
            staging,
            scope,
            walnut_name,
            bundles=bundle_names if scope == "bundle" else None,
            description=description,
            note=note,
            session_id=session_id,
            engine=engine,
            plugin_version=plugin_version,
            sender=sender,
            exclusions_applied=list(effective_exclusions),
            substitutions_applied=substitutions,
            source_layout=source_layout,
        )
        import_id = hashlib.sha256(
            canonical_manifest_bytes(manifest)
        ).hexdigest()

        # ---- Pack the staging tree into the .walnut tarball ----------------
        safe_tar_create(staging, output_path)

        # ---- Optional encryption -------------------------------------------
        if encrypt_mode == "passphrase":
            # LD21 passphrase envelope: the .walnut file IS the raw output
            # of ``openssl enc -aes-256-cbc -pbkdf2 -salt`` over the gzipped
            # inner tar. The receive side detects ``Salted__`` magic and
            # walks the LD5 fallback chain on decryption.
            if not passphrase_env:
                raise ValueError(
                    "--encrypt passphrase requires --passphrase-env ENV_VAR"
                )
            passphrase_value = os.environ.get(passphrase_env, "")
            if not passphrase_value:
                raise ValueError(
                    "Environment variable '{0}' is not set; cannot encrypt.".format(
                        passphrase_env
                    )
                )
            ssl = _get_openssl()
            if not ssl["supports_pbkdf2"]:
                raise RuntimeError(
                    "OpenSSL {0} does not support -pbkdf2; passphrase "
                    "encryption requires LibreSSL >= 3.1 or OpenSSL >= "
                    "1.1.1".format(ssl["version"])
                )
            enc_tmp = output_path + ".enc.tmp"
            proc = subprocess.run(
                [ssl["binary"], "enc", "-aes-256-cbc", "-md", "sha256",
                 "-pbkdf2", "-iter", "600000", "-salt",
                 "-in", output_path, "-out", enc_tmp,
                 "-pass", "env:{0}".format(passphrase_env)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, passphrase_env: passphrase_value},
            )
            if proc.returncode != 0:
                if os.path.exists(enc_tmp):
                    try:
                        os.unlink(enc_tmp)
                    except OSError:
                        pass
                raise RuntimeError(
                    "Passphrase encryption failed: {0}".format(
                        proc.stderr.strip()
                    )
                )
            os.replace(enc_tmp, output_path)
        elif encrypt_mode == "rsa":
            # LD21 RSA hybrid envelope. Resolve each peer handle to a PEM
            # path via the LD23 keyring helper, build the inner payload
            # bytes from the already-packed gzipped tar at ``output_path``,
            # encrypt to a new outer tar, and replace ``output_path``.
            keys_dir = os.environ.get("ALIVE_RELAY_KEYS_DIR") or None
            recipient_pem_paths = []  # type: List[str]
            for peer in (recipient_peers or []):
                pem_path = resolve_peer_pubkey_path(peer, keys_dir=keys_dir)
                if not pem_path:
                    raise FileNotFoundError(
                        "Peer key not found: {0}. Add via "
                        "'alive:relay add <repo>' or place PEM at "
                        "$HOME/.alive/relay/keys/peers/{0}.pem".format(peer)
                    )
                recipient_pem_paths.append(pem_path)
            with open(output_path, "rb") as f:
                inner_bytes = f.read()
            outer_bytes = encrypt_rsa_hybrid(
                payload_tar_gz_bytes=inner_bytes,
                recipient_pubkey_pems=recipient_pem_paths,
            )
            with open(output_path, "wb") as f:
                f.write(outer_bytes)

        # ---- Optional signing ----------------------------------------------
        if sign and signing_key_path:
            # ``sign_manifest`` operates on a manifest file path, but the
            # manifest now lives inside the packed tar. The legacy v2
            # helpers do not yet sign LD20 canonical bytes; this is the
            # known cross-task gap documented in generate_manifest's
            # docstring. Surface a warning rather than silently no-op so
            # callers know the package is unsigned despite ``--sign``.
            warnings.append(
                "--sign accepted but RSA-PSS signing of v3 manifests lands "
                "in task .11; package was created without a signature."
            )

        size_bytes = os.path.getsize(output_path)
        return {
            "package_path": output_path,
            "size_bytes": size_bytes,
            "import_id": import_id,
            "manifest": manifest,
            "warnings": warnings,
            "exclusions_applied": list(effective_exclusions),
            "removed_paths": removed_paths,
            "preferences_found": prefs.get("_preferences_found", False),
            "world_root": prefs.get("_world_root"),
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# LD1 receive pipeline (task .8)
# ---------------------------------------------------------------------------
#
# ``receive_package`` orchestrates the 13-step LD1 pipeline:
#
#   1. extract            -- detect envelope, decrypt, safe_extractall to staging
#   2. validate           -- schema, per-file checksums, payload sha256, signature
#   3. dedupe-check       -- LD2 subset-of-union against target/_kernel/imports.json
#   4. infer-layout       -- LD7 precedence: --source-layout > manifest > inference
#   5. scope-check        -- LD18 target preconditions per scope
#   6. migrate            -- LD8 v2 -> v3 staging reshape if needed
#   7. preview            -- print summary; await --yes for non-interactive use
#   8. acquire-lock       -- LD4/LD28 fcntl or mkdir fallback
#   9. transact-swap      -- LD18 atomic move (full/snapshot) or journaled move (bundle)
#  10. log-edit           -- LD12 insert import entry after frontmatter (atomic)
#  11. ledger-write       -- LD2 append entry to _kernel/imports.json
#  12. regenerate-now     -- LD1 explicit subprocess to project.py (NOT hook chain)
#  13. cleanup-and-release -- always runs; release lock, delete or preserve staging
#
# RSA hybrid decryption deferred to task .11 -- raises NotImplementedError.

# Detection magic bytes per LD21.
_MAGIC_GZIP = b"\x1f\x8b"
_MAGIC_OPENSSL_SALTED = b"Salted__"

# Default scope-aware exclusion list applied DURING staging extract -- defense
# in depth: even if a malicious sender ships .alive/ or .walnut/ inside a
# package, we strip them before any swap. The pre-validation in safe_tar_extract
# already prevents path traversal; this strips legitimately-named-but-dangerous
# system dirs.
_RECEIVE_STRIP_DIRS = (".alive", ".walnut", "__MACOSX")


def _detect_envelope(package_path):
    # type: (str) -> str
    """Sniff the first bytes of a .walnut package and return its envelope kind.

    Returns one of:
        "gzip"        -- unencrypted gzipped tarball (LD21 path 1)
        "passphrase"  -- OpenSSL ``Salted__`` envelope (LD21 path 2)
        "rsa"         -- RSA hybrid envelope (LD21 path 3, deferred to .11)

    Detection algorithm:
        1. First two bytes ``1F 8B`` -> gzip
        2. First eight bytes ``"Salted__"`` -> passphrase
        3. Otherwise: try opening as a tar archive and look for the
           ``payload.key`` member (legacy v2 RSA hybrid produced by
           ``encrypt_package`` with ``mode="rsa"``) or the
           ``rsa-envelope-v1.json`` member (LD21 spec, lands in .11)
        4. If neither match, raise ``ValueError`` with an actionable message.
    """
    package_path = os.path.abspath(package_path)
    if not os.path.isfile(package_path):
        raise FileNotFoundError("Package not found: {0}".format(package_path))

    with open(package_path, "rb") as f:
        head = f.read(8)

    if head[:2] == _MAGIC_GZIP:
        return "gzip"
    if head == _MAGIC_OPENSSL_SALTED:
        return "passphrase"

    # Try treating it as an unencrypted tar (might be the legacy v2 RSA outer
    # tar produced by ``encrypt_package``, which is an uncompressed tar with
    # ``payload.key`` + ``payload.enc`` + ``manifest.yaml``).
    try:
        with tarfile.open(package_path, "r:*") as tar:
            names = set(tar.getnames())
    except (tarfile.TarError, OSError):
        raise ValueError(
            "Unknown package format: {0}. Expected gzip, passphrase, or "
            "RSA hybrid envelope.".format(package_path)
        )

    # LD21 RSA hybrid (canonical, lands in .11): rsa-envelope-v1.json + payload.enc
    if "rsa-envelope-v1.json" in names and "payload.enc" in names:
        return "rsa"
    # Legacy v2 RSA hybrid produced by encrypt_package: payload.key + payload.enc
    if "payload.key" in names and "payload.enc" in names:
        return "rsa"

    raise ValueError(
        "Unknown package format: {0}. Expected gzip, passphrase, or RSA "
        "hybrid envelope.".format(package_path)
    )


def _decrypt_to_staging(package_path, envelope, passphrase_env, private_key_path,
                        staging_parent):
    # type: (str, str, Optional[str], Optional[str], str) -> str
    """Decrypt a package envelope (if needed) and return the path to a
    plaintext gzipped tar that can be safely extracted via ``safe_extractall``.

    Returns either the original ``package_path`` (gzip) or a path inside a
    sibling temp dir under ``staging_parent`` containing the decrypted inner
    payload tarball.

    Caller is responsible for cleaning up any temp files this returns.

    Raises:
        NotImplementedError -- RSA hybrid (deferred to task .11)
        ValueError          -- malformed envelope
        RuntimeError        -- decryption failure (wrong passphrase, etc.)
    """
    if envelope == "gzip":
        return package_path

    if envelope == "passphrase":
        if not passphrase_env:
            raise ValueError(
                "Package is passphrase-encrypted. Re-run with "
                "--passphrase-env <ENV_VAR> pointing at an env var that "
                "holds the passphrase."
            )
        passphrase = os.environ.get(passphrase_env, "")
        if not passphrase:
            raise ValueError(
                "Environment variable {0!r} is empty or unset; cannot "
                "decrypt package.".format(passphrase_env)
            )

        ssl = _get_openssl()
        # Decrypt to a sibling temp file. We don't reuse decrypt_package because
        # that helper assumes the v2 outer-tar layout (manifest.yaml +
        # payload.enc + optional payload.key). LD21 passphrase mode is the raw
        # OpenSSL output: we feed the .walnut file directly into ``openssl enc -d``.
        decrypted_dir = tempfile.mkdtemp(
            prefix=".alive-receive-dec-", dir=staging_parent,
        )
        decrypted_path = os.path.join(decrypted_dir, "payload.tar.gz")

        fallbacks = [
            ("v2.1.0 default (pbkdf2, iter=600000)",
             ["-md", "sha256", "-pbkdf2", "-iter", "600000"]),
            ("epic-LD5 baseline (pbkdf2, iter=100000)",
             ["-md", "sha256", "-pbkdf2", "-iter", "100000"]),
            ("v2 defaults (pbkdf2, no iter)",
             ["-md", "sha256", "-pbkdf2"]),
            ("v1 legacy (md5)",
             ["-md", "md5"]),
        ]
        last_err = ""
        for desc, extra in fallbacks:
            proc = subprocess.run(
                [ssl["binary"], "enc", "-d", "-aes-256-cbc",
                 *extra,
                 "-in", package_path, "-out", decrypted_path,
                 "-pass", "env:{0}".format(passphrase_env)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, passphrase_env: passphrase},
            )
            if proc.returncode == 0:
                # Sanity check: must look like a gzip file now.
                with open(decrypted_path, "rb") as f:
                    if f.read(2) == _MAGIC_GZIP:
                        return decrypted_path
                last_err = "{0}: openssl exit 0 but output is not gzip".format(desc)
                continue
            last_err = "{0}: {1}".format(desc, proc.stderr.strip())

        # All fallbacks failed -- clean up and raise.
        shutil.rmtree(decrypted_dir, ignore_errors=True)
        raise RuntimeError(
            "Cannot decrypt package -- wrong passphrase or unsupported "
            "format. Try `openssl enc -d` manually to debug. Last error: {0}".format(
                last_err
            )
        )

    if envelope == "rsa":
        if not private_key_path:
            raise ValueError(
                "Package is RSA-encrypted. Re-run with --private-key <PATH> "
                "pointing at the local RSA private key."
            )
        with open(package_path, "rb") as f:
            outer_bytes = f.read()
        try:
            inner_bytes = decrypt_rsa_hybrid(outer_bytes, private_key_path)
        except (ValueError, RuntimeError, FileNotFoundError):
            raise
        decrypted_dir = tempfile.mkdtemp(
            prefix=".alive-receive-rsa-", dir=staging_parent,
        )
        decrypted_path = os.path.join(decrypted_dir, "inner-payload.tar.gz")
        with open(decrypted_path, "wb") as f:
            f.write(inner_bytes)
        return decrypted_path

    raise ValueError("Unknown envelope kind: {0!r}".format(envelope))


def _strip_unwanted_dirs_from_staging(staging_dir):
    # type: (str) -> List[str]
    """Defense-in-depth: remove any ``.alive``/``.walnut`` dirs that may have
    snuck into a package. The pre-validation in ``safe_tar_extract`` already
    rejects path traversal, so this only strips legitimately-named directories
    that would be dangerous on the receiver side.

    Returns a list of removed relative paths (for diagnostics).
    """
    removed = []  # type: List[str]
    for entry in os.listdir(staging_dir):
        if entry in _RECEIVE_STRIP_DIRS:
            full = os.path.join(staging_dir, entry)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
                removed.append(entry)
            elif os.path.isfile(full):
                try:
                    os.unlink(full)
                except OSError:
                    pass
                removed.append(entry)
    return removed


def _infer_source_layout(staging_dir, manifest_layout, cli_override):
    # type: (str, Optional[str], Optional[str]) -> str
    """LD7 layout inference. Returns ``"v2"`` or ``"v3"`` (or ``"agnostic"``
    for snapshot-shaped staging trees).

    Precedence:
        1. ``cli_override`` if set AND ALIVE_P2P_TESTING=1
        2. ``manifest_layout`` if in {v2, v3}
        3. Structural inspection of immediate children only
        4. Fail with ``ValueError``

    Structural rules (immediate children only, never recursive):
        a) staging/bundles/ exists AND any staging/bundles/*/context.manifest.yaml
           -> v2
        b) staging/_kernel/_generated/ exists -> v2
        c) any immediate child <name>/ (not _kernel, not bundles) with
           context.manifest.yaml at its root -> v3
        d) staging contains ONLY _kernel/ as immediate child -> agnostic
        e) otherwise: fail
    """
    if cli_override and os.environ.get("ALIVE_P2P_TESTING") == "1":
        if cli_override in ("v2", "v3"):
            return cli_override
    if manifest_layout in ("v2", "v3"):
        return manifest_layout

    children = sorted(os.listdir(staging_dir))

    # Rule (a): v2 bundles container
    bundles_dir = os.path.join(staging_dir, "bundles")
    if os.path.isdir(bundles_dir):
        for sub in os.listdir(bundles_dir):
            sub_path = os.path.join(bundles_dir, sub)
            if os.path.isdir(sub_path) and os.path.isfile(
                os.path.join(sub_path, "context.manifest.yaml")
            ):
                return "v2"

    # Rule (b): v2 _generated marker
    generated_dir = os.path.join(staging_dir, "_kernel", "_generated")
    if os.path.isdir(generated_dir):
        return "v2"

    # Rule (c): v3 flat top-level bundle
    for entry in children:
        if entry in ("_kernel", "bundles", "manifest.yaml"):
            continue
        entry_path = os.path.join(staging_dir, entry)
        if os.path.isdir(entry_path) and os.path.isfile(
            os.path.join(entry_path, "context.manifest.yaml")
        ):
            return "v3"

    # Rule (d): snapshot agnostic (only _kernel + manifest.yaml)
    non_manifest = [c for c in children if c != "manifest.yaml"]
    if non_manifest == ["_kernel"]:
        return "agnostic"

    raise ValueError(
        "Cannot infer source layout. Add a source_layout field to the "
        "package manifest or verify the package is not corrupt. "
        "Staging children: {0}".format(children)
    )


def _staging_top_level_bundles(staging_dir):
    # type: (str) -> List[str]
    """Return sorted list of top-level bundle leaf names in a v3-shaped staging
    directory. A bundle is a child dir containing ``context.manifest.yaml`` at
    its root and not equal to ``_kernel``/``bundles``.
    """
    bundles = []  # type: List[str]
    if not os.path.isdir(staging_dir):
        return bundles
    for entry in os.listdir(staging_dir):
        if entry in ("_kernel", "manifest.yaml"):
            continue
        entry_path = os.path.join(staging_dir, entry)
        if os.path.isdir(entry_path) and os.path.isfile(
            os.path.join(entry_path, "context.manifest.yaml")
        ):
            bundles.append(entry)
    return sorted(bundles)


def _read_imports_ledger(target_path):
    # type: (str) -> Dict[str, Any]
    """Load ``{target}/_kernel/imports.json`` if it exists; return canonical
    empty ledger if not. Tolerates missing target dir gracefully.
    """
    ledger_path = os.path.join(target_path, "_kernel", "imports.json")
    if not os.path.isfile(ledger_path):
        return {"imports": []}
    try:
        with open(ledger_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "imports" not in data:
            return {"imports": []}
        if not isinstance(data["imports"], list):
            return {"imports": []}
        return data
    except (IOError, OSError, ValueError, json.JSONDecodeError):
        return {"imports": []}


def _write_imports_ledger(target_path, ledger):
    # type: (str, Dict[str, Any]) -> None
    """Atomically write the imports ledger to ``{target}/_kernel/imports.json``.

    Uses ``tempfile.NamedTemporaryFile`` in the same dir + ``os.replace`` so
    crash mid-write leaves the prior file intact.
    """
    kernel_dir = os.path.join(target_path, "_kernel")
    os.makedirs(kernel_dir, exist_ok=True)
    ledger_path = os.path.join(kernel_dir, "imports.json")
    fd, tmp_path = tempfile.mkstemp(
        prefix=".imports-", suffix=".json", dir=kernel_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, ledger_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _compute_dedupe(ledger, import_id, requested_bundles):
    # type: (Dict[str, Any], str, Optional[List[str]]) -> Tuple[bool, List[str], List[str]]
    """LD2 subset-of-union dedupe.

    Args:
        ledger: parsed imports.json dict
        import_id: this package's import_id
        requested_bundles: list of bundle leaves to apply (None for full/snapshot)

    Returns ``(is_noop, prior_applied, effective_to_apply)``:
        is_noop          -- True if every requested bundle is already in the union
        prior_applied    -- sorted list of bundles already applied across all
                            ledger entries with matching import_id
        effective_to_apply -- bundles still needing to be applied (sorted)
    """
    prior = set()  # type: Set[str]
    for entry in ledger.get("imports", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("import_id") != import_id:
            continue
        applied = entry.get("applied_bundles", []) or []
        for b in applied:
            if isinstance(b, str):
                prior.add(b)

    if requested_bundles is None:
        requested = set()  # type: Set[str]
    else:
        requested = set(requested_bundles)

    if requested and requested.issubset(prior):
        return (True, sorted(prior), [])
    if not requested and prior:
        # Snapshot/full with empty requested list and SOMETHING already
        # applied: dedupe says no-op only if there is also no work to do.
        # We treat this as not-no-op so the caller's regular logic runs --
        # the caller decides whether requested set is meaningful.
        return (False, sorted(prior), [])

    effective = sorted(requested - prior)
    return (False, sorted(prior), effective)


def _atomic_write_text(path, content):
    # type: (str, str) -> None
    """Write text to ``path`` atomically via tempfile + os.replace."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-write-", dir=parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


_LOG_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_log_frontmatter(content):
    # type: (str) -> Tuple[Dict[str, Any], str, str]
    """Parse the YAML frontmatter at the head of a log.md file.

    Returns ``(fields, frontmatter_block, body)``:
        fields            -- dict of top-level scalar fields parsed from the FM
        frontmatter_block -- the literal frontmatter text including ``---`` lines
                             and trailing newline (so callers can replace it)
        body              -- everything after the closing ``---`` line

    If the file does not start with a YAML frontmatter block, all three return
    values are empty / None to signal that the caller should treat the file
    as malformed.
    """
    if not content.startswith("---"):
        return ({}, "", content)
    m = _LOG_FRONTMATTER_RE.match(content)
    if not m:
        return ({}, "", content)
    fm_body = m.group(1)
    fields = {}  # type: Dict[str, Any]
    for line in fm_body.split("\n"):
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # Strip simple quotes
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        fields[key] = val
    block = m.group(0)
    body = content[m.end():]
    return (fields, block, body)


def _render_log_frontmatter(fields, key_order):
    # type: (Dict[str, Any], List[str]) -> str
    """Render a log.md frontmatter block from a fields dict.

    Emits keys in ``key_order`` first, then any remaining keys alphabetically.
    Always wraps in ``---`` lines and ends with a single newline. Strings are
    NOT quoted (matches the v3 log.md convention from rules/standards.md).
    """
    out = ["---"]
    seen = set()  # type: Set[str]
    for k in key_order:
        if k in fields:
            out.append("{0}: {1}".format(k, fields[k]))
            seen.add(k)
    for k in sorted(fields.keys()):
        if k in seen:
            continue
        out.append("{0}: {1}".format(k, fields[k]))
    out.append("---")
    out.append("")
    return "\n".join(out)


_LOG_FM_KEY_ORDER = ["walnut", "created", "last-entry", "entry-count", "summary"]


def _build_import_log_entry(iso_timestamp, session_id, sender, scope,
                             bundles, source_layout, import_id):
    # type: (str, str, str, str, Optional[List[str]], str, str) -> str
    """Render the LD12 import log entry body (no frontmatter).

    The template ends with a trailing blank line so the next entry slots in
    cleanly above existing content.
    """
    if bundles:
        bundle_list = ", ".join(bundles)
    else:
        bundle_list = "n/a"
    return (
        "## {ts} - squirrel:{sid}\n"
        "\n"
        "Imported package from {sender} via P2P.\n"
        "- Scope: {scope}\n"
        "- Bundles: {blist}\n"
        "- source_layout: {layout}\n"
        "- import_id: {iid}\n"
        "\n"
        "signed: squirrel:{sid}\n"
        "\n"
    ).format(
        ts=iso_timestamp,
        sid=session_id,
        sender=sender,
        scope=scope,
        blist=bundle_list,
        layout=source_layout,
        iid=import_id[:16],
    )


def _edit_log_md(target_path, iso_timestamp, session_id, sender, scope,
                 bundles, source_layout, import_id, walnut_name, allow_create):
    # type: (str, str, str, str, str, Optional[List[str]], str, str, str, bool) -> None
    """LD12 log edit operation. Inserts an import entry after the YAML
    frontmatter, before any existing entries.

    Args:
        target_path: absolute path to target walnut
        allow_create: True for full/snapshot scope (creates log.md if missing).
                      False for bundle scope (raises if log.md missing).

    Raises:
        FileNotFoundError -- log.md missing and not allow_create
        ValueError        -- log.md exists but has no valid frontmatter
    """
    log_path = os.path.join(target_path, "_kernel", "log.md")
    entry_body = _build_import_log_entry(
        iso_timestamp, session_id, sender, scope, bundles, source_layout, import_id,
    )

    if not os.path.isfile(log_path):
        if not allow_create:
            raise FileNotFoundError(
                "Target walnut missing _kernel/log.md. Walnut is malformed "
                "or incomplete. Refusing to edit."
            )
        # Create canonical frontmatter + entry.
        today = iso_timestamp.split("T")[0]
        fm_fields = {
            "walnut": walnut_name,
            "created": today,
            "last-entry": iso_timestamp,
            "entry-count": "1",
            "summary": "Walnut imported via P2P.",
        }
        fm = _render_log_frontmatter(fm_fields, _LOG_FM_KEY_ORDER)
        content = fm + "\n" + entry_body
        _atomic_write_text(log_path, content)
        return

    with open(log_path, "r", encoding="utf-8") as f:
        existing = f.read()

    fields, fm_block, body = _parse_log_frontmatter(existing)
    if not fm_block:
        raise ValueError(
            "Target log.md has no YAML frontmatter. Walnut is malformed. "
            "Fix manually before retrying receive."
        )

    # Update last-entry + entry-count
    try:
        prev_count = int(fields.get("entry-count", "0"))
    except (TypeError, ValueError):
        prev_count = 0
    fields["entry-count"] = str(prev_count + 1)
    fields["last-entry"] = iso_timestamp
    if "walnut" not in fields:
        fields["walnut"] = walnut_name

    new_fm = _render_log_frontmatter(fields, _LOG_FM_KEY_ORDER)
    new_content = new_fm + "\n" + entry_body + body
    _atomic_write_text(log_path, new_content)


def _walnut_lock_path(target_path):
    # type: (str) -> str
    """Return the canonical lock path for a walnut. Hash matches LD4/LD28."""
    abs_target = os.path.abspath(target_path)
    digest = hashlib.sha256(abs_target.encode("utf-8")).hexdigest()[:16]
    return os.path.expanduser("~/.alive/locks/{0}.lock".format(digest))


def _try_acquire_lock(target_path):
    # type: (str) -> Tuple[str, Any]
    """Acquire an exclusive lock on a target walnut per LD4/LD28.

    Returns ``(strategy, handle)``:
        strategy = "fcntl" -> handle is an open fd; release via close+unlink
        strategy = "mkdir" -> handle is the lock dir path; release via rmtree

    Raises ``RuntimeError`` with an actionable error if the lock is held by
    a live process. Performs LD28 stale-PID recovery (POSIX) on a dead holder.
    """
    lock_path = _walnut_lock_path(target_path)
    locks_dir = os.path.dirname(lock_path)
    os.makedirs(locks_dir, exist_ok=True)

    try:
        import fcntl
        strategy = "fcntl"
    except ImportError:
        fcntl = None  # type: ignore
        strategy = "mkdir"

    pid = os.getpid()
    now_iso = now_utc_iso()
    holder_text = "pid={0}\nstarted={1}\naction=receive\n".format(pid, now_iso)

    if strategy == "fcntl":
        # Open or create lock file.
        for attempt in range(2):
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore
            except BlockingIOError:
                # Try stale PID recovery.
                try:
                    os.lseek(fd, 0, 0)
                    existing = os.read(fd, 1024).decode("utf-8", errors="replace")
                except OSError:
                    existing = ""
                os.close(fd)
                holder_pid = _parse_holder_pid(existing)
                if holder_pid and _is_pid_dead(holder_pid):
                    if attempt == 0:
                        # Stale lock -- remove and retry once.
                        try:
                            os.unlink(lock_path)
                        except OSError:
                            pass
                        continue
                raise RuntimeError(
                    "busy: another operation holds the walnut lock "
                    "(pid {0}). Retry later or run 'alive-p2p.py unlock "
                    "--walnut {1}' if stuck.".format(
                        holder_pid or "?", target_path
                    )
                )
            # Acquired -- write holder text.
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, holder_text.encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:
                pass
            return ("fcntl", fd)
        # Loop fell through (shouldn't happen).
        raise RuntimeError(
            "busy: lock acquisition retry exhausted for {0}".format(target_path)
        )

    # mkdir fallback
    lock_dir = lock_path + ".d"
    for attempt in range(2):
        try:
            os.makedirs(lock_dir, exist_ok=False)
        except FileExistsError:
            holder_file = os.path.join(lock_dir, "holder.txt")
            existing = ""
            if os.path.isfile(holder_file):
                try:
                    with open(holder_file, "r", encoding="utf-8") as f:
                        existing = f.read()
                except (IOError, OSError):
                    pass
            holder_pid = _parse_holder_pid(existing)
            if holder_pid and _is_pid_dead(holder_pid):
                if attempt == 0:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            raise RuntimeError(
                "busy: another operation holds the walnut lock "
                "(pid {0}). Retry later or run 'alive-p2p.py unlock "
                "--walnut {1}' if stuck.".format(
                    holder_pid or "?", target_path
                )
            )
        # Acquired -- write holder.txt
        with open(os.path.join(lock_dir, "holder.txt"), "w", encoding="utf-8") as f:
            f.write(holder_text)
        return ("mkdir", lock_dir)
    raise RuntimeError(
        "busy: lock acquisition retry exhausted for {0}".format(target_path)
    )


def _parse_holder_pid(holder_text):
    # type: (str) -> Optional[int]
    """Parse the PID line out of a lock holder text block."""
    for line in holder_text.split("\n"):
        line = line.strip()
        if line.startswith("pid="):
            try:
                return int(line[4:])
            except ValueError:
                return None
    return None


def _is_pid_dead(pid):
    # type: (int) -> bool
    """Return True if a PID definitely does not refer to a running process.

    POSIX: ``os.kill(pid, 0)`` raises ProcessLookupError on dead processes.
    Other errors (PermissionError) are treated as "alive" because we cannot
    distinguish them from a live process.
    """
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _release_lock(strategy, handle, target_path):
    # type: (str, Any, str) -> None
    """Release a lock acquired via ``_try_acquire_lock``. Idempotent."""
    if strategy == "fcntl":
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(_walnut_lock_path(target_path))
        except OSError:
            pass
    elif strategy == "mkdir":
        try:
            shutil.rmtree(handle, ignore_errors=True)
        except OSError:
            pass


def _journal_path(staging_dir):
    # type: (str) -> str
    return os.path.join(staging_dir, ".alive-receive-journal.json")


def _write_journal(staging_dir, journal):
    # type: (str, Dict[str, Any]) -> None
    """Atomically write the receive journal to staging."""
    path = _journal_path(staging_dir)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".journal-", suffix=".json", dir=staging_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(journal, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _resolve_collision_name(target_path, leaf, today_yyyymmdd):
    # type: (str, str, str) -> str
    """LD3 deterministic chaining: try ``{leaf}-imported-{today}`` first, then
    ``-2``, ``-3``, ... until a free slot is found at the target.
    """
    base = "{0}-imported-{1}".format(leaf, today_yyyymmdd)
    candidate = base
    n = 2
    while os.path.exists(os.path.join(target_path, candidate)):
        candidate = "{0}-{1}".format(base, n)
        n += 1
        if n > 1000:
            raise RuntimeError(
                "LD3 collision chaining gave up after 1000 attempts for "
                "{0!r}".format(leaf)
            )
    return candidate


def _resolve_plugin_root():
    # type: () -> str
    """Resolve the alive plugin root directory for invoking ``project.py``."""
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env and os.path.isdir(env):
        return env
    # Derive from this file: <plugin>/scripts/alive-p2p.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _regenerate_now_json(target_path):
    # type: (str) -> Tuple[bool, str]
    """LD1 step 12: invoke ``project.py --walnut <target>`` as an explicit
    subprocess. Returns ``(success, message)``.

    Non-fatal: caller treats failure as a WARN per LD1.
    """
    plugin_root = _resolve_plugin_root()
    project_py = os.path.join(plugin_root, "scripts", "project.py")
    if not os.path.isfile(project_py):
        return (False, "project.py not found at {0}".format(project_py))
    try:
        proc = subprocess.run(
            [sys.executable, project_py, "--walnut", target_path],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, "subprocess error: {0}".format(exc))
    if proc.returncode != 0:
        return (False, "project.py exit {0}: {1}".format(
            proc.returncode, proc.stderr.strip()[:200]
        ))
    return (True, "ok")


def _format_migration_block(migrate_result, source_layout):
    # type: (Optional[Dict[str, Any]], str) -> str
    """Render the v2 -> v3 migration block shown above the receive preview.

    Returns an empty string when ``migrate_result`` is ``None`` or the
    inferred ``source_layout`` is not ``"v2"``. Otherwise produces a bordered
    block enumerating the migration actions, the bundles touched, the task
    conversion count, and any warnings/errors recorded by
    ``migrate_v2_layout``. The shape matches the LD8 surfacing contract from
    the receive skill (fn-7-7cw.9):

        ╭─ v2 -> v3 migration required
        │  Package source_layout: v2
        │  Actions:
        │    - Dropped _kernel/_generated/
        │    - Flattened bundles/foo -> foo
        │  Bundles migrated: foo, bar-imported
        │  Tasks converted:  7
        │  Warnings:
        │    - bundle 'baz' had no parseable entries
        ╰-
    """
    if migrate_result is None or source_layout != "v2":
        return ""

    lines = []
    lines.append("\u256d\u2500 v2 -> v3 migration required")
    lines.append("\u2502  Package source_layout: v2")

    actions = migrate_result.get("actions") or []
    if actions:
        lines.append("\u2502  Actions:")
        for action in actions:
            lines.append("\u2502    - {0}".format(action))
    else:
        lines.append("\u2502  Actions: (none)")

    bundles_migrated = migrate_result.get("bundles_migrated") or []
    if bundles_migrated:
        lines.append("\u2502  Bundles migrated: {0}".format(
            ", ".join(bundles_migrated)
        ))

    tasks_converted = migrate_result.get("tasks_converted", 0)
    if tasks_converted:
        lines.append("\u2502  Tasks converted:  {0}".format(tasks_converted))

    warnings_block = migrate_result.get("warnings") or []
    if warnings_block:
        lines.append("\u2502  Warnings:")
        for warn in warnings_block:
            lines.append("\u2502    - {0}".format(warn))

    errors_block = migrate_result.get("errors") or []
    if errors_block:
        lines.append("\u2502  Errors:")
        for err in errors_block:
            lines.append("\u2502    - {0}".format(err))

    lines.append("\u2570\u2500")
    return "\n".join(lines)


def _format_preview(scope, bundles_in_package, effective_to_apply, prior_applied,
                    file_count, package_size, envelope, signer, sensitivity,
                    rename_map, migrate_result=None, source_layout=None):
    # type: (str, List[str], List[str], List[str], int, int, str, Optional[str], Optional[str], Optional[Dict[str, str]], Optional[Dict[str, Any]], Optional[str]) -> str
    """Render the preview block printed before swap when --yes is not set.

    When ``migrate_result`` is provided AND ``source_layout == "v2"``, the
    v2 -> v3 migration summary is rendered as a bordered block ABOVE the
    standard preview so the human sees the rewrite the receive pipeline
    just performed in staging before they confirm the swap.
    """
    lines = []
    migration_block = _format_migration_block(migrate_result, source_layout)
    if migration_block:
        lines.append(migration_block)
        lines.append("")  # spacer between migration block and preview
    lines.append("=== receive preview ===")
    lines.append("scope:        {0}".format(scope))
    lines.append("bundles:      {0}".format(
        ", ".join(bundles_in_package) if bundles_in_package else "(none)"
    ))
    if scope == "bundle":
        lines.append("to apply:     {0}".format(
            ", ".join(effective_to_apply) if effective_to_apply else "(none)"
        ))
        if prior_applied:
            lines.append("already applied: {0}".format(", ".join(prior_applied)))
    lines.append("file count:   {0}".format(file_count))
    lines.append("package size: {0} bytes".format(package_size))
    lines.append("encryption:   {0}".format(envelope))
    if signer:
        lines.append("signer:       {0}".format(signer))
    if sensitivity:
        lines.append("sensitivity:  {0}".format(sensitivity))
    if rename_map:
        lines.append("renames:")
        for src, dst in sorted(rename_map.items()):
            lines.append("  {0} -> {1}".format(src, dst))
    lines.append("=======================")
    return "\n".join(lines)


def receive_package(package_path,
                    target_path,
                    scope=None,
                    bundle_names=None,
                    rename=False,
                    passphrase_env=None,
                    private_key_path=None,
                    verify_signature=False,
                    yes=False,
                    source_layout=None,
                    strict=False,
                    stdout=None):
    # type: (str, str, Optional[str], Optional[List[str]], bool, Optional[str], Optional[str], bool, bool, Optional[str], bool, Any) -> Dict[str, Any]
    """LD1 receive pipeline orchestrator (task .8 / fn-7-7cw.8).

    Receives a .walnut package into a target walnut path with full
    transactional safety. Implements all 13 LD1 steps in order.

    Args:
        package_path: input .walnut file
        target_path: target walnut path (must NOT exist for full/snapshot;
                     must exist for bundle scope)
        scope: optional CLI override; must match manifest if set
        bundle_names: optional bundle filter (only valid for bundle scope)
        rename: apply LD3 deterministic collision chaining
        passphrase_env: env var holding passphrase (passphrase envelopes)
        private_key_path: path to RSA private key (RSA envelopes -- defers to .11)
        verify_signature: refuse on signature verification failure
        yes: skip interactive confirmation
        source_layout: testing-only LD7 override (requires ALIVE_P2P_TESTING=1)
        strict: turn step 10/11/12 warnings into a non-zero exit
        stdout: optional file-like for preview output (defaults to sys.stdout)

    Returns:
        dict with keys:
            status      -- "ok", "noop", "warn"
            import_id   -- canonical import_id (sha256 hex)
            scope       -- effective scope used
            applied_bundles -- list of bundle leaves actually applied
            bundle_renames  -- dict of {original: renamed} for collisions
            warnings    -- list of warning strings (LD1 steps 10/11/12)
            target      -- absolute target path
            source_layout -- "v2", "v3", or "agnostic" (LD7 inference)
            migration   -- migrate_v2_layout result dict if source_layout
                           was "v2", else None. Carries actions[],
                           warnings[], bundles_migrated[], tasks_converted,
                           errors[] -- callers can surface these directly.

    Raises:
        FileNotFoundError -- package or required dependency missing
        ValueError        -- pre-swap validation failure (LD1 steps 1-6)
        RuntimeError      -- swap failure (LD1 step 9)
        NotImplementedError -- RSA hybrid (deferred to .11)
    """
    if stdout is None:
        stdout = sys.stdout

    package_path = os.path.abspath(package_path)
    target_path = os.path.abspath(target_path)

    if not os.path.isfile(package_path):
        raise FileNotFoundError(
            "Package not found: {0}".format(package_path)
        )

    parent_target = os.path.dirname(target_path)
    if not os.path.isdir(parent_target):
        raise ValueError(
            "Parent directory '{0}' does not exist. Create it first, or "
            "choose a different --target path.".format(parent_target)
        )

    warnings_list = []  # type: List[str]
    cleanup_paths = []  # type: List[str]

    # ---- Step 1: extract --------------------------------------------------
    envelope = _detect_envelope(package_path)
    decrypted_archive = _decrypt_to_staging(
        package_path, envelope, passphrase_env, private_key_path,
        parent_target,
    )
    if decrypted_archive != package_path:
        # decrypted_archive lives in a sibling temp dir; track for cleanup.
        cleanup_paths.append(os.path.dirname(decrypted_archive))

    staging = tempfile.mkdtemp(
        prefix=".alive-receive-", dir=parent_target,
    )
    cleanup_paths.append(staging)

    try:
        safe_tar_extract(decrypted_archive, staging)
    except ValueError as exc:
        # Tar safety violation -- staging may exist but contains no files.
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError(
            "Package tar failed safety check: {0}".format(exc)
        )

    # Strip .alive/.walnut/__MACOSX dirs (defense in depth).
    stripped = _strip_unwanted_dirs_from_staging(staging)
    if stripped:
        warnings_list.append(
            "stripped {0} system dir(s) from package: {1}".format(
                len(stripped), ", ".join(stripped)
            )
        )

    # ---- Step 2: validate -------------------------------------------------
    manifest_path = os.path.join(staging, "manifest.yaml")
    if not os.path.isfile(manifest_path):
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError("Package missing manifest.yaml")

    manifest = read_manifest_yaml(manifest_path)
    ok, errors = validate_manifest(manifest)
    if not ok:
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError("Manifest validation failed: " + "; ".join(errors))

    ok, failures = verify_checksums(manifest, staging)
    if not ok:
        details = []
        for fail in failures:
            if fail.get("error") == "file_missing":
                details.append("missing: {0}".format(fail["path"]))
            else:
                details.append("mismatch: {0}".format(fail["path"]))
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError("Checksum verification failed: " + "; ".join(details))

    # Recompute payload_sha256 from files[] and compare.
    files_field = manifest.get("files", []) or []
    expected_payload = manifest.get("payload_sha256", "")
    actual_payload = compute_payload_sha256(files_field)
    if expected_payload and actual_payload != expected_payload:
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError(
            "Payload sha256 mismatch: manifest says {0}, computed {1}".format(
                expected_payload[:16], actual_payload[:16],
            )
        )

    # Signature: verify if present and required.
    signature = manifest.get("signature")
    signer = None  # type: Optional[str]
    if isinstance(signature, dict):
        signer = signature.get("pubkey_id", "unknown")
        if verify_signature:
            warnings_list.append(
                "signature verification requested but signer keyring "
                "lookup defers to task .11; skipping verify (signer "
                "pubkey_id: {0})".format(signer)
            )

    # Compute import_id from canonical bytes.
    import_id = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()

    # Effective scope: must match manifest if --scope provided.
    manifest_scope = manifest.get("scope")
    if not manifest_scope:
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError("Package manifest has no scope field. Package is malformed.")
    if scope and scope != manifest_scope:
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError(
            "--scope {0} does not match package scope {1}. Receive uses "
            "the package's declared scope.".format(scope, manifest_scope)
        )
    effective_scope = manifest_scope

    if bundle_names and effective_scope != "bundle":
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError(
            "--bundle is only valid when receiving a scope:bundle package"
        )

    # ---- Step 4: infer-layout (do this BEFORE dedupe so we can migrate) ---
    manifest_layout = manifest.get("source_layout")
    try:
        inferred_layout = _infer_source_layout(
            staging, manifest_layout, source_layout,
        )
    except ValueError as exc:
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise

    # ---- Step 6: migrate (before dedupe so bundle leaves are stable) ------
    # ``migrate_result`` is None for v3/agnostic packages and a result dict
    # for v2 packages so the preview step can surface the LD8 transform log
    # to the human and the return value can carry it back to callers/tests.
    migrate_result = None  # type: Optional[Dict[str, Any]]
    if inferred_layout == "v2":
        migrate_result = migrate_v2_layout(staging)
        if migrate_result.get("errors"):
            # LD8: migration failure aborts receive WITHOUT touching the
            # target. Preserve the staging tree as
            # .alive-receive-incomplete-{ts} next to the target so the
            # human can inspect or rerun ``alive-p2p.py migrate`` against it.
            preserved = None
            try:
                stamp = now_utc_iso().replace(":", "")
                preserved = os.path.join(
                    parent_target,
                    ".alive-receive-incomplete-{0}".format(stamp),
                )
                shutil.move(staging, preserved)
                cleanup_paths = [p for p in cleanup_paths if p != staging]
                print(
                    "staging preserved at {0}".format(preserved),
                    file=sys.stderr,
                )
            except (OSError, shutil.Error):
                preserved = None
            for p in cleanup_paths:
                shutil.rmtree(p, ignore_errors=True)
            raise ValueError(
                "v2 -> v3 staging migration failed: " + "; ".join(
                    migrate_result["errors"]
                )
            )

    # Now compute the bundle list visible in the (post-migrated) staging dir.
    package_bundles = _staging_top_level_bundles(staging)

    # Determine requested bundles for dedupe.
    if effective_scope == "full":
        requested_for_dedupe = package_bundles
    elif effective_scope == "snapshot":
        requested_for_dedupe = []
    else:  # bundle
        if bundle_names:
            # Validate the requested leaves exist in the package.
            unknown = [b for b in bundle_names if b not in package_bundles]
            if unknown:
                for p in cleanup_paths:
                    shutil.rmtree(p, ignore_errors=True)
                raise ValueError(
                    "Requested bundles not in package: {0}".format(
                        ", ".join(unknown)
                    )
                )
            requested_for_dedupe = list(bundle_names)
        else:
            requested_for_dedupe = list(package_bundles)

    # ---- Step 3: dedupe-check (LD2 subset-of-union) -----------------------
    ledger = _read_imports_ledger(target_path)
    is_noop, prior_applied, effective_to_apply = _compute_dedupe(
        ledger, import_id, requested_for_dedupe,
    )
    if is_noop:
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        return {
            "status": "noop",
            "import_id": import_id,
            "scope": effective_scope,
            "applied_bundles": [],
            "bundle_renames": {},
            "warnings": warnings_list,
            "target": target_path,
            "message": "already imported on prior receive; all requested "
                       "bundles already applied",
        }

    # ---- Step 5: scope-check ----------------------------------------------
    if effective_scope in ("full", "snapshot"):
        if os.path.exists(target_path):
            for p in cleanup_paths:
                shutil.rmtree(p, ignore_errors=True)
            raise ValueError(
                "Target path '{0}' already exists. Choose a non-existent "
                "path - receive will create it.".format(target_path)
            )
    elif effective_scope == "bundle":
        target_key = os.path.join(target_path, "_kernel", "key.md")
        if not os.path.isfile(target_key):
            for p in cleanup_paths:
                shutil.rmtree(p, ignore_errors=True)
            raise ValueError(
                "Target walnut '{0}' missing _kernel/key.md. Bundle scope "
                "requires an existing valid walnut.".format(target_path)
            )
        # LD18 walnut identity check.
        package_key = os.path.join(staging, "_kernel", "key.md")
        if os.path.isfile(package_key):
            with open(target_key, "rb") as f:
                target_key_bytes = f.read()
            with open(package_key, "rb") as f:
                package_key_bytes = f.read()
            if target_key_bytes != package_key_bytes:
                if os.environ.get("ALIVE_P2P_ALLOW_CROSS_WALNUT") != "1":
                    for p in cleanup_paths:
                        shutil.rmtree(p, ignore_errors=True)
                    raise ValueError(
                        "Package key.md does not match target walnut "
                        "key.md. This bundle was exported from a different "
                        "walnut. Aborting to prevent cross-walnut grafting. "
                        "Set ALIVE_P2P_ALLOW_CROSS_WALNUT=1 to override."
                    )
        # Pre-swap log validation: target log.md must have YAML frontmatter.
        target_log = os.path.join(target_path, "_kernel", "log.md")
        if not os.path.isfile(target_log):
            for p in cleanup_paths:
                shutil.rmtree(p, ignore_errors=True)
            raise ValueError(
                "Target walnut missing _kernel/log.md. Walnut is malformed "
                "or incomplete."
            )
        with open(target_log, "r", encoding="utf-8") as f:
            log_content = f.read()
        _, fm_block, _ = _parse_log_frontmatter(log_content)
        if not fm_block:
            for p in cleanup_paths:
                shutil.rmtree(p, ignore_errors=True)
            raise ValueError(
                "Target log.md has no YAML frontmatter. Walnut is malformed. "
                "Fix manually before retrying receive."
            )

    # Sensitivity for preview (read from package _kernel/key.md if present).
    sensitivity = manifest.get("sensitivity") or None
    sender = manifest.get("sender", "unknown")

    # ---- Bundle scope: pre-compute collision plan -------------------------
    rename_map = {}  # type: Dict[str, str]
    bundles_to_apply = effective_to_apply if effective_scope == "bundle" else []
    if effective_scope == "bundle":
        today = now_utc_iso().split("T")[0].replace("-", "")
        for leaf in bundles_to_apply:
            target_bundle_path = os.path.join(target_path, leaf)
            if os.path.exists(target_bundle_path):
                if not rename:
                    for p in cleanup_paths:
                        shutil.rmtree(p, ignore_errors=True)
                    raise ValueError(
                        "Bundle name collision at target: {0!r} already "
                        "exists. Re-run with --rename to apply LD3 "
                        "deterministic chaining.".format(leaf)
                    )
                renamed = _resolve_collision_name(target_path, leaf, today)
                rename_map[leaf] = renamed

    # ---- Step 7: preview ---------------------------------------------------
    file_count = len(files_field)
    try:
        package_size = os.path.getsize(package_path)
    except OSError:
        package_size = 0
    preview = _format_preview(
        scope=effective_scope,
        bundles_in_package=package_bundles,
        effective_to_apply=bundles_to_apply,
        prior_applied=prior_applied,
        file_count=file_count,
        package_size=package_size,
        envelope=envelope,
        signer=signer,
        sensitivity=sensitivity,
        rename_map=rename_map,
        migrate_result=migrate_result,
        source_layout=inferred_layout,
    )
    print(preview, file=stdout)
    if not yes:
        for p in cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)
        raise ValueError(
            "Interactive confirmation required: re-run with --yes to "
            "proceed (preview shown above)."
        )

    # ---- Step 8: acquire-lock ---------------------------------------------
    lock_strategy, lock_handle = _try_acquire_lock(target_path)

    swap_succeeded = False
    journal = None  # type: Optional[Dict[str, Any]]
    log_warned = False
    ledger_warned = False
    project_warned = False
    applied_bundles_final = []  # type: List[str]
    walnut_name = os.path.basename(target_path)
    try:
        # ---- Step 9: transact-swap ----------------------------------------
        if effective_scope in ("full", "snapshot"):
            # The target does not exist; staging dir IS the target.
            try:
                # Strip the package's manifest.yaml from staging before move
                # (it's a packaging artifact, not walnut content).
                staging_manifest = os.path.join(staging, "manifest.yaml")
                if os.path.isfile(staging_manifest):
                    os.unlink(staging_manifest)
                shutil.move(staging, target_path)
                cleanup_paths = [
                    p for p in cleanup_paths if p != staging
                ]
                applied_bundles_final = list(package_bundles)
                # For snapshot scope: ensure tasks.json + completed.json + log.md
                # are bootstrapped.
                if effective_scope == "snapshot":
                    kernel_dir = os.path.join(target_path, "_kernel")
                    os.makedirs(kernel_dir, exist_ok=True)
                    tasks_path = os.path.join(kernel_dir, "tasks.json")
                    if not os.path.isfile(tasks_path):
                        _atomic_write_text(tasks_path, '{"tasks": []}\n')
                    completed_path = os.path.join(kernel_dir, "completed.json")
                    if not os.path.isfile(completed_path):
                        _atomic_write_text(completed_path, '{"completed": []}\n')
                swap_succeeded = True
            except (OSError, shutil.Error) as exc:
                # Rollback: target may have been partially created.
                if os.path.exists(target_path):
                    shutil.rmtree(target_path, ignore_errors=True)
                raise RuntimeError(
                    "swap failed (full/snapshot): {0}".format(exc)
                )
        else:
            # bundle scope: journaled move
            ops = []  # type: List[Dict[str, Any]]
            for leaf in bundles_to_apply:
                src = os.path.join(staging, leaf)
                dst_name = rename_map.get(leaf, leaf)
                dst = os.path.join(target_path, dst_name)
                ops.append({
                    "op": "move",
                    "src": src,
                    "dst": dst,
                    "leaf": leaf,
                    "renamed_to": dst_name,
                    "status": "pending",
                })
            journal = {
                "target": target_path,
                "import_id": import_id,
                "started_at": now_utc_iso(),
                "operations": ops,
            }
            _write_journal(staging, journal)

            done_ops = []  # type: List[Dict[str, Any]]
            try:
                for op in ops:
                    op["status"] = "committing"
                    _write_journal(staging, journal)
                    shutil.move(op["src"], op["dst"])
                    op["status"] = "done"
                    _write_journal(staging, journal)
                    done_ops.append(op)
                    applied_bundles_final.append(op["renamed_to"])
                swap_succeeded = True
            except (OSError, shutil.Error) as exc:
                # Reverse rollback.
                for op in reversed(done_ops):
                    try:
                        shutil.move(op["dst"], op["src"])
                        op["status"] = "rolled_back"
                    except (OSError, shutil.Error):
                        op["status"] = "rollback_failed"
                _write_journal(staging, journal)
                # Preserve staging for diagnosis.
                incomplete_path = os.path.join(
                    parent_target,
                    ".alive-receive-incomplete-{0}".format(
                        now_utc_iso().replace(":", "")
                    ),
                )
                try:
                    shutil.move(staging, incomplete_path)
                    cleanup_paths = [
                        p for p in cleanup_paths if p != staging
                    ]
                    print("staging preserved at {0}".format(incomplete_path),
                          file=sys.stderr)
                except (OSError, shutil.Error):
                    pass
                raise RuntimeError(
                    "swap failed (bundle scope): {0}".format(exc)
                )

        # ---- Step 10: log-edit (NON-FATAL post-swap) ----------------------
        try:
            if effective_scope == "snapshot":
                allow_create = True
            elif effective_scope == "full":
                allow_create = True
            else:
                allow_create = False
            iso_now = now_utc_iso()
            session_id = resolve_session_id()
            _edit_log_md(
                target_path=target_path,
                iso_timestamp=iso_now,
                session_id=session_id,
                sender=sender,
                scope=effective_scope,
                bundles=applied_bundles_final or None,
                source_layout=inferred_layout,
                import_id=import_id,
                walnut_name=walnut_name,
                allow_create=allow_create,
            )
        except (FileNotFoundError, ValueError, OSError) as exc:
            log_warned = True
            warnings_list.append(
                "log edit failed - walnut structurally correct but log "
                "missing this import entry. Recovery: alive-p2p.py "
                "log-import --walnut {0} --import-id {1} ({2})".format(
                    target_path, import_id[:16], exc
                )
            )

        # ---- Step 11: ledger-write (NON-FATAL post-swap) -------------------
        try:
            new_entry = {
                "import_id": import_id,
                "format_version": manifest.get("format_version", "2.1.0"),
                "source_layout": inferred_layout,
                "scope": effective_scope,
                "package_bundles": package_bundles,
                "applied_bundles": applied_bundles_final,
                "bundle_renames": rename_map,
                "sender": sender,
                "created": manifest.get("created", ""),
                "received_at": now_utc_iso(),
            }
            ledger = _read_imports_ledger(target_path)
            ledger.setdefault("imports", []).append(new_entry)
            _write_imports_ledger(target_path, ledger)
        except (OSError, IOError, ValueError) as exc:
            ledger_warned = True
            warnings_list.append(
                "ledger write failed - future duplicate imports of this "
                "package will not dedupe. Recovery: manually append entry "
                "to _kernel/imports.json ({0})".format(exc)
            )

        # ---- Step 12: regenerate-now (NON-FATAL) --------------------------
        if not os.environ.get("ALIVE_P2P_SKIP_REGEN"):
            ok_regen, msg = _regenerate_now_json(target_path)
            if not ok_regen:
                project_warned = True
                warnings_list.append(
                    "now.json regeneration failed - walnut is correct but "
                    "projection is stale. Recovery: python3 {0}/scripts/"
                    "project.py --walnut {1} ({2})".format(
                        _resolve_plugin_root(), target_path, msg,
                    )
                )

    finally:
        # ---- Step 13: cleanup-and-release (ALWAYS RUNS) -------------------
        _release_lock(lock_strategy, lock_handle, target_path)

        if swap_succeeded:
            # If steps 10/11 warned: preserve staging+journal as .incomplete.
            if (log_warned or ledger_warned) and staging and os.path.isdir(staging):
                stamp = now_utc_iso().replace(":", "")
                incomplete = os.path.join(
                    parent_target,
                    ".alive-receive-incomplete-{0}".format(stamp),
                )
                try:
                    shutil.move(staging, incomplete)
                    cleanup_paths = [p for p in cleanup_paths if p != staging]
                except (OSError, shutil.Error):
                    pass
            else:
                # Clean delete journal + staging.
                if staging and os.path.isdir(staging):
                    shutil.rmtree(staging, ignore_errors=True)
                cleanup_paths = [p for p in cleanup_paths if p != staging]

        # Always clean any decrypt temp dirs.
        for p in cleanup_paths:
            if p and os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)

    status = "ok"
    if log_warned or ledger_warned or project_warned:
        status = "warn"
        if strict:
            # Caller (CLI) decides exit code based on this status.
            pass

    return {
        "status": status,
        "import_id": import_id,
        "scope": effective_scope,
        "applied_bundles": applied_bundles_final,
        "bundle_renames": rename_map,
        "warnings": warnings_list,
        "target": target_path,
        "source_layout": inferred_layout,
        "migration": migrate_result,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#
# The full user-facing CLI (share / receive / encrypt / decrypt / sign /
# verify) lands in later fn-7-7cw tasks. Right now ``migrate``, ``create``,
# ``list-bundles``, ``receive``, ``info``, ``log-import``, ``unlock``, and
# ``verify`` are wired up.


def _cmd_migrate(args):
    # type: (Any) -> int
    """Run ``migrate_v2_layout`` against an extracted staging directory.

    Prints the result dict to stdout -- human-readable by default, JSON when
    ``--json`` is set. Exit code is always 0 unless the helper recorded
    errors, in which case the exit code is 1 so shell callers can detect
    partial failure.
    """
    staging = args.staging
    if not os.path.isdir(staging):
        print(
            "error: staging dir does not exist: {0}".format(staging),
            file=sys.stderr,
        )
        return 2

    result = migrate_v2_layout(staging)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("migrate_v2_layout result:")
        print("  staging:          {0}".format(os.path.abspath(staging)))
        print("  bundles_migrated: {0}".format(
            ", ".join(result["bundles_migrated"]) or "(none)"
        ))
        print("  tasks_converted:  {0}".format(result["tasks_converted"]))
        if result["actions"]:
            print("  actions:")
            for line in result["actions"]:
                print("    - {0}".format(line))
        if result["warnings"]:
            print("  warnings:")
            for line in result["warnings"]:
                print("    - {0}".format(line))
        if result["errors"]:
            print("  errors:")
            for line in result["errors"]:
                print("    - {0}".format(line))

    return 1 if result["errors"] else 0


def _cmd_create(args):
    # type: (Any) -> int
    """Run ``create_package`` against a walnut and write a .walnut file.

    Wraps the LD11 share CLI contract: validates flags, calls
    ``create_package``, prints a human-readable summary (or JSON when
    ``--json`` is set). Exit code is 0 on success, 1 on validation /
    runtime error, 2 on filesystem precondition failure.
    """
    try:
        result = create_package(
            walnut_path=args.walnut,
            scope=args.scope,
            output_path=args.output,
            bundle_names=args.bundle or None,
            description=args.description or "",
            note=args.note or "",
            session_id=None,
            engine=os.environ.get("ALIVE_ENGINE", "unknown"),
            plugin_version="3.1.0",
            sender=None,
            exclusions=args.exclude or None,
            preset=args.preset,
            exclude_from_peer=getattr(args, "exclude_from", None),
            include_full_history=args.include_full_history,
            encrypt_mode=args.encrypt,
            passphrase_env=args.passphrase_env,
            recipient_peers=args.recipient or None,
            sign=args.sign,
            source_layout=args.source_layout,
            yes=args.yes,
        )
    except (ValueError, KeyError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    if args.json:
        # Strip the manifest for JSON output -- it can be huge and the
        # caller can read manifest.yaml from inside the package if they
        # want the full schema. Keep the import_id and the file path.
        compact = {
            "package_path": result["package_path"],
            "size_bytes": result["size_bytes"],
            "import_id": result["import_id"],
            "warnings": result["warnings"],
            "exclusions_applied": result["exclusions_applied"],
            "removed_paths": result["removed_paths"],
            "preferences_found": result["preferences_found"],
            "world_root": result["world_root"],
        }
        print(json.dumps(compact, indent=2, ensure_ascii=False))
    else:
        print("created package: {0}".format(result["package_path"]))
        print("  size:        {0} bytes".format(result["size_bytes"]))
        print("  import_id:   {0}".format(result["import_id"][:16]))
        print("  scope:       {0}".format(args.scope))
        if args.bundle:
            print("  bundles:     {0}".format(", ".join(args.bundle)))
        if result["exclusions_applied"]:
            print("  exclusions:  {0}".format(
                ", ".join(result["exclusions_applied"])
            ))
        if result["warnings"]:
            print("  warnings:")
            for w in result["warnings"]:
                print("    - {0}".format(w))

    return 0


def _cmd_list_bundles(args):
    # type: (Any) -> int
    """Enumerate top-level bundles in a walnut for the share skill.

    Output schema (JSON):
        [{"name": <leaf>, "relpath": <posix relpath>,
          "abs_path": <absolute path>, "top_level": True/False}, ...]

    Human output is a one-bundle-per-line summary with leaf name +
    indication when the bundle is nested. Both forms include nested
    (non-shareable) bundles in the result so the share skill can warn
    the human about them.
    """
    walnut = os.path.abspath(args.walnut)
    if not os.path.isdir(walnut):
        print(
            "error: walnut path not found: {0}".format(walnut),
            file=sys.stderr,
        )
        return 2

    if walnut_paths is None:  # pragma: no cover -- defensive only
        print("error: walnut_paths module not available", file=sys.stderr)
        return 1

    bundles = []  # type: List[Dict[str, Any]]
    for relpath, abs_path in walnut_paths.find_bundles(walnut):
        leaf = relpath.split("/")[-1]
        bundles.append({
            "name": leaf,
            "relpath": relpath,
            "abs_path": abs_path,
            "top_level": is_top_level_bundle(relpath),
        })

    if args.json:
        print(json.dumps(bundles, indent=2, ensure_ascii=False))
    else:
        if not bundles:
            print("(no bundles found in {0})".format(walnut))
        else:
            print("bundles in {0}:".format(walnut))
            for b in bundles:
                tag = "" if b["top_level"] else "  [nested -- not shareable]"
                print("  - {0}{1}".format(b["name"], tag))
                if b["relpath"] != b["name"]:
                    print("      relpath: {0}".format(b["relpath"]))

    return 0


def _cmd_receive(args):
    # type: (Any) -> int
    """Run the LD1 receive pipeline against an input package + target walnut.

    Wraps ``receive_package`` and translates exceptions to actionable
    exit codes:
        0 -- success or no-op (with warnings if --strict not set)
        1 -- pre-swap or swap failure
        2 -- filesystem precondition (parent missing, etc.)
    """
    try:
        result = receive_package(
            package_path=args.input,
            target_path=args.target,
            scope=args.scope,
            bundle_names=args.bundle or None,
            rename=args.rename,
            passphrase_env=args.passphrase_env,
            private_key_path=args.private_key,
            verify_signature=args.verify_signature,
            yes=args.yes,
            source_layout=args.source_layout,
            strict=args.strict,
        )
    except FileNotFoundError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    if result.get("status") == "noop":
        print("noop: {0}".format(result.get("message", "already imported")))
        return 0

    print("received package: target={0}".format(result["target"]))
    print("  import_id:        {0}".format(result["import_id"][:16]))
    print("  scope:            {0}".format(result["scope"]))
    if result["applied_bundles"]:
        print("  applied bundles:  {0}".format(
            ", ".join(result["applied_bundles"])
        ))
    if result["bundle_renames"]:
        print("  renames:")
        for src, dst in sorted(result["bundle_renames"].items()):
            print("    {0} -> {1}".format(src, dst))
    if result["warnings"]:
        print("  warnings:")
        for w in result["warnings"]:
            print("    - {0}".format(w))

    if args.strict and result.get("status") == "warn":
        return 1
    return 0


def _cmd_info(args):
    # type: (Any) -> int
    """Display package metadata. Envelope-only mode for missing creds.

    Behaviour by envelope (LD24):
        gzip       -- read manifest.yaml directly from tar, full output
        passphrase -- requires --passphrase-env; without it, envelope-only
                      output and exit 0 (info is a discovery tool)
        rsa        -- requires --private-key; without it, envelope-only
                      output and exit 0
    """
    package = os.path.abspath(args.package)
    if not os.path.isfile(package):
        print("error: package not found: {0}".format(package), file=sys.stderr)
        return 1

    try:
        envelope = _detect_envelope(package)
    except (ValueError, FileNotFoundError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    try:
        size = os.path.getsize(package)
    except OSError:
        size = 0

    if envelope == "gzip":
        # Read manifest directly from the tarball.
        try:
            with tarfile.open(package, "r:gz") as tar:
                manifest_member = None
                for m in tar.getmembers():
                    if m.name == "manifest.yaml" or m.name.endswith("/manifest.yaml"):
                        manifest_member = m
                        break
                if manifest_member is None:
                    print("error: package missing manifest.yaml", file=sys.stderr)
                    return 1
                f = tar.extractfile(manifest_member)
                manifest_bytes = f.read() if f else b""
        except (tarfile.TarError, OSError) as exc:
            print("error: {0}".format(exc), file=sys.stderr)
            return 1
        try:
            manifest = parse_manifest(manifest_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            print("error: parse manifest: {0}".format(exc), file=sys.stderr)
            return 1
        info_dict = {
            "package": package,
            "size": size,
            "encryption": "none",
            "format_version": manifest.get("format_version", ""),
            "source_layout": manifest.get("source_layout", ""),
            "scope": manifest.get("scope", ""),
            "sender": manifest.get("sender", "unknown"),
            "created": manifest.get("created", ""),
            "bundles": manifest.get("bundles", []) or [],
            "exclusions_applied": manifest.get("exclusions_applied", []) or [],
            "file_count": len(manifest.get("files", []) or []),
            "signature": "present" if manifest.get("signature") else "absent",
        }
        try:
            info_dict["import_id"] = hashlib.sha256(
                canonical_manifest_bytes(manifest)
            ).hexdigest()
        except Exception:
            info_dict["import_id"] = "unknown"
    else:
        # Encrypted: emit envelope-only when creds missing.
        if envelope == "passphrase" and not args.passphrase_env:
            info_dict = {
                "package": package,
                "size": size,
                "encryption": "passphrase",
                "note": "Re-run with --passphrase-env <ENV_VAR> to read the "
                        "full manifest.",
            }
        elif envelope == "rsa" and not args.private_key:
            info_dict = {
                "package": package,
                "size": size,
                "encryption": "rsa",
                "note": "Re-run with --private-key <PATH> to read the full "
                        "manifest.",
            }
        else:
            print(
                "error: full info for encrypted packages requires the "
                "decryption credentials and the LD21 RSA path which lands "
                "in task .11. Envelope-only output not requested.",
                file=sys.stderr,
            )
            return 1

    if args.json:
        print(json.dumps(info_dict, indent=2, ensure_ascii=False))
    else:
        print("Package:        {0}".format(info_dict["package"]))
        print("Size:           {0} bytes".format(info_dict["size"]))
        print("Encryption:     {0}".format(info_dict["encryption"]))
        if info_dict.get("format_version"):
            print("Format version: {0}".format(info_dict["format_version"]))
        if info_dict.get("source_layout"):
            print("Source layout:  {0}".format(info_dict["source_layout"]))
        if info_dict.get("scope"):
            print("Scope:          {0}".format(info_dict["scope"]))
        if info_dict.get("sender"):
            print("Sender:         {0}".format(info_dict["sender"]))
        if info_dict.get("created"):
            print("Created:        {0}".format(info_dict["created"]))
        if info_dict.get("bundles"):
            print("Bundles:        {0}".format(
                ", ".join(info_dict["bundles"])
            ))
        if info_dict.get("exclusions_applied"):
            print("Exclusions:     {0}".format(
                ", ".join(info_dict["exclusions_applied"])
            ))
        if "file_count" in info_dict:
            print("File count:     {0}".format(info_dict["file_count"]))
        if "signature" in info_dict:
            print("Signature:      {0}".format(info_dict["signature"]))
        if "import_id" in info_dict:
            print("import_id:      {0}".format(info_dict["import_id"][:16]))
        if "note" in info_dict:
            print("note:           {0}".format(info_dict["note"]))

    return 0


def _cmd_log_import(args):
    # type: (Any) -> int
    """Manual log-import recovery tool (LD24). Append a single import entry
    to ``{walnut}/_kernel/log.md`` after the YAML frontmatter.

    Used when the receive pipeline's step 10 failed post-swap.
    """
    walnut = os.path.abspath(args.walnut)
    if not os.path.isdir(walnut):
        print("error: walnut not found: {0}".format(walnut), file=sys.stderr)
        return 1
    bundles = None  # type: Optional[List[str]]
    if args.bundles:
        bundles = [b.strip() for b in args.bundles.split(",") if b.strip()]
    try:
        _edit_log_md(
            target_path=walnut,
            iso_timestamp=now_utc_iso(),
            session_id=resolve_session_id(),
            sender=args.sender or "unknown",
            scope=args.scope or "bundle",
            bundles=bundles,
            source_layout=args.source_layout or "v3",
            import_id=args.import_id,
            walnut_name=os.path.basename(walnut),
            allow_create=False,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1
    print("ok: log entry appended to {0}/_kernel/log.md".format(walnut))
    return 0


def _cmd_unlock(args):
    # type: (Any) -> int
    """Force-release a stuck walnut lock per LD28. Checks BOTH lock artifacts
    (``.lock`` file and ``.lock.d/`` dir) and removes the one that exists if
    its holder PID is dead.

    Exit codes:
        0 -- removed an active stale lock
        1 -- refused (live PID)
        2 -- no lock artifact found
    """
    walnut = os.path.abspath(args.walnut)
    base = _walnut_lock_path(walnut)
    file_path = base
    dir_path = base + ".d"

    found = None
    holder_text = ""
    if os.path.isfile(file_path):
        found = file_path
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                holder_text = f.read()
        except (IOError, OSError):
            pass
    elif os.path.isdir(dir_path):
        found = dir_path
        holder_file = os.path.join(dir_path, "holder.txt")
        if os.path.isfile(holder_file):
            try:
                with open(holder_file, "r", encoding="utf-8") as f:
                    holder_text = f.read()
            except (IOError, OSError):
                pass

    if not found:
        print("no lock artifact found for {0}".format(walnut))
        return 2

    holder_pid = _parse_holder_pid(holder_text)
    if holder_pid and not _is_pid_dead(holder_pid):
        print(
            "error: lock held by running process {0}. Kill the process "
            "or wait for it to complete.".format(holder_pid),
            file=sys.stderr,
        )
        return 1

    try:
        if os.path.isfile(found):
            os.unlink(found)
        else:
            shutil.rmtree(found, ignore_errors=True)
    except OSError as exc:
        print("error: cannot remove lock: {0}".format(exc), file=sys.stderr)
        return 1
    print("ok: lock removed (was holder pid {0})".format(holder_pid or "?"))
    return 0


def _cmd_verify(args):
    # type: (Any) -> int
    """Verify a package: signature, per-file checksums, payload sha256,
    schema. Extracts to a temp dir on the same filesystem as the package
    and cleans up on exit.

    Exit code 0 if all checks pass, 1 otherwise.
    """
    package = os.path.abspath(args.package)
    if not os.path.isfile(package):
        print("error: package not found: {0}".format(package), file=sys.stderr)
        return 1
    parent = os.path.dirname(package)

    try:
        envelope = _detect_envelope(package)
    except (ValueError, FileNotFoundError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    try:
        plaintext = _decrypt_to_staging(
            package, envelope, args.passphrase_env, args.private_key, parent,
        )
    except (NotImplementedError, ValueError, RuntimeError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    cleanup = []  # type: List[str]
    if plaintext != package:
        cleanup.append(os.path.dirname(plaintext))

    staging = tempfile.mkdtemp(prefix=".alive-verify-", dir=parent)
    cleanup.append(staging)

    rc = 0
    try:
        try:
            safe_tar_extract(plaintext, staging)
        except ValueError as exc:
            print("FAIL tar safety: {0}".format(exc), file=sys.stderr)
            return 1
        manifest_path = os.path.join(staging, "manifest.yaml")
        if not os.path.isfile(manifest_path):
            print("FAIL: manifest.yaml missing", file=sys.stderr)
            return 1
        try:
            manifest = read_manifest_yaml(manifest_path)
        except (ValueError, IOError, OSError) as exc:
            print("FAIL parse manifest: {0}".format(exc), file=sys.stderr)
            return 1

        ok, errors = validate_manifest(manifest)
        print("Format version: {0} ({1})".format(
            "PASS" if ok else "FAIL",
            manifest.get("format_version", "?"),
        ))
        print("Source layout:  PASS ({0})".format(
            manifest.get("source_layout", "?")
        ))
        print("Scope:          {0}".format(manifest.get("scope", "?")))
        print("Schema:         {0}".format("PASS" if ok else "FAIL"))
        if not ok:
            for e in errors:
                print("  - {0}".format(e), file=sys.stderr)
            rc = 1

        ok_chk, failures = verify_checksums(manifest, staging)
        files_count = len(manifest.get("files", []) or [])
        if ok_chk:
            print("File checksums: PASS ({0} files)".format(files_count))
        else:
            print("File checksums: FAIL ({0} failures)".format(len(failures)))
            rc = 1

        expected_payload = manifest.get("payload_sha256", "")
        actual_payload = compute_payload_sha256(manifest.get("files", []) or [])
        if expected_payload and actual_payload == expected_payload:
            print("Payload sha256: PASS")
        elif expected_payload:
            print("Payload sha256: FAIL (manifest {0} != computed {1})".format(
                expected_payload[:16], actual_payload[:16],
            ))
            rc = 1
        else:
            print("Payload sha256: SKIP (manifest field missing)")

        sig = manifest.get("signature")
        if sig:
            print("Signature:      present (signer pubkey_id: {0}) - "
                  "verification defers to task .11".format(
                      sig.get("pubkey_id", "?")
                  ))
        else:
            print("Signature:      absent")
    finally:
        for p in cleanup:
            if p and os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)

    return rc


def _cli(argv=None):
    # type: (Optional[List[str]]) -> None
    """Dispatch the argparse CLI for the v3 P2P share + maintenance verbs."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="alive-p2p.py",
        description=(
            "ALIVE v3 P2P sharing layer CLI. Subcommands: create, "
            "list-bundles, migrate. The receive pipeline lands in task .8."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    # ---- create ----------------------------------------------------------
    create_p = sub.add_parser(
        "create",
        help="Create a .walnut package from a walnut (full|bundle|snapshot).",
    )
    create_p.add_argument(
        "--scope",
        required=True,
        choices=("full", "bundle", "snapshot"),
        help="Package scope per LD18.",
    )
    create_p.add_argument(
        "--walnut",
        required=True,
        help="Absolute path to the source walnut.",
    )
    create_p.add_argument(
        "--output",
        default=None,
        help="Output .walnut file path (default: ~/Desktop/{walnut}-{scope}-{date}.walnut).",
    )
    create_p.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="Bundle leaf name (repeatable, required for --scope bundle).",
    )
    create_p.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclusion glob (repeatable, additive to preset exclusions).",
    )
    create_p.add_argument(
        "--preset",
        default=None,
        help="Share preset name (loaded from .alive/preferences.yaml p2p.share_presets).",
    )
    create_p.add_argument(
        "--include-full-history",
        action="store_true",
        help="Override LD9 baseline stubs and ship real log/insights content.",
    )
    create_p.add_argument(
        "--exclude-from",
        default=None,
        help="Apply exclusion patterns from a peer entry in ~/.alive/relay/relay.json.",
    )
    create_p.add_argument(
        "--source-layout",
        default="v3",
        choices=("v2", "v3"),
        help="Wire layout for the package (default v3; v2 is testing only).",
    )
    create_p.add_argument(
        "--encrypt",
        default="none",
        choices=("none", "passphrase", "rsa"),
        help="Encryption envelope (default none).",
    )
    create_p.add_argument(
        "--passphrase-env",
        default=None,
        help="Env var holding the passphrase (required for --encrypt passphrase).",
    )
    create_p.add_argument(
        "--recipient",
        action="append",
        default=[],
        help="Peer name (repeatable, required for --encrypt rsa).",
    )
    create_p.add_argument(
        "--sign",
        action="store_true",
        help="Sign the manifest using p2p.signing_key_path from preferences.",
    )
    create_p.add_argument(
        "--description",
        default="",
        help="Optional human-readable description (single line).",
    )
    create_p.add_argument(
        "--note",
        default="",
        help="Optional personal note (single line).",
    )
    create_p.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation (no-op today; receive uses it).",
    )
    create_p.add_argument(
        "--json",
        action="store_true",
        help="Emit a compact JSON summary instead of human-readable text.",
    )
    create_p.set_defaults(func=_cmd_create)

    # ---- list-bundles ----------------------------------------------------
    list_p = sub.add_parser(
        "list-bundles",
        help="List top-level bundles in a walnut for the share skill.",
    )
    list_p.add_argument(
        "--walnut",
        required=True,
        help="Absolute path to the walnut.",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    list_p.set_defaults(func=_cmd_list_bundles)

    # ---- migrate ---------------------------------------------------------
    migrate_p = sub.add_parser(
        "migrate",
        help="Transform a v2 package staging dir into v3 shape in place.",
    )
    migrate_p.add_argument(
        "--staging",
        required=True,
        help="Path to the extracted staging directory.",
    )
    migrate_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the result dict as JSON instead of human-readable text.",
    )
    migrate_p.set_defaults(func=_cmd_migrate)

    # ---- receive ---------------------------------------------------------
    receive_p = sub.add_parser(
        "receive",
        help="Import a .walnut package into a target walnut (LD1 pipeline).",
    )
    receive_p.add_argument(
        "input",
        help="Path to the .walnut package to import.",
    )
    receive_p.add_argument(
        "--target",
        required=True,
        help="Target walnut path (must NOT exist for full/snapshot scope; "
             "MUST exist for bundle scope).",
    )
    receive_p.add_argument(
        "--scope",
        default=None,
        choices=("full", "bundle", "snapshot"),
        help="Optional CLI override; must match the package's manifest scope.",
    )
    receive_p.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="Bundle leaf name (repeatable; valid only for --scope bundle).",
    )
    receive_p.add_argument(
        "--rename",
        action="store_true",
        help="Apply LD3 deterministic collision chaining on bundle name "
             "collisions instead of refusing.",
    )
    receive_p.add_argument(
        "--passphrase-env",
        default=None,
        help="Env var holding the passphrase (required for passphrase "
             "envelopes).",
    )
    receive_p.add_argument(
        "--private-key",
        default=None,
        help="Path to the local RSA private key (required for RSA hybrid "
             "envelopes; defers to task .11).",
    )
    receive_p.add_argument(
        "--verify-signature",
        action="store_true",
        help="Refuse the receive on signature verification failure.",
    )
    receive_p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive preview confirmation. REQUIRED for "
             "non-interactive use.",
    )
    receive_p.add_argument(
        "--source-layout",
        default=None,
        choices=("v2", "v3"),
        help="Override LD7 layout inference (requires ALIVE_P2P_TESTING=1).",
    )
    receive_p.add_argument(
        "--strict",
        action="store_true",
        help="Turn LD1 step 10/11/12 warnings into a non-zero exit code.",
    )
    receive_p.set_defaults(func=_cmd_receive)

    # ---- info ------------------------------------------------------------
    info_p = sub.add_parser(
        "info",
        help="Display package metadata (envelope-only for encrypted "
             "packages without credentials).",
    )
    info_p.add_argument(
        "package",
        help="Path to the .walnut package.",
    )
    info_p.add_argument(
        "--passphrase-env",
        default=None,
        help="Env var holding the passphrase for full info on passphrase "
             "envelopes.",
    )
    info_p.add_argument(
        "--private-key",
        default=None,
        help="Path to the RSA private key for full info on RSA envelopes "
             "(deferred to .11).",
    )
    info_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    info_p.set_defaults(func=_cmd_info)

    # ---- log-import ------------------------------------------------------
    logimp_p = sub.add_parser(
        "log-import",
        help="Manually append an import entry to a walnut log.md (recovery "
             "tool for LD1 step 10 failures).",
    )
    logimp_p.add_argument(
        "--walnut",
        required=True,
        help="Target walnut path.",
    )
    logimp_p.add_argument(
        "--import-id",
        required=True,
        help="Import id (sha256 hex) to record in the entry.",
    )
    logimp_p.add_argument(
        "--sender",
        default=None,
        help="Sender handle to record in the entry (default 'unknown').",
    )
    logimp_p.add_argument(
        "--scope",
        default=None,
        choices=("full", "bundle", "snapshot"),
        help="Scope to record in the entry (default 'bundle').",
    )
    logimp_p.add_argument(
        "--bundles",
        default=None,
        help="Comma-separated bundle leaf names to record.",
    )
    logimp_p.add_argument(
        "--source-layout",
        default=None,
        help="Source layout to record (default 'v3').",
    )
    logimp_p.set_defaults(func=_cmd_log_import)

    # ---- unlock ----------------------------------------------------------
    unlock_p = sub.add_parser(
        "unlock",
        help="Force-release a stuck walnut lock (stale PID recovery).",
    )
    unlock_p.add_argument(
        "--walnut",
        required=True,
        help="Target walnut path whose lock should be released.",
    )
    unlock_p.set_defaults(func=_cmd_unlock)

    # ---- verify ----------------------------------------------------------
    verify_p = sub.add_parser(
        "verify",
        help="Verify a package: signature, per-file checksums, payload sha.",
    )
    verify_p.add_argument(
        "--package",
        required=True,
        help="Path to the .walnut package.",
    )
    verify_p.add_argument(
        "--passphrase-env",
        default=None,
        help="Env var holding the passphrase for passphrase envelopes.",
    )
    verify_p.add_argument(
        "--private-key",
        default=None,
        help="Path to the RSA private key for RSA hybrid envelopes "
             "(deferred to .11).",
    )
    verify_p.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        sys.exit(0)

    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    _cli()
