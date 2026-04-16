#!/usr/bin/env bash
#
# build-mcpb.sh — pack alive-mcp into a Claude Desktop one-click install bundle.
#
# Produces ``dist/alive-mcp-<version>.mcpb`` from a clean staging directory
# so the bundle contains ONLY the files Claude Desktop needs to run the
# server:
#
#   manifest.json
#   pyproject.toml
#   uv.lock          (optional — locks the dep graph for reproducibility)
#   README.md
#   LICENSE
#   src/alive_mcp/   (the package source)
#
# Because ``server.type: "uv"`` in the manifest, Claude Desktop uses ``uv``
# to resolve + cache the environment at first-run. The bundle therefore
# ships source + pyproject ONLY; no wheels, no .venv, no node_modules,
# no tests. Target bundle size is under 500KB. See
# ``docs/release/mcpb-install-verification.md`` for the install-side
# verification matrix.
#
# Dependencies
# ------------
# Requires the ``mcpb`` CLI from ``@anthropic-ai/mcpb``. If it is not on
# PATH we install it globally via npm:
#
#   npm install -g @anthropic-ai/mcpb
#
# The script fails fast with a helpful message if ``npm`` itself is
# missing (you need Node.js installed). On CI we pin ``mcpb`` via the
# same npm global install — no version pin yet because upstream is
# young and still iterating; revisit when ``mcpb`` reaches 1.0.
#
# Usage
# -----
#   scripts/build-mcpb.sh              # build dist/alive-mcp-<version>.mcpb
#   scripts/build-mcpb.sh --validate   # validate the staged manifest, then pack
#   scripts/build-mcpb.sh -h           # print help
#
# Exit codes
# ----------
#   0  success
#   1  missing prerequisite (npm, python) or validation failure
#   2  usage error

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: build-mcpb.sh [--validate] [-h|--help]

Build a Claude Desktop .mcpb bundle for alive-mcp.

Options:
  --validate   run `mcpb validate` on the staged manifest before packing
               (recommended; catches manifest schema violations early)
  -h, --help   show this help and exit

Output:
  dist/alive-mcp-<version>.mcpb

Requirements:
  * npm (installs @anthropic-ai/mcpb globally on first run)
  * python3 (used to read the version from pyproject.toml)

The script is idempotent and safe to re-run. It wipes and re-creates
the staging dir on every invocation.
EOF
}

# --- argument parsing --------------------------------------------------------

VALIDATE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --validate) VALIDATE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

# --- resolve paths -----------------------------------------------------------
#
# Resolve ``PACKAGE_DIR`` relative to this script so the build works from
# any CWD (useful when invoked from CI, from a git worktree root, or from
# inside the alive-mcp package itself).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFEST_SRC="${PACKAGE_DIR}/mcpb/manifest.json"
DIST_DIR="${PACKAGE_DIR}/dist"
STAGING_DIR="${PACKAGE_DIR}/dist/.mcpb-staging"

if [[ ! -f "${MANIFEST_SRC}" ]]; then
    echo "error: manifest not found at ${MANIFEST_SRC}" >&2
    echo "       expected package layout: <package>/mcpb/manifest.json" >&2
    exit 1
fi

# --- dependency checks -------------------------------------------------------
#
# Only two real prereqs: `awk` (for version parsing — installed on every
# Unix by default, including every GitHub Actions runner) and `npm` (to
# install `mcpb` if it is missing). `awk` is so universal we do not check
# for it explicitly; the script would fail with a clearer shell error
# than a redundant `command -v` check anyway.

if ! command -v npm >/dev/null 2>&1; then
    echo "error: npm is required to install @anthropic-ai/mcpb" >&2
    echo "       install Node.js first (https://nodejs.org/)" >&2
    exit 1
fi

if ! command -v mcpb >/dev/null 2>&1; then
    echo "mcpb CLI not found, installing @anthropic-ai/mcpb globally..." >&2
    # Intentionally not version-pinned yet — upstream @anthropic-ai/mcpb is
    # pre-1.0 and still iterating on CLI ergonomics. When it stabilizes,
    # pin here (e.g. @anthropic-ai/mcpb@1.0.0).
    npm install -g @anthropic-ai/mcpb
    if ! command -v mcpb >/dev/null 2>&1; then
        echo "error: mcpb still not on PATH after npm install" >&2
        echo "       check your npm global prefix: npm config get prefix" >&2
        echo "       then add <prefix>/bin to PATH" >&2
        exit 1
    fi
fi

# --- read version from pyproject.toml ---------------------------------------
#
# The manifest ``version`` field and the pyproject ``version`` field must
# agree. Rather than parsing both and comparing, we read pyproject as the
# source of truth (since ``uv`` will honor it at runtime) and use it to
# name the output file.
#
# We avoid Python heredocs here for bash 3.2 compatibility: macOS ships
# bash 3.2 by default, and 3.2's parser has quirks with mixed-quote
# Python code inside ``$(<<'PY' ... PY)`` subshells. Pure bash plus awk
# is portable to every bash on any platform and needs no tomllib / no
# Python 3.11+ detection. ``pyproject.toml`` is author-controlled and
# tiny, so an awk parser is entirely adequate: extract the first
# ``version = "..."`` inside the ``[project]`` table header.
#
# Absolute path resolution: both files are referenced by the absolute
# ``PACKAGE_DIR`` / ``MANIFEST_SRC`` computed from ``BASH_SOURCE``
# above, so the script is fully CWD-independent.

extract_pyproject_version() {
    # Usage: extract_pyproject_version <path-to-pyproject.toml>
    #
    # Prints the [project] table's version field to stdout. Returns
    # non-zero if the field is missing. The awk program tracks whether
    # we are inside ``[project]`` (set on that header, cleared on any
    # other ``[...]`` header) so a ``version = "..."`` elsewhere in the
    # file (e.g., under ``[tool.poetry]``) is not accidentally matched.
    awk '
        /^\[project\][[:space:]]*$/ { in_project = 1; next }
        /^\[/                       { in_project = 0; next }
        in_project && /^[[:space:]]*version[[:space:]]*=[[:space:]]*"[^"]+"/ {
            match($0, /"[^"]+"/)
            v = substr($0, RSTART + 1, RLENGTH - 2)
            print v
            exit 0
        }
    ' "$1"
}

VERSION="$(extract_pyproject_version "${PACKAGE_DIR}/pyproject.toml")"

if [[ -z "${VERSION}" ]]; then
    echo "error: could not extract [project] version from ${PACKAGE_DIR}/pyproject.toml" >&2
    exit 1
fi

# Cross-check manifest version against pyproject version. The
# ``mcpb pack`` step will embed the manifest as-is, so if these drift
# Claude Desktop installs a bundle whose manifest claims one version
# while the code reports another. Same awk approach — no Python heredoc.
extract_manifest_version() {
    # Usage: extract_manifest_version <path-to-manifest.json>
    #
    # Prints the top-level ``"version"`` value. Prefers ``jq`` when
    # present (robust against any JSON reformatting), falls back to
    # awk (robust against everything EXCEPT the file being pretty-
    # printed with non-two-space indent). Python is NOT used so we
    # keep the build prereqs minimal — awk + jq-if-available is
    # enough, and both are on every mainstream CI runner.
    if command -v jq >/dev/null 2>&1; then
        jq -r '.version' "$1"
        return
    fi
    # awk fallback. We avoid matching a nested ``"version"`` (e.g.,
    # under ``"compatibility"``) by requiring the key at exactly
    # two-space indent — our canonical format. Reformatting the file
    # with different indent will trip this; re-run with ``jq`` on
    # PATH to bypass the constraint.
    awk '
        /^  "version"[[:space:]]*:[[:space:]]*"[^"]+"/ {
            # Strip the key portion, then extract the first quoted
            # string from the rest. This handles trailing commas,
            # trailing whitespace, and arbitrary line endings.
            sub(/^.*"version"[[:space:]]*:[[:space:]]*/, "", $0)
            match($0, /"[^"]+"/)
            if (RSTART > 0) {
                v = substr($0, RSTART + 1, RLENGTH - 2)
                print v
                exit 0
            }
        }
    ' "$1"
}

MANIFEST_VERSION="$(extract_manifest_version "${MANIFEST_SRC}")"

if [[ -z "${MANIFEST_VERSION}" ]]; then
    echo "error: could not extract top-level \"version\" from ${MANIFEST_SRC}" >&2
    echo "       the awk parser expects two-space indent on the version line" >&2
    exit 1
fi

if [[ "${MANIFEST_VERSION}" != "${VERSION}" ]]; then
    echo "error: version drift detected" >&2
    echo "       pyproject.toml version:  ${VERSION}" >&2
    echo "       mcpb/manifest.json version: ${MANIFEST_VERSION}" >&2
    echo "       bump both to the same value before building" >&2
    exit 1
fi

# --- stage files -------------------------------------------------------------
#
# We build into a staging dir instead of the package root because the
# package root has ``.venv/``, ``node_modules/``, ``tests/``,
# ``dist/`` (build output), and other artifacts that the ``.mcpbignore``
# default exclusion list does NOT cover. Staging gives us an explicit
# allowlist: exactly the files that end up in the bundle.

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"

# manifest.json MUST be at the staging root — mcpb pack discovers it there.
cp "${MANIFEST_SRC}"                 "${STAGING_DIR}/manifest.json"
cp "${PACKAGE_DIR}/pyproject.toml"   "${STAGING_DIR}/pyproject.toml"
cp "${PACKAGE_DIR}/README.md"        "${STAGING_DIR}/README.md"

# Repo-root LICENSE if alive-mcp does not carry its own copy. Monorepo
# hosts alive-mcp one directory below the repo root (claude-code/alive-mcp/
# when the worktree is at claude-code/, or alive-mcp/ directly in a split
# repo). Walk up at most two levels looking for LICENSE.
if [[ -f "${PACKAGE_DIR}/LICENSE" ]]; then
    cp "${PACKAGE_DIR}/LICENSE" "${STAGING_DIR}/LICENSE"
elif [[ -f "${PACKAGE_DIR}/../LICENSE" ]]; then
    cp "${PACKAGE_DIR}/../LICENSE" "${STAGING_DIR}/LICENSE"
elif [[ -f "${PACKAGE_DIR}/../../LICENSE" ]]; then
    cp "${PACKAGE_DIR}/../../LICENSE" "${STAGING_DIR}/LICENSE"
else
    echo "warn: no LICENSE file found; bundle will ship without one" >&2
fi

# uv.lock makes the install reproducible when present. It is committed
# alongside pyproject.toml so CI and Claude Desktop resolve the same
# dep graph.
if [[ -f "${PACKAGE_DIR}/uv.lock" ]]; then
    cp "${PACKAGE_DIR}/uv.lock" "${STAGING_DIR}/uv.lock"
fi

# Package source. The ``uv`` server type expects the entry_point path
# declared in manifest.json (``src/alive_mcp/__main__.py``) to resolve
# relative to the bundle root — hence we preserve the ``src/alive_mcp/``
# prefix exactly.
mkdir -p "${STAGING_DIR}/src"
# rsync gives us precise control: copy tree, strip __pycache__ and .pyc.
if command -v rsync >/dev/null 2>&1; then
    rsync -a \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        "${PACKAGE_DIR}/src/alive_mcp" "${STAGING_DIR}/src/"
else
    # Fallback for minimal CI images without rsync: plain cp, then prune.
    cp -R "${PACKAGE_DIR}/src/alive_mcp" "${STAGING_DIR}/src/alive_mcp"
    find "${STAGING_DIR}/src" -type d -name '__pycache__' -prune -exec rm -rf {} +
    find "${STAGING_DIR}/src" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
fi

# --- optional: validate ------------------------------------------------------

if [[ "${VALIDATE}" -eq 1 ]]; then
    echo "validating staged manifest..." >&2
    mcpb validate "${STAGING_DIR}/manifest.json"
fi

# --- pack --------------------------------------------------------------------

mkdir -p "${DIST_DIR}"
OUTPUT="${DIST_DIR}/alive-mcp-${VERSION}.mcpb"

# Remove any stale bundle first so `mcpb pack` doesn't refuse to
# overwrite (behavior varies by version).
rm -f "${OUTPUT}"

mcpb pack "${STAGING_DIR}" "${OUTPUT}"

# --- size check --------------------------------------------------------------
#
# Acceptance criterion: bundle is under 500KB. A uv-type bundle with just
# source + pyproject should weigh in around 60-120KB; 500KB is a generous
# ceiling that only trips if someone accidentally stages .venv or similar.

SIZE_BYTES="$(wc -c < "${OUTPUT}" | tr -d ' ')"
SIZE_KB=$(( SIZE_BYTES / 1024 ))
MAX_KB=500

if [[ "${SIZE_KB}" -gt "${MAX_KB}" ]]; then
    echo "error: bundle is ${SIZE_KB}KB, exceeds ${MAX_KB}KB ceiling" >&2
    echo "       inspect ${STAGING_DIR} and prune before packing again" >&2
    exit 1
fi

# --- report ------------------------------------------------------------------

echo "built ${OUTPUT} (${SIZE_KB}KB)" >&2
echo "${OUTPUT}"
