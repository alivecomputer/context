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
from typing import Any, Dict, List, Optional, Tuple

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


def safe_tar_extract(archive_path, output_dir):
    # type: (str, str) -> None
    """Extract a tar.gz archive with path-traversal and symlink protection.

    - Rejects entries with ``../`` or absolute paths (Zip Slip).
    - Rejects symlinks pointing outside *output_dir*.
    - Extracts to a staging directory first, then moves into *output_dir*.
    """
    archive_path = os.path.abspath(archive_path)
    output_dir = os.path.abspath(output_dir)

    if not os.path.isfile(archive_path):
        raise FileNotFoundError("Archive not found: {0}".format(archive_path))

    os.makedirs(output_dir, exist_ok=True)

    # Use a staging directory in the same parent (same filesystem for rename)
    parent = os.path.dirname(output_dir)
    staging = tempfile.mkdtemp(dir=parent, prefix=".p2p-extract-")

    try:
        with tarfile.open(archive_path, "r:*") as tar:
            # First pass: validate every entry
            for member in tar.getmembers():
                # Reject absolute paths
                if os.path.isabs(member.name):
                    raise ValueError(
                        "Absolute path in archive: {0}".format(member.name)
                    )

                # Reject path traversal
                resolved = _resolve_path(staging, member.name)
                if resolved is None:
                    raise ValueError(
                        "Path traversal in archive: {0}".format(member.name)
                    )

                # Reject symlinks that escape output
                if member.issym() or member.islnk():
                    link_target = member.linkname
                    # For symlinks, resolve relative to the member's parent
                    member_parent = os.path.join(
                        staging, os.path.dirname(member.name)
                    )
                    if os.path.isabs(link_target):
                        link_resolved = link_target
                    else:
                        link_resolved = os.path.normpath(
                            os.path.join(member_parent, link_target)
                        )
                    if not (link_resolved == staging
                            or link_resolved.startswith(staging + os.sep)):
                        raise ValueError(
                            "Symlink escapes output: {0} -> {1}".format(
                                member.name, member.linkname
                            )
                        )

            # Second pass: extract (rewind)
            tar.extractall(path=staging)

        # Move contents from staging into output_dir
        for item in os.listdir(staging):
            src = os.path.join(staging, item)
            dst = os.path.join(output_dir, item)
            if os.path.exists(dst):
                # Remove existing to allow overwrite
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            os.replace(src, dst)

    finally:
        # Clean up staging directory
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


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
):
    # type: (str, str, Optional[List[str]], Optional[str], Optional[str], bool, Optional[str], Optional[List[str]]) -> str
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
    """
    if scope not in ("full", "bundle", "snapshot"):
        raise ValueError(
            "Unknown staging scope '{0}'; expected full|bundle|snapshot".format(
                scope
            )
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
# CLI placeholder
# ---------------------------------------------------------------------------
#
# The user-facing CLI (share / receive / encrypt / decrypt / sign / verify)
# lands in fn-7-7cw.5. This stub exists so ``python3 alive-p2p.py`` does not
# crash with a NameError when invoked during smoke checks.

def _cli():
    # type: () -> None
    """Print the module docstring and exit. Real CLI lands in task .5."""
    print(__doc__)
    sys.exit(0)


if __name__ == "__main__":
    _cli()
