#!/bin/bash
# alive-common.sh -- shared functions for all ALIVE Context System hooks.
# Source this at the top of every hook script.
# Cross-platform: python3 (Mac/Linux) with node fallback (Windows/all).

# -- Platform detection --
ALIVE_PLATFORM="unix"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
  ALIVE_PLATFORM="windows"
fi

# -- JSON runtime detection --
# python3 preferred (fast). node guaranteed (Claude Code is a Node app).
# Windows ships a python3 Store stub (AppInstallerPythonRedirector.exe) that
# passes command -v but fails to execute (exit code 49). We validate execution,
# not just existence. The py -3 launcher is the standard Windows Python path.
ALIVE_JSON_RT=""
if command -v python3 &>/dev/null && python3 -c "" &>/dev/null 2>&1; then
  ALIVE_JSON_RT="python3"
elif command -v py &>/dev/null && py -3 -c "" &>/dev/null 2>&1; then
  # Windows py launcher: shim python3 so all existing callsites work
  python3() { py -3 "$@"; }
  export -f python3
  ALIVE_JSON_RT="python3"
elif command -v node &>/dev/null; then
  ALIVE_JSON_RT="node"
fi

# -- JSON parsing helpers --
# All JSON parsing goes through python3 or node. Never sed/regex.

# Parse multiple fields from JSON in one call.
# Usage: _json_multi "$json" "key1 key2 key3" (outputs one value per line)
_json_multi() {
  local json="$1" keys="$2"
  if [ "$ALIVE_JSON_RT" = "python3" ]; then
    printf '%s' "$json" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for k in '''$keys'''.split():
    v=d
    for p in k.split('.'):
        v=v.get(p,'') if isinstance(v,dict) else ''
    print(v if v else '')
" 2>/dev/null || echo ""
  elif [ "$ALIVE_JSON_RT" = "node" ]; then
    printf '%s' "$json" | node -e "
const d=JSON.parse(require('fs').readFileSync(0,'utf8'));
'$keys'.split(' ').forEach(k=>{
  let v=d;k.split('.').forEach(p=>{v=v&&typeof v==='object'?v[p]||'':''});
  console.log(v||'')
})" 2>/dev/null || echo ""
  else
    # No runtime -- return empty (should never happen with Claude Code)
    for _ in $keys; do echo ""; done
  fi
}

# Read JSON input from stdin. Must be called BEFORE any other stdin read.
# Sets: HOOK_INPUT, HOOK_SESSION_ID, HOOK_CWD, HOOK_EVENT
read_hook_input() {
  HOOK_INPUT=$(cat 2>/dev/null || echo '{}')
  local parsed
  parsed=$(_json_multi "$HOOK_INPUT" "session_id cwd hook_event_name")
  HOOK_SESSION_ID=$(echo "$parsed" | sed -n '1p')
  HOOK_CWD=$(echo "$parsed" | sed -n '2p')
  HOOK_EVENT=$(echo "$parsed" | sed -n '3p')
}

# SessionStart-specific fields. Call after read_hook_input.
# Sets: HOOK_MODEL, HOOK_SOURCE, HOOK_TRANSCRIPT
read_session_fields() {
  local parsed
  parsed=$(_json_multi "$HOOK_INPUT" "model source transcript_path")
  HOOK_MODEL=$(echo "$parsed" | sed -n '1p')
  : "${HOOK_MODEL:=unknown}"
  HOOK_SOURCE=$(echo "$parsed" | sed -n '2p')
  HOOK_TRANSCRIPT=$(echo "$parsed" | sed -n '3p')
}

# Extract a single JSON field (flat or nested dot-path).
# Usage: json_field "tool_name" or json_field "tool_input.file_path"
json_field() {
  _json_multi "$HOOK_INPUT" "$1" | head -1
}

# PreToolUse-specific fields. Call after read_hook_input.
# Sets: HOOK_TOOL_NAME, HOOK_TOOL_INPUT
read_tool_fields() {
  HOOK_TOOL_NAME=$(json_field "tool_name")
  HOOK_TOOL_INPUT="$HOOK_INPUT"
}

# Migrate legacy paths if needed.
# Called after WORLD_ROOT is set. Returns 0 if migration happened, 1 if not needed.
migrate_legacy_to_alive() {
  local world_root="$1"
  local old_walnut_dir="$world_root/.walnut"
  local new_dir="$world_root/.alive"

  # Check for legacy .walnut/ dir
  if [ -d "$old_walnut_dir" ] && [ ! -d "$new_dir" ]; then
    mv "$old_walnut_dir" "$new_dir" 2>/dev/null || return 1
    export ALIVE_MIGRATED_FROM="walnut"
    return 0
  fi

  # Both exist -- flag for manual resolution
  if [ -d "$old_walnut_dir" ] && [ -d "$new_dir" ]; then
    export ALIVE_MIGRATION_CONFLICT="both_exist"
    return 1
  fi

  return 1
}

# Find the world root.
# Tier order (locked, fn-15-la5.4):
#   1. env override   ($ALIVE_WORLD_ROOT_OVERRIDE) -- fail loud if invalid
#   2. config file    (~/.config/alive/world-root, with legacy walnut
#                      migration) -- fail loud on stale config; fall
#                      through ONLY when both files are absent
#   3. bootstrap discovery -- cwd walk-up FIRST, cowork mount-scan
#                      SECOND (gated on CLAUDE_CODE_IS_COWORK=1).
#                      Each candidate must pass is_valid_world_root
#                      AND validate_path_choice with decision=allow.
#                      confirm_required (e.g. $HOME, iCloud, Dropbox,
#                      Google Drive) and deny candidates are
#                      REJECTED for implicit bootstrap.
#   4. fail silently -- return 1; the noisy find_world_or_die wrapper
#                      from T6 is what callers use for fail-loud.
#
# On failure, sets WORLD_ROOT_FAIL_REASON to one of:
#   not_found        -- no candidate matched any tier
#   stale_config     -- tier-2 file present but world is gone (unmounted
#                      volume, missing dir, missing marker)
#   invalid_override -- tier-1 env override set but does not validate
#   denied_home      -- tier-3 found a confirm_required:home candidate
#                      that we refused to auto-anchor on
# WORLD_ROOT_ADVISORY_REASON is a SEPARATE channel set when legacy-
# walnut migration write fails; the resolver still returns the legacy
# path successfully in that case.
#
# `$ALIVE_WORLD_ROOT` is NOT consumed (was tier 3 in the legacy resolver).
# It continues being WRITTEN by alive-session-new.sh for downstream tools
# but is no longer read here -- doctor (T7) surfaces a one-time hint.
#
# All paths are handled lexically -- no realpath, no readlink -f.
# T1's per-OS unmount detection runs inside is_valid_world_root /
# validate_world_root before any path-following stat.
#
# fn-25: Surface-gated cwd-vs-config divergence detector.
#
# Called from find_world AFTER a successful tier-2 (config-file) resolve.
# When the user is standing in a valid world that differs from the world
# resolved from the config file, sets:
#   WORLD_ROOT_ADVISORY_REASON=cwd_config_divergence
#   WORLD_ROOT_DIVERGENT_CWD_PATH=<cwd-walked-up world>
# Resolver still returns the config-resolved world unchanged (config wins;
# advisory is informational).
#
# Surface gate (locked):
#   * Runs only when CLAUDE_CODE_HOOK_EVENT=SessionStart OR
#     ALIVE_RESOLVER_DIVERGENCE_CHECK=1.
#   * Default off. SessionResume / non-CC surfaces (alive-mcp, Hermes,
#     Codex) pay zero overhead and never see the advisory.
#
# Skip-conditions inside the detector itself:
#   * cwd walk-up finds nothing valid -> no advisory (the canonical
#     "user opens Claude in ~/Downloads" case).
#   * cwd-resolved == config-resolved -> no advisory (everything aligns).
#   * cwd-resolved is validate_path_choice=deny -> no advisory (the user
#     accidentally cd'd into /private/var/folders/.alive or similar; the
#     doctor --fix path would refuse the write anyway, so don't waste a
#     SessionStart prompt on it).
#   * cwd-resolved is confirm_required (home or cloud) -> STILL fires the
#     advisory; the user can decide via doctor with --allow-home /
#     --allow-cloud at fix time.
#
# Args: $1 = config-resolved world root (the value find_world is about to
#            return on the success path).
# Returns: always 0 (advisory is best-effort; never fail the resolve).
_alive_detect_cwd_config_divergence() {
  local config_world="$1"

  # Surface gate. SessionResume is intentionally NOT in the SessionStart
  # branch (resume is mid-flight; divergence has no actionable meaning).
  case "${CLAUDE_CODE_HOOK_EVENT:-}" in
    SessionStart) ;;
    *)
      if [ "${ALIVE_RESOLVER_DIVERGENCE_CHECK:-}" != "1" ]; then
        return 0
      fi
      ;;
  esac

  # Walk up from cwd looking for a valid world. Mirrors tier-3a's lexical
  # normalization + predicate but does NOT honor cowork mount-scan
  # (cowork worlds are launcher-picked, not user-cwd-picked).
  local dir="${HOOK_CWD:-${CLAUDE_PROJECT_DIR:-$PWD}}"
  local check_raw="$dir"
  local cwd_world=""
  local cwd_decision=""
  while [ -n "$check_raw" ] && [ "$check_raw" != "/" ]; do
    local check_norm
    if check_norm="$(lexical_normalize_path "$check_raw" 2>/dev/null)" \
        && [ -n "$check_norm" ] \
        && is_valid_world_root "$check_norm"; then
      local choice decision
      choice="$(validate_path_choice "$check_norm")"
      decision="${choice%%	*}"
      cwd_world="$check_norm"
      cwd_decision="$decision"
      break
    fi
    local parent
    parent="$(dirname -- "$check_raw")"
    if [ "$parent" = "$check_raw" ]; then
      break
    fi
    check_raw="$parent"
  done

  # No valid world up the cwd chain -> nothing to flag.
  [ -z "$cwd_world" ] && return 0

  # Skip deny: validate_path_choice already refuses to anchor here, so
  # surfacing a "switch to it" prompt would lead the user nowhere.
  [ "$cwd_decision" = "deny" ] && return 0

  # Same world -> no divergence (config IS the cwd-resolved world).
  if [ "$cwd_world" = "$config_world" ]; then
    return 0
  fi

  # Real divergence: surface it via the advisory channel. Resolver still
  # returns the config-resolved world (caller-side WORLD_ROOT unchanged).
  export WORLD_ROOT_ADVISORY_REASON="cwd_config_divergence"
  export WORLD_ROOT_DIVERGENT_CWD_PATH="$cwd_world"
  return 0
}

# Sets: WORLD_ROOT on success; returns 1 on fail (with reason as above).
find_world() {
  # Clear any prior resolver state on the in-process surface. WORLD_ROOT
  # in particular must be wiped at function entry so a caller that
  # ignores our return code cannot observe a stale value from a previous
  # successful call as if it belonged to the current cwd. The advisory
  # channel is cleared too so a prior migration_write_failed cannot
  # leak into a later successful resolve within the same process. The
  # stale-config diagnostic exports (WORLD_ROOT_STALE_PATH /
  # WORLD_ROOT_STALE_STATUS) are also cleared so T6's fail-loud wrapper
  # never reads a value left over from a previous call.
  unset WORLD_ROOT WORLD_ROOT_FAIL_REASON WORLD_ROOT_ADVISORY_REASON
  unset WORLD_ROOT_STALE_PATH WORLD_ROOT_STALE_STATUS
  # fn-25: divergence-detection state (post-tier-2 cwd-vs-config check).
  # Cleared on entry alongside fail/advisory state so a prior call's
  # divergent-cwd export cannot leak into a later resolve in the same
  # process. Population is gated; absence here is the normal case.
  unset WORLD_ROOT_DIVERGENT_CWD_PATH

  # ---- Tier 1: env override ----
  if [ -n "${ALIVE_WORLD_ROOT_OVERRIDE:-}" ]; then
    local override_norm override_status
    if override_norm="$(lexical_normalize_path "$ALIVE_WORLD_ROOT_OVERRIDE" 2>/dev/null)"; then
      override_status="$(validate_world_root "$override_norm")"
      if [ "$override_status" = "ok" ]; then
        WORLD_ROOT="$override_norm"
        migrate_legacy_to_alive "$WORLD_ROOT" || true
        return 0
      fi
    fi
    # Set-but-invalid override fails loud rather than falling through:
    # an explicit override should never silently get downgraded.
    export WORLD_ROOT_FAIL_REASON="invalid_override"
    return 1
  fi

  # ---- Tier 2: config file ----
  # _alive_parse_persisted_world_root_file return codes (locked):
  #   0 -- file present, parseable; path printed on stdout
  #   1 -- file absent (caller may fall through to next source)
  #   2 -- file present but content is corrupt / unparseable
  #        (empty after trim, multi-line, non-absolute, etc.)
  # rc=2 must NOT be treated as "absent" -- spec says tier 2 falls
  # through to bootstrap ONLY when files are absent. Corrupt content
  # is a stale_config fail-loud condition; silent migration to a
  # different world (or implicit re-bootstrap onto a sibling) would
  # reintroduce the failure mode this resolver is built to prevent.
  local alive_norm alive_rc
  alive_norm="$(_alive_parse_persisted_world_root_file "$ALIVE_CONFIG_FILE" 2>/dev/null)"
  alive_rc=$?
  if [ "$alive_rc" -eq 0 ]; then
    local alive_status
    alive_status="$(validate_world_root "$alive_norm")"
    if [ "$alive_status" = "ok" ]; then
      WORLD_ROOT="$alive_norm"
      migrate_legacy_to_alive "$WORLD_ROOT" || true
      # fn-25: surface-gated cwd-vs-config divergence advisory.
      _alive_detect_cwd_config_divergence "$WORLD_ROOT"
      return 0
    fi
    # File present but world is gone -- fail loud rather than silently
    # bootstrapping onto a different world. Surface the offending path
    # and the specific status (unmounted_volume / missing_dir /
    # missing_marker) so T6's fail-loud wrapper can produce a
    # ``alive doctor --fix --world-root <path>`` hint without re-
    # parsing the file.
    export WORLD_ROOT_FAIL_REASON="stale_config"
    export WORLD_ROOT_STALE_PATH="$alive_norm"
    export WORLD_ROOT_STALE_STATUS="$alive_status"
    return 1
  fi
  if [ "$alive_rc" -eq 2 ]; then
    # File present but corrupt content: same fail-loud semantics as
    # stale-but-validating-format would have produced. We have no
    # parseable path to surface, so STALE_PATH points at the config
    # file itself (which is what the user must fix or remove).
    export WORLD_ROOT_FAIL_REASON="stale_config"
    export WORLD_ROOT_STALE_PATH="$ALIVE_CONFIG_FILE"
    export WORLD_ROOT_STALE_STATUS="corrupt_config_file"
    return 1
  fi

  # Alive config absent: try legacy walnut path with one-time migration.
  local walnut_norm walnut_rc
  walnut_norm="$(_alive_parse_persisted_world_root_file "$LEGACY_WALNUT_CONFIG_FILE" 2>/dev/null)"
  walnut_rc=$?
  if [ "$walnut_rc" -eq 0 ]; then
    local walnut_status
    walnut_status="$(validate_world_root "$walnut_norm")"
    if [ "$walnut_status" = "ok" ]; then
      # Migration only fires on a VALIDATING legacy path -- writing a
      # known-bad path into the alive config would brick all future
      # resolutions (every later boot would hard-fail tier 2 with
      # stale_config). Migration write failure surfaces via
      # WORLD_ROOT_ADVISORY_REASON (separate channel) and does NOT
      # block the successful resolve.
      if ! write_world_root_file "$walnut_norm" 2>/dev/null; then
        export WORLD_ROOT_ADVISORY_REASON="migration_write_failed"
      fi
      WORLD_ROOT="$walnut_norm"
      migrate_legacy_to_alive "$WORLD_ROOT" || true
      # fn-25: surface-gated cwd-vs-config divergence advisory. Skipped
      # when migration_write_failed is already set so the advisory channel
      # surfaces the migration failure (the more actionable signal) rather
      # than overwriting it with a divergence prompt the user cannot heal
      # via --fix until the alive config dir is writable again.
      if [ -z "${WORLD_ROOT_ADVISORY_REASON:-}" ]; then
        _alive_detect_cwd_config_divergence "$WORLD_ROOT"
      fi
      return 0
    fi
    # Legacy file present but stale: fail loud, do NOT migrate the
    # bad path into the alive config, do NOT fall through to tier 3.
    export WORLD_ROOT_FAIL_REASON="stale_config"
    export WORLD_ROOT_STALE_PATH="$walnut_norm"
    export WORLD_ROOT_STALE_STATUS="$walnut_status"
    return 1
  fi
  if [ "$walnut_rc" -eq 2 ]; then
    # Legacy file present but corrupt: same fail-loud semantics. Point
    # at the legacy file rather than a path we couldn't extract.
    export WORLD_ROOT_FAIL_REASON="stale_config"
    export WORLD_ROOT_STALE_PATH="$LEGACY_WALNUT_CONFIG_FILE"
    export WORLD_ROOT_STALE_STATUS="corrupt_config_file"
    return 1
  fi

  # ---- Tier 3: bootstrap discovery ----
  # Internal order locked: cwd walk-up FIRST (cheap, common case),
  # cowork mount-scan SECOND. Both gated through validate_path_choice
  # so that $HOME / iCloud / Dropbox / Google Drive candidates are
  # REJECTED for implicit bootstrap -- they must be pinned explicitly
  # via setup, doctor --fix --allow-home, or override env.
  local saw_home_confirm_required=""

  # Tier 3a: cwd walk-up via canonical predicate. Lexically normalize
  # FIRST so the predicate, the path-choice policy, and the value we
  # eventually return all see the same path. is_valid_world_root will
  # re-normalize internally (idempotent) but running it on raw input
  # would let a path that fails normalization slip past.
  local dir="${HOOK_CWD:-${CLAUDE_PROJECT_DIR:-$PWD}}"
  local check_raw="$dir"
  while [ -n "$check_raw" ] && [ "$check_raw" != "/" ]; do
    local check_norm
    if check_norm="$(lexical_normalize_path "$check_raw" 2>/dev/null)" \
        && [ -n "$check_norm" ] \
        && is_valid_world_root "$check_norm"; then
      local choice decision rest category
      choice="$(validate_path_choice "$check_norm")"
      decision="${choice%%	*}"
      rest="${choice#*	}"
      category="${rest%%	*}"
      if [ "$decision" = "allow" ]; then
        WORLD_ROOT="$check_norm"
        migrate_legacy_to_alive "$WORLD_ROOT" || true
        return 0
      fi
      if [ "$decision" = "confirm_required" ] && [ "$category" = "home" ]; then
        saw_home_confirm_required="1"
      fi
    fi
    local parent
    # Use ``dirname --`` for consistency with other call sites and
    # defensive safety against malformed HOOK_CWD that starts with '-'.
    parent="$(dirname -- "$check_raw")"
    if [ "$parent" = "$check_raw" ]; then
      break
    fi
    check_raw="$parent"
  done

  # Tier 3b: Cowork mount-scan -- user folder is mounted under
  # $HOME/mnt/<name>/. Only fires when CLAUDE_CODE_IS_COWORK=1.
  if [ "${CLAUDE_CODE_IS_COWORK:-}" = "1" ]; then
    local mnt_dir="${HOME:-$dir}/mnt"
    if [ -d "$mnt_dir" ]; then
      local candidate trimmed cw_norm cw_choice cw_decision cw_rest cw_category
      for candidate in "$mnt_dir"/*/; do
        trimmed="${candidate%/}"
        # Without ``nullglob`` set, an empty $mnt_dir iterates once with
        # the literal glob (".../mnt/*"). Guard explicitly so we don't
        # waste a normalize+predicate pass on a non-directory path.
        [ -d "$trimmed" ] || continue
        cw_norm="$(lexical_normalize_path "$trimmed" 2>/dev/null)" || continue
        [ -z "$cw_norm" ] && continue
        if is_valid_world_root "$cw_norm"; then
          cw_choice="$(validate_path_choice "$cw_norm")"
          cw_decision="${cw_choice%%	*}"
          cw_rest="${cw_choice#*	}"
          cw_category="${cw_rest%%	*}"
          if [ "$cw_decision" = "allow" ]; then
            WORLD_ROOT="$cw_norm"
            migrate_legacy_to_alive "$WORLD_ROOT" || true
            return 0
          fi
          if [ "$cw_decision" = "confirm_required" ] && [ "$cw_category" = "home" ]; then
            saw_home_confirm_required="1"
          fi
        fi
      done
    fi
  fi

  # ---- Tier 4: fail ----
  if [ -n "$saw_home_confirm_required" ]; then
    export WORLD_ROOT_FAIL_REASON="denied_home"
  else
    export WORLD_ROOT_FAIL_REASON="not_found"
  fi
  return 1
}

# Escape string for JSON embedding.
# Large strings (>1KB) go through python3/node for proper Unicode handling.
# Small strings use bash (fast, ASCII-safe).
escape_for_json() {
  if [ ${#1} -gt 1000 ]; then
    if [ "$ALIVE_JSON_RT" = "python3" ]; then
      printf '%s' "$1" | python3 -c "import sys,json; sys.stdout.buffer.write(json.dumps(sys.stdin.buffer.read().decode('utf-8','replace'))[1:-1].encode('utf-8'))"
    elif [ "$ALIVE_JSON_RT" = "node" ]; then
      printf '%s' "$1" | node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(d).slice(1,-1)))"
    else
      # Fallback: bash escaping (correct but slow for large strings)
      local s="$1"
      s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\n'/\\n}"; s="${s//$'\r'/\\r}"; s="${s//$'\t'/\\t}"
      printf '%s' "$s"
    fi
  else
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
  fi
}

# ---------------------------------------------------------------------------
# fn-15-la5.1: world-root predicate, validation, and config-file I/O.
# Bash sibling of scripts/_world_root_io.py. Same I/O contract; same
# predicate; same WorldRootStatus taxonomy. Sourced into hook scripts.
# ---------------------------------------------------------------------------

# Domain-dir constants. Two distinct arrays keep "world-root predicate"
# and "walnut scan" usages from getting conflated.
WORLD_ROOT_DOMAIN_DIRS=(01_Archive 02_Life 03_Inbox 04_Ventures 05_Experiments)
WALNUT_SCAN_DOMAIN_DIRS=(01_Archive 02_Life 04_Ventures 05_Experiments)

ALIVE_CONFIG_FILE="${HOME}/.config/alive/world-root"
LEGACY_WALNUT_CONFIG_FILE="${HOME}/.config/walnut/world-root"

# Module-level mount-point cache. Populated once per hook process.
_ALIVE_MOUNT_POINTS_LOADED=""
_ALIVE_MOUNT_POINTS=()
# Linux autofs / unresponsive-fuse roots (parallel to Python's
# _LINUX_AUTOFS_ROOTS_CACHE); populated only when sysname=Linux.
_ALIVE_LINUX_AUTOFS_ROOTS=()
# Sentinel: set to "1" when mount-data load failed or produced
# zero rows. _alive_is_under_unmounted_volume must skip detection
# in that case (per the "best-effort, never blocking" policy --
# false-positive rejects brick valid worlds; false-negatives only
# re-expose the existing hang risk).
_ALIVE_MOUNT_PARSE_FAILED=""

# lexical_normalize_path <path>
# Lexically normalizes a path. NEVER calls realpath/cd; pure string ops.
# Steps (locked):
#   1) reject empty
#   2) strip a single trailing '/' (unless path is '/')
#   3) tilde expansion (~/ -> $HOME/, ~ alone -> $HOME, ~user rejected)
#   4) reject relative paths
#   5) collapse '//' runs to single '/'
#   6) collapse '/./' to '/'
#   7) resolve '/../' lexically; reject ascend-past-root
# Echoes normalized path to stdout. Returns 1 on rejection.
lexical_normalize_path() {
  local p="$1"
  [ -z "$p" ] && return 1

  # Reject paths containing tab / newline / CR. These bytes would
  # break every text-line / TSV / hook-JSON surface this module
  # emits over; hard-rejecting at the normalization boundary keeps
  # every downstream surface safe by construction. Python sibling
  # rejects in lock-step.
  case "$p" in
    *$'\t'*|*$'\n'*|*$'\r'*) return 1 ;;
  esac

  # ~user (any non-/ char after ~) is unsupported -- match Python.
  if [ "${p:0:1}" = "~" ]; then
    if [ "$p" = "~" ]; then
      p="$HOME"
    elif [ "${p:0:2}" = "~/" ]; then
      p="$HOME/${p:2}"
    else
      return 1
    fi
  fi

  # Reject relative paths.
  [ "${p:0:1}" != "/" ] && return 1

  # Strip exactly one trailing '/' if path is not just '/'.
  if [ "$p" != "/" ] && [ "${p: -1}" = "/" ]; then
    p="${p%/}"
  fi

  # Collapse '//' runs.
  while [[ "$p" == *"//"* ]]; do
    p="${p//\/\///}"
  done

  # Collapse '/./' segments.
  while [[ "$p" == *"/./"* ]]; do
    p="${p//\/.\///}"
  done
  # Trailing '/.' (after a non-root prefix) becomes the prefix.
  if [[ "$p" == */. ]]; then
    p="${p%/.}"
    [ -z "$p" ] && p="/"
  fi

  # Resolve '..' lexically by walking segments. Use ``set -f`` +
  # ``IFS=/`` word-splitting on an unquoted expansion rather than a
  # here-string for this single-line splitter -- here-strings allocate
  # a tempfile under ``$TMPDIR`` per invocation, which is wasteful for
  # the hottest path on every hook firing AND historically a
  # reliability hazard under restrictive TMPDIR configs. Other helpers
  # in this file (mount-line / config-content readers below) use
  # here-strings deliberately because the readability win on a
  # multi-line input loop outweighs the per-call cost; the choice is
  # localized rather than a blanket prohibition.
  local parts=()
  local _alive_old_ifs="$IFS"
  set -f
  IFS=/
  # shellcheck disable=SC2206  # intentional unquoted expansion for split
  parts=( ${p#/} )
  set +f
  IFS="$_alive_old_ifs"
  local out=()
  local seg
  for seg in "${parts[@]}"; do
    if [ -z "$seg" ] || [ "$seg" = "." ]; then
      continue
    fi
    if [ "$seg" = ".." ]; then
      if [ ${#out[@]} -eq 0 ]; then
        return 1  # ascend past root
      fi
      unset 'out[${#out[@]}-1]'
      out=("${out[@]}")  # re-pack
      continue
    fi
    out+=("$seg")
  done

  if [ ${#out[@]} -eq 0 ]; then
    printf '/\n'
    return 0
  fi
  # Build the result with explicit ``printf`` calls -- never ``echo``
  # because segments may legitimately start with ``-`` and ``echo``
  # would interpret them as flags.
  local result=""
  local part
  for part in "${out[@]}"; do
    result="${result}/${part}"
  done
  printf '%s\n' "$result"
  return 0
}

# _alive_decode_fstab_octal <string>
# Decode the four fstab-style escapes (\040 space, \011 tab, \012 nl,
# \134 backslash). Idempotent on strings without backslashes.
_alive_decode_fstab_octal() {
  local s="$1"
  case "$s" in
    *\\*) ;;
    *) printf '%s' "$s"; return 0 ;;
  esac
  s="${s//\\040/ }"
  s="${s//\\011/$'\t'}"
  s="${s//\\012/$'\n'}"
  s="${s//\\134/\\}"
  printf '%s' "$s"
}

# _alive_load_mount_points
# Populate _ALIVE_MOUNT_POINTS once per process. Honors the
# ALIVE_MOUNT_OUTPUT_FIXTURE / ALIVE_PROC_MOUNTS_FIXTURE env vars for
# hermetic tests. Best-effort: parse failures leave the cache empty
# and the predicate biases toward "let it through".
_alive_load_mount_points() {
  if [ -n "$_ALIVE_MOUNT_POINTS_LOADED" ]; then
    return 0
  fi
  _ALIVE_MOUNT_POINTS_LOADED="1"
  _ALIVE_MOUNT_POINTS=()
  _ALIVE_LINUX_AUTOFS_ROOTS=()
  _ALIVE_MOUNT_PARSE_FAILED=""

  local sysname
  sysname="$(uname -s 2>/dev/null || echo unknown)"

  case "$sysname" in
    Darwin)
      local raw
      if [ -n "${ALIVE_MOUNT_OUTPUT_FIXTURE:-}" ] && [ -f "$ALIVE_MOUNT_OUTPUT_FIXTURE" ]; then
        raw="$(cat "$ALIVE_MOUNT_OUTPUT_FIXTURE" 2>/dev/null)"
      else
        raw="$(mount 2>/dev/null)"
      fi
      if [ -z "$raw" ]; then
        # Empty mount output -- treat as parse failure; without this
        # every /Volumes/<name> would be flagged as unmounted.
        _ALIVE_MOUNT_PARSE_FAILED="1"
        return 0
      fi
      local line on_idx paren_idx mp
      while IFS= read -r line; do
        [ -z "$line" ] && continue
        # Find ' on ' (left boundary) and ' (' (right boundary, last).
        if [[ "$line" != *" on "* ]] || [[ "$line" != *" ("* ]]; then
          continue
        fi
        local left="${line%% on *}"
        local rest="${line#${left} on }"
        # rest is '<mountpoint> (flags)'. Strip from last ' (' onward.
        local mp_raw="${rest% (*}"
        if [ "$mp_raw" = "$rest" ]; then
          continue
        fi
        mp="$(_alive_decode_fstab_octal "$mp_raw")"
        [ -z "$mp" ] && continue
        _ALIVE_MOUNT_POINTS+=("$mp")
      done <<< "$raw"
      ;;
    Linux)
      local raw line
      if [ -n "${ALIVE_PROC_MOUNTS_FIXTURE:-}" ] && [ -f "$ALIVE_PROC_MOUNTS_FIXTURE" ]; then
        raw="$(cat "$ALIVE_PROC_MOUNTS_FIXTURE" 2>/dev/null)"
      elif [ -r /proc/mounts ]; then
        raw="$(cat /proc/mounts 2>/dev/null)"
      else
        _ALIVE_MOUNT_PARSE_FAILED="1"
        return 0
      fi
      if [ -z "$raw" ]; then
        _ALIVE_MOUNT_PARSE_FAILED="1"
        return 0
      fi
      while IFS= read -r line; do
        [ -z "$line" ] && continue
        # shellcheck disable=SC2206
        local cols=( $line )
        [ ${#cols[@]} -lt 3 ] && continue
        local mp fstype
        mp="$(_alive_decode_fstab_octal "${cols[1]}")"
        fstype="${cols[2]}"
        _ALIVE_MOUNT_POINTS+=("$mp")
        if [ "$fstype" = "autofs" ]; then
          _ALIVE_LINUX_AUTOFS_ROOTS+=("$mp")
        else
          case "$fstype" in
            fuse.*unresponsive*|fuse.*UNRESPONSIVE*)
              _ALIVE_LINUX_AUTOFS_ROOTS+=("$mp")
              ;;
          esac
        fi
      done <<< "$raw"
      ;;
    *)
      # Windows / unknown: skip detection.
      return 0
      ;;
  esac

  # Final guard: if parsing yielded zero mountpoints (corrupted
  # output, unexpected line shapes), bias toward "skip detection"
  # rather than "reject every /Volumes/<name>".
  if [ ${#_ALIVE_MOUNT_POINTS[@]} -eq 0 ]; then
    _ALIVE_MOUNT_PARSE_FAILED="1"
  fi
}

# _alive_is_under_unmounted_volume <path>
# Returns 0 (true) iff the path lives under an unmounted volume per
# the platform's hang-class rules. Pure string op against the cached
# mount list -- never stats the path.
#
# macOS: any path under /Volumes/<name> where <name> is NOT in mount.
# Linux: any path under an autofs / unresponsive-fuse root where the
#        first sub-segment is NOT in mount. Mirrors the Python
#        sibling.
_alive_is_under_unmounted_volume() {
  local p="$1"
  local sysname
  sysname="$(uname -s 2>/dev/null || echo unknown)"
  _alive_load_mount_points
  # Best-effort policy: if mount data was unavailable / unparseable,
  # skip detection. False-negatives (failing to detect a real
  # unmount) only re-expose the existing hang risk; false-positives
  # would brick valid worlds.
  if [ -n "$_ALIVE_MOUNT_PARSE_FAILED" ]; then
    return 1
  fi
  case "$sysname" in
    Darwin)
      case "$p" in
        /Volumes/*) ;;
        *) return 1 ;;
      esac
      local rest="${p#/Volumes/}"
      local volume_name="${rest%%/*}"
      [ -z "$volume_name" ] && return 1
      local candidate="/Volumes/$volume_name"
      local mp
      for mp in "${_ALIVE_MOUNT_POINTS[@]}"; do
        if [ "$mp" = "$candidate" ]; then
          return 1  # mounted
        fi
      done
      return 0  # not in mount list
      ;;
    Linux)
      local root rest sub_name candidate mp
      for root in "${_ALIVE_LINUX_AUTOFS_ROOTS[@]}"; do
        [ -z "$root" ] && continue
        if [ "$p" = "$root" ]; then
          return 1
        fi
        case "$p" in
          "$root"/*) ;;
          *) continue ;;
        esac
        rest="${p#${root}/}"
        sub_name="${rest%%/*}"
        [ -z "$sub_name" ] && return 1
        candidate="${root}/${sub_name}"
        local matched=1
        for mp in "${_ALIVE_MOUNT_POINTS[@]}"; do
          if [ "$mp" = "$candidate" ]; then
            matched=0
            break
          fi
        done
        if [ "$matched" -ne 0 ]; then
          return 0
        fi
      done
      return 1
      ;;
    *)
      return 1
      ;;
  esac
}

# _alive_resolve_symlink_target <path>
# Echo the lexically-normalized symlink target if the path is a
# symlink; echo nothing and return 1 otherwise.
_alive_resolve_symlink_target() {
  local p="$1"
  if [ ! -L "$p" ]; then
    return 1
  fi
  local target
  target="$(readlink -- "$p" 2>/dev/null)" || return 1
  if [ "${target:0:1}" != "/" ]; then
    target="$(dirname -- "$p")/$target"
  fi
  lexical_normalize_path "$target" 2>/dev/null
}

# _alive_child_present <child>
# Per-child probe used by the predicate. Mirrors the Python
# _child_is_present_dir contract.
_alive_child_present() {
  local child="$1"
  if [ -L "$child" ]; then
    local target
    target="$(_alive_resolve_symlink_target "$child")" || return 1
    if _alive_is_under_unmounted_volume "$target"; then
      return 1
    fi
  fi
  [ -d "$child" ]
}

# is_valid_world_root <path>
# Canonical predicate. Returns 0 (true) iff path is a directory and
# either contains .alive/ or has >=2 of WORLD_ROOT_DOMAIN_DIRS as
# direct children. Each child probe is symlink-safe + mount-aware.
is_valid_world_root() {
  local raw="$1"
  local p
  p="$(lexical_normalize_path "$raw" 2>/dev/null)" || return 1
  [ -z "$p" ] && return 1

  if _alive_is_under_unmounted_volume "$p"; then
    return 1
  fi
  if [ -L "$p" ]; then
    local target
    target="$(_alive_resolve_symlink_target "$p")" || true
    if [ -n "$target" ] && _alive_is_under_unmounted_volume "$target"; then
      return 1
    fi
  fi
  [ -d "$p" ] || return 1

  if _alive_child_present "$p/.alive"; then
    return 0
  fi

  local count=0
  local d
  for d in "${WORLD_ROOT_DOMAIN_DIRS[@]}"; do
    if _alive_child_present "$p/$d"; then
      count=$((count + 1))
      if [ "$count" -ge 2 ]; then
        return 0
      fi
    fi
  done
  return 1
}

# validate_world_root <path>
# Echo the WorldRootStatus value (ok | unmounted_volume | missing_dir |
# missing_marker). Returns 0 always; status is on stdout.
validate_world_root() {
  local raw="$1"
  local p
  if ! p="$(lexical_normalize_path "$raw" 2>/dev/null)"; then
    printf '%s\n' "missing_dir"
    return 0
  fi
  if _alive_is_under_unmounted_volume "$p"; then
    printf '%s\n' "unmounted_volume"
    return 0
  fi
  if [ -L "$p" ]; then
    local target
    target="$(_alive_resolve_symlink_target "$p")" || true
    if [ -n "$target" ] && _alive_is_under_unmounted_volume "$target"; then
      printf '%s\n' "unmounted_volume"
      return 0
    fi
  fi
  if [ ! -d "$p" ]; then
    printf '%s\n' "missing_dir"
    return 0
  fi
  if _alive_child_present "$p/.alive"; then
    printf '%s\n' "ok"
    return 0
  fi
  local count=0 d
  for d in "${WORLD_ROOT_DOMAIN_DIRS[@]}"; do
    if _alive_child_present "$p/$d"; then
      count=$((count + 1))
      if [ "$count" -ge 2 ]; then
        printf '%s\n' "ok"
        return 0
      fi
    fi
  done
  printf '%s\n' "missing_marker"
  return 0
}

# _alive_parse_persisted_world_root_file <path>
# Read a config file, validate format (>=1 non-empty line is rejected
# as multi-line corruption matching Python), trim leading/trailing
# whitespace, and echo the lexically-normalized path to stdout.
# Returns:
#   0 -- file present, parseable; normalized path on stdout
#   1 -- file ABSENT (caller may fall through to next source)
#   2 -- file present but content is corrupt / unparseable / unreadable
#        (empty after trim, multi-line, non-absolute, tab/CR/newline
#         in path, ascend-past-root, OR transient I/O failure on a
#         file that exists)
# rc=1 must be reserved for "file truly absent" so find_world's
# "tier 2 falls through ONLY when file is absent" guarantee holds.
# An unreadable-but-present file is a stale_config fail-loud condition
# (callers that fell through here would silently re-bootstrap onto a
# different world the next time the file became readable).
_alive_parse_persisted_world_root_file() {
  local file="$1"
  [ -f "$file" ] || return 1

  local content
  if ! content="$(cat "$file" 2>/dev/null)"; then
    # File existed at the -f check but cat failed (permission denied,
    # transient I/O, etc.). Re-check -f to disambiguate true delete
    # races (treat as absent) from genuinely-unreadable files (treat
    # as corrupt -- caller must fail loud).
    if [ -f "$file" ]; then
      return 2
    fi
    return 1
  fi

  # Strip leading whitespace.
  local leading="${content%%[![:space:]]*}"
  content="${content#"$leading"}"
  # Strip trailing whitespace.
  local trailing="${content##*[![:space:]]}"
  content="${content%"$trailing"}"

  if [ -z "$content" ]; then
    return 2  # empty after strip
  fi

  # Reject more than one non-empty line.
  local non_empty_count=0
  local single=""
  local line
  while IFS= read -r line; do
    local stripped="$line"
    local lead2="${stripped%%[![:space:]]*}"
    stripped="${stripped#"$lead2"}"
    local trail2="${stripped##*[![:space:]]}"
    stripped="${stripped%"$trail2"}"
    if [ -n "$stripped" ]; then
      non_empty_count=$((non_empty_count + 1))
      if [ "$non_empty_count" -eq 1 ]; then
        single="$line"
      fi
    fi
  done <<< "$content"

  if [ "$non_empty_count" -ne 1 ]; then
    return 2
  fi

  # Final trim of the single-line candidate.
  local lead3="${single%%[![:space:]]*}"
  single="${single#"$lead3"}"
  local trail3="${single##*[![:space:]]}"
  single="${single%"$trail3"}"

  local norm
  norm="$(lexical_normalize_path "$single" 2>/dev/null)" || return 2
  printf '%s\n' "$norm"
  return 0
}

# read_world_root_file
# Tier-2 read with legacy migration. Echoes the path on success;
# returns 1 on missing/invalid (so the caller can fall through). On a
# VALIDATING legacy path attempts migration via write_world_root_file;
# on write failure sets WORLD_ROOT_ADVISORY_REASON=migration_write_failed
# and still echoes the legacy path. Migration NEVER fires on a stale
# legacy path -- writing a known-bad path into the alive config would
# brick all future resolutions (every later boot would hard-fail
# tier-2 with stale_config). The advisory channel is separate from
# WORLD_ROOT_FAIL_REASON (the resolver-failure taxonomy set by
# find_world); a successful resolve via legacy migration must never
# stomp the fail-reason channel.
#
# Note: this helper is the public surface for callers that just want
# "give me the configured world root or nothing" (e.g. the setup
# skill's P1 happy-path). find_world() does NOT use this helper -- it
# needs to distinguish absent / corrupt / stale to drive its own
# fail-reason taxonomy, so it works directly against the lower-level
# parser. Both code paths must share the locked migration semantics:
# only migrate on a validating legacy.
read_world_root_file() {
  unset WORLD_ROOT_ADVISORY_REASON

  local norm
  if norm="$(_alive_parse_persisted_world_root_file "$ALIVE_CONFIG_FILE" 2>/dev/null)"; then
    if validate_world_root "$norm" | grep -q '^ok$'; then
      printf '%s\n' "$norm"
      return 0
    fi
    return 1
  fi

  if norm="$(_alive_parse_persisted_world_root_file "$LEGACY_WALNUT_CONFIG_FILE" 2>/dev/null)"; then
    local status
    status="$(validate_world_root "$norm")"
    if [ "$status" = "ok" ]; then
      # Migration only fires on a validating legacy. Write failure is
      # advisory; the caller still gets the legacy path back.
      if ! write_world_root_file "$norm" 2>/dev/null; then
        export WORLD_ROOT_ADVISORY_REASON="migration_write_failed"
      fi
      printf '%s\n' "$norm"
      return 0
    fi
    # Stale legacy: do NOT migrate (would persist a bad path into the
    # alive config and brick future resolutions); fall through to the
    # caller's "no usable config" handling.
    return 1
  fi

  return 1
}

# ---------------------------------------------------------------------------
# fn-15-la5.2: validate_path_choice -- system-path policy validator.
# Bash sibling of scripts/_world_root_io.py validate_path_choice.
# Output contract: prints exactly THREE tab-separated fields on
# stdout, terminated by a newline:
#
#   <decision>\t<category>\t<message>
#
# Decision is one of: allow, deny, confirm_required.
# Category is "" for allow, otherwise filesystem_root | system_root |
# home | cloud. Message is human-readable. Returns 0 always; callers
# branch on the decision field.
#
# The set of system roots and the match algorithm exactly mirror the
# Python sibling -- parity is enforced by the test harness.
# ---------------------------------------------------------------------------

# Hard-deny subtree roots. Bash array; iterated in declared order.
# Order doesn't change semantics (first match wins per category, all
# are deny-subtree) but the tests rely on the listed entries existing.
_ALIVE_DENY_SUBTREE_ROOTS=(
  /tmp
  /etc
  /var
  /usr
  /bin
  /sbin
  /opt
  /private
  /Library
  /System
  /Applications
  /c/Windows
  /C/Windows
  "/c/Program Files"
  "/C/Program Files"
)

# _alive_is_subtree <path> <root>
# Returns 0 (true) iff path == root OR path starts with "${root}/".
# Pure string compare via parameter expansion -- no glob/case
# patterns -- so the locked match algorithm matches the Python
# sibling exactly: ``path == root or path.startswith(root + "/")``.
# Both inputs must already be lexically normalized.
_alive_is_subtree() {
  local p="$1" root="$2"
  if [ "$p" = "$root" ]; then
    return 0
  fi
  local rlen=${#root}
  # Substring prefix check: the first ${#root} chars of $p must equal
  # $root, AND the very next char must be '/'. Pure string slicing,
  # zero glob involvement.
  if [ "${p:0:rlen}" = "$root" ] && [ "${p:rlen:1}" = "/" ]; then
    return 0
  fi
  return 1
}

# validate_path_choice <path> [home]
# System-path policy validator. Prints "<decision>\t<category>\t<message>".
# When [home] is omitted, defaults to $HOME.
validate_path_choice() {
  local raw="$1"
  local home="${2:-$HOME}"
  local norm

  if ! norm="$(lexical_normalize_path "$raw" 2>/dev/null)"; then
    # Sanitize control chars in the raw input before echoing into
    # the message field, so a path containing tab or newline cannot
    # break the 3-tab-field output contract that consumers parse via
    # ``split('\t')``. Replace tab/newline/CR with a literal '?'.
    local safe_raw="${raw//$'\t'/?}"
    safe_raw="${safe_raw//$'\n'/?}"
    safe_raw="${safe_raw//$'\r'/?}"
    printf 'deny\tsystem_root\t%s is not a valid path.\n' "$safe_raw"
    return 0
  fi

  local home_norm=""
  if [ -n "$home" ]; then
    home_norm="$(lexical_normalize_path "$home" 2>/dev/null || true)"
  fi

  # 1. Hard-deny exact.
  if [ "$norm" = "/" ]; then
    printf 'deny\tfilesystem_root\t%s is the filesystem root and cannot host a world.\n' "$norm"
    return 0
  fi
  if [ "$norm" = "/Volumes" ]; then
    printf 'deny\tsystem_root\t%s is the volumes mount directory; pick a specific volume like /Volumes/<name>/alive instead.\n' "$norm"
    return 0
  fi

  # 2. Hard-deny subtree.
  local r
  for r in "${_ALIVE_DENY_SUBTREE_ROOTS[@]}"; do
    if _alive_is_subtree "$norm" "$r"; then
      printf 'deny\tsystem_root\t%s lives under the system root %s; pick a path in your home or a mounted volume instead.\n' "$norm" "$r"
      return 0
    fi
  done

  # 3. Confirm-required exact: bare $HOME.
  if [ -n "$home_norm" ] && [ "$norm" = "$home_norm" ]; then
    printf 'confirm_required\thome\t%s is your home directory. Setting up a world here scatters domain folders across your home -- type the path back exactly to confirm, or pick a subdirectory like %s/alive instead.\n' "$norm" "$norm"
    return 0
  fi

  # 4. Confirm-required subtree: cloud-sync roots. Note Google Drive
  # is handled separately below by GoogleDrive-<email> prefix match;
  # a blanket subtree match on ${home_norm}/Library/CloudStorage would
  # incorrectly flag OneDrive / ProtonDrive / Box etc.
  if [ -n "$home_norm" ]; then
    local cloud_roots=(
      "${home_norm}/Library/Mobile Documents"
      "${home_norm}/Dropbox"
    )
    local cloud_labels=(
      "iCloud"
      "Dropbox"
    )
    local idx
    for idx in "${!cloud_roots[@]}"; do
      if _alive_is_subtree "$norm" "${cloud_roots[$idx]}"; then
        printf 'confirm_required\tcloud\t%s is inside a cloud-sync directory (%s). Cloud sync can corrupt atomic writes and replicate private context. Type the path back exactly to confirm, or pick a non-synced location.\n' "$norm" "${cloud_labels[$idx]}"
        return 0
      fi
    done

    # Google Drive: ${home_norm}/Library/CloudStorage/GoogleDrive-<*>
    # Pure-string prefix check (no globs / case patterns) to match
    # Python ``_matches_gdrive_prefix`` exactly: the path must start
    # with the GoogleDrive- prefix AND the very next character must
    # be a non-slash (i.e. the email segment is at least 1 char).
    local gdrive_base="${home_norm}/Library/CloudStorage/GoogleDrive-"
    local glen=${#gdrive_base}
    if [ "${norm:0:glen}" = "$gdrive_base" ]; then
      local first_after="${norm:glen:1}"
      if [ -n "$first_after" ] && [ "$first_after" != "/" ]; then
        printf 'confirm_required\tcloud\t%s is inside Google Drive (~/Library/CloudStorage/GoogleDrive-<email>). Cloud sync can corrupt atomic writes and replicate private context. Type the path back exactly to confirm, or pick a non-synced location.\n' "$norm"
        return 0
      fi
    fi
  fi

  # 5. Allow.
  printf 'allow\t\t%s is allowed.\n' "$norm"
  return 0
}

# write_world_root_file <path>
# Atomically persist the lexically-normalized path to
# ~/.config/alive/world-root with mode 0600 (parent dir 0700 on first
# create). Returns 0 on success.
write_world_root_file() {
  local raw="$1"
  local norm
  norm="$(lexical_normalize_path "$raw" 2>/dev/null)" || return 1
  [ -z "$norm" ] && return 1
  case "$norm" in
    /*) ;;
    *) return 1 ;;
  esac

  local target_dir
  target_dir="$(dirname -- "$ALIVE_CONFIG_FILE")"
  if [ ! -d "$target_dir" ]; then
    mkdir -p "$target_dir" || return 1
    chmod 0700 "$target_dir" 2>/dev/null || true
  fi

  local tmp
  tmp="$(mktemp "${target_dir}/.world-root.XXXXXX")" || return 1
  printf '%s\n' "$norm" > "$tmp" || { rm -f "$tmp"; return 1; }
  chmod 0600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$ALIVE_CONFIG_FILE" || { rm -f "$tmp"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# fn-15-la5.6: find_world_or_warn / find_world_or_die helpers + bridge.
#
# Sole-emitter contract: hooks NEVER echo JSON themselves on the no-world
# path post-T6. They pipe their pre-existing payload (if any) to the
# helper on stdin; the helper does ONE write to stdout: a single, valid
# JSON object that merges the existing payload with the bridge contract
# message in the event's required shape.
#
# Exit-code layers (locked):
#   * find_world (in-bash function): returns 1 on fail-loud (no world).
#     Sets WORLD_ROOT_FAIL_REASON.
#   * find_world_or_warn / find_world_or_die (helpers): always emit one
#     JSON object on stdout, then return 1 in-bash so the calling hook
#     can `exit 0` cleanly.
#   * Hook script process exit: ALWAYS 0. The JSON contract carries the
#     deny signal via `permissionDecision: deny` (cutover only). A non-
#     zero exit would override the hook contract.
#
# Per-session dedup: SessionStart-only atomic-mkdir of a sentinel
# directory under ${TMPDIR:-/tmp}/. Non-SessionStart events emit pure
# {} (or preserved payload) on failed-resolve regardless of dedup state.
# ---------------------------------------------------------------------------

# _alive_session_sentinel_dir
# Echo the per-session sentinel directory path. Uses $CLAUDE_SESSION_ID
# when set, else a deterministic fallback so tests / shells without the
# env var still get a single, race-safe sentinel name.
_alive_session_sentinel_dir() {
  local sid="${CLAUDE_SESSION_ID:-${HOOK_SESSION_ID:-no-session}}"
  # Sanitize: replace anything that isn't [A-Za-z0-9._-] with '_' so a
  # malformed session id can't escape into a different tmp path. Pure
  # parameter expansion -- no subprocess.
  local safe="${sid//[^A-Za-z0-9._-]/_}"
  printf '%s/alive-upgrade-warned-%s' "${TMPDIR:-/tmp}" "$safe"
}

# _alive_try_acquire_session_sentinel
# Atomic mkdir of the sentinel directory. Returns 0 if THIS process
# acquired (was the winner), 1 otherwise. ONLY SessionStart events
# attempt acquisition -- callers gate on event name themselves.
_alive_try_acquire_session_sentinel() {
  local dir
  dir="$(_alive_session_sentinel_dir)"
  # Best-effort parent: ${TMPDIR}/ should always exist; if it doesn't,
  # mkdir below fails and we return 1 (treat as loser, emit pure {}).
  if mkdir "$dir" 2>/dev/null; then
    return 0
  fi
  return 1
}

# _alive_session_sentinel_acquired
# Read-only check: returns 0 iff the sentinel directory exists. Used by
# tests; helpers themselves rely on the atomic mkdir return.
_alive_session_sentinel_acquired() {
  local dir
  dir="$(_alive_session_sentinel_dir)"
  [ -d "$dir" ]
}

# _alive_resolve_doctor_cmd
# Resolve ${ALIVE_DOCTOR_CMD} at message-build time. Returns the command
# prefix that, when followed by `doctor <args>`, invokes the alive
# doctor subcommand. Order:
#   1) ${CLAUDE_PLUGIN_ROOT}/bin/alive   if executable
#   2) alive                              if `command -v alive` succeeds
#   3) python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cli.py   fallback
# Bridge messages then build "${ALIVE_DOCTOR_CMD} doctor --check=..."
# which composes correctly across all three forms (the binaries dispatch
# `doctor` as a subcommand; the python3 fallback also routes through
# scripts/cli.py which registers `doctor`).
_alive_resolve_doctor_cmd() {
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -x "${CLAUDE_PLUGIN_ROOT}/bin/alive" ]; then
    printf '%s\n' "${CLAUDE_PLUGIN_ROOT}/bin/alive"
    return 0
  fi
  if command -v alive >/dev/null 2>&1; then
    printf '%s\n' "alive"
    return 0
  fi
  # Fallback: python3 entrypoint via cli.py (NOT doctor.py -- doctor.py
  # is registered as a subcommand by cli.py). CLAUDE_PLUGIN_ROOT may be
  # unset in weird harnesses; emit a placeholder rather than an empty
  # path so the message stays informative.
  local pr="${CLAUDE_PLUGIN_ROOT:-<plugin-root>}"
  printf 'python3 %s/scripts/cli.py\n' "$pr"
  return 0
}

# _alive_bridge_message <fail_reason>
# Echo the bridge-warn copy for the given WORLD_ROOT_FAIL_REASON. The
# message includes the resolved doctor command. Single source of truth
# for the user-facing copy; no per-hook customization.
_alive_bridge_message() {
  local reason="$1"
  local doctor
  doctor="$(_alive_resolve_doctor_cmd)"

  local stale_path="${WORLD_ROOT_STALE_PATH:-}"
  local override_val="${ALIVE_WORLD_ROOT_OVERRIDE:-}"

  case "$reason" in
    not_found)
      printf 'ALIVE world is not configured. Run `/alive:world` to set up your world location, or run `%s doctor --check=world-root --fix --world-root <path>` to pin an existing world.\n' "$doctor"
      ;;
    stale_config)
      if [ -n "$stale_path" ]; then
        printf 'ALIVE world-root config points at a stale path (%s). Run `%s doctor --check=world-root --fix --world-root <path>` to repin, or `rm ~/.config/alive/world-root` to re-bootstrap.\n' "$stale_path" "$doctor"
      else
        printf 'ALIVE world-root config is stale or unreadable. Run `%s doctor --check=world-root --fix --world-root <path>` to repin, or `rm ~/.config/alive/world-root` to re-bootstrap.\n' "$doctor"
      fi
      ;;
    invalid_override)
      printf 'ALIVE_WORLD_ROOT_OVERRIDE=%s is invalid. Unset it or correct the path, then re-open the session.\n' "$override_val"
      ;;
    denied_home)
      printf 'ALIVE bootstrap matched $HOME, which requires explicit confirmation. Run `%s doctor --check=world-root --fix --world-root $HOME --allow-home`, or set ALIVE_WORLD_ROOT_OVERRIDE.\n' "$doctor"
      ;;
    denied_cloud)
      printf 'ALIVE bootstrap matched a cloud-sync directory, which requires explicit confirmation. Run `%s doctor --check=world-root --fix --world-root <path> --allow-cloud`, or set ALIVE_WORLD_ROOT_OVERRIDE.\n' "$doctor"
      ;;
    *)
      # Unknown reason -- fall back to the generic bridge copy. Defensive:
      # we'd rather show *something* than crash the hook chain.
      printf 'ALIVE world is not configured. Run `/alive:world` to set up your world location.\n'
      ;;
  esac
}

# _alive_advisory_message <advisory_reason>
# Echo the one-time advisory copy for WORLD_ROOT_ADVISORY_REASON. Used
# by the SessionStart helper when find_world SUCCEEDS but with a soft
# warning (e.g., legacy migration write failed).
_alive_advisory_message() {
  local reason="$1"
  local doctor
  doctor="$(_alive_resolve_doctor_cmd)"
  case "$reason" in
    migration_write_failed)
      printf 'ALIVE: legacy walnut config-file migration write failed. Using the legacy ~/.config/walnut/world-root for this session. Run `%s doctor --check=world-root --fix` to retry, or check permissions on ~/.config/alive/.\n' "$doctor"
      ;;
    cwd_config_divergence)
      # fn-25: cwd walks up to a different valid world than the
      # config-resolved one. Both paths are user-facing. Restart-after-fix
      # is mandatory copy: --fix updates the config file but the
      # currently-loaded world stays bound to the old config until the
      # next session start re-runs find_world.
      local divergent_cwd="${WORLD_ROOT_DIVERGENT_CWD_PATH:-}"
      local config_world="${WORLD_ROOT:-}"
      printf 'You are working inside %s, but ALIVE loaded %s from your config. Run `%s doctor --check=world-root --fix` and restart Claude Code to switch your config to the world you are standing in, or proceed with the loaded world.\n' "$divergent_cwd" "$config_world" "$doctor"
      ;;
    *)
      # Future advisory reasons land here. Empty echo means no hint
      # appended -- callers should treat empty advisory as "nothing to
      # surface" rather than crashing.
      printf ''
      ;;
  esac
}

# _alive_emit_hook_json <event_name> <bridge_msg>
# Sole emitter on the no-world path. Reads optional pre-existing JSON
# payload from STDIN (NOT argv -- argv breaks on multi-line / large
# payloads). Merges the bridge message in the event's required shape:
#
#   SessionStart  -> {"additional_context": "<msg>",
#                     "hookSpecificOutput": {"hookEventName": "SessionStart",
#                                            "additionalContext": "<msg>"}}
#                    (top-level snake_case AND nested camelCase, matching
#                     alive-session-new.sh's existing shape)
#   PreToolUse    -> {"hookSpecificOutput": {"hookEventName": "PreToolUse",
#                                            "permissionDecision": "deny",
#                                            "permissionDecisionReason": "<msg>"}}
#                    (cutover release ONLY -- bridge release no-ops here.
#                     The find_world_or_warn caller passes empty bridge_msg
#                     for non-SessionStart events to suppress this branch.)
#   Other events  -> {} (or the preserved payload, if any).
#
# Output preservation rules:
#   - Non-string values: exact-equal at the same JSON path.
#   - String values: pre-T6 string is a SUBSTRING of post-T6 string at
#     the same path. (additional_context is appended newline-separated.)
#   - New keys: allowed; pre-T6 keys MUST NOT be removed or renamed.
#
# Always emits exactly ONE valid JSON object on stdout.
_alive_emit_hook_json() {
  local event_name="$1"
  local bridge_msg="${2:-}"

  # Read any pre-existing payload from stdin. Never blocks: when stdin
  # is a closed pipe (no payload), `cat` returns immediately with empty
  # output. Hooks with no payload pipe </dev/null OR omit the
  # redirection entirely -- empty stdin is treated as "no payload".
  local payload
  payload="$(cat 2>/dev/null || true)"

  # Routing: only SessionStart and PreToolUse take a non-{} shape.
  # Everything else (PostToolUse, UserPromptSubmit, Stop, SubagentStop,
  # PreCompact, CwdChanged, etc.) emits {} (or the preserved payload
  # unchanged).
  local merger_event=""
  case "$event_name" in
    SessionStart)
      merger_event="SessionStart"
      ;;
    PreToolUse)
      merger_event="PreToolUse"
      ;;
    *)
      merger_event=""
      ;;
  esac

  # If there's no bridge message OR the event isn't message-bearing,
  # passthrough: payload unchanged, or {} when no payload.
  if [ -z "$bridge_msg" ] || [ -z "$merger_event" ]; then
    if [ -n "$payload" ]; then
      printf '%s' "$payload"
      # Ensure trailing newline so multiple emits in test scaffolding
      # don't run together when piped.
      case "$payload" in
        *$'\n') ;;
        *) printf '\n' ;;
      esac
    else
      printf '{}\n'
    fi
    return 0
  fi

  # Message-bearing path. Need the JSON runtime; without it we cannot
  # safely escape arbitrary message strings. Bias toward "emit {}"
  # rather than "emit broken JSON" if no runtime is available.
  if [ "$ALIVE_JSON_RT" != "python3" ] && [ "$ALIVE_JSON_RT" != "node" ]; then
    if [ -n "$payload" ]; then
      printf '%s' "$payload"
      case "$payload" in
        *$'\n') ;;
        *) printf '\n' ;;
      esac
    else
      printf '{}\n'
    fi
    return 0
  fi

  if [ "$ALIVE_JSON_RT" = "python3" ]; then
    ALIVE_HOOK_EVENT="$merger_event" \
    ALIVE_HOOK_BRIDGE_MSG="$bridge_msg" \
    ALIVE_HOOK_PAYLOAD="$payload" \
    python3 - <<'PY'
import json, os, sys

event = os.environ.get("ALIVE_HOOK_EVENT", "")
bridge_msg = os.environ.get("ALIVE_HOOK_BRIDGE_MSG", "")
raw_payload = os.environ.get("ALIVE_HOOK_PAYLOAD", "")

# Parse the existing payload (if any). Anything that doesn't decode
# as a JSON object is treated as no-payload -- we can't safely merge
# arrays/scalars into a hook-shaped object, and the alternative
# (emit two top-level objects) is invalid hook output.
existing = {}
if raw_payload.strip():
    try:
        decoded = json.loads(raw_payload)
        if isinstance(decoded, dict):
            existing = decoded
    except (ValueError, TypeError):
        existing = {}

if event == "SessionStart":
    # Top-level additional_context: append (newline-separated) when
    # there's an existing string, else set fresh.
    prior_top = existing.get("additional_context")
    if isinstance(prior_top, str) and prior_top:
        existing["additional_context"] = prior_top + "\n\n" + bridge_msg
    else:
        existing["additional_context"] = bridge_msg

    # Nested hookSpecificOutput.{hookEventName, additionalContext}: same
    # append rule for additionalContext; hookEventName is force-set to
    # SessionStart (per locked schema) but ONLY if not already that
    # value -- preserve any pre-existing non-string equality at this
    # path per the output preservation contract (which allows extending
    # strings but requires non-string values to remain exact-equal).
    nested = existing.get("hookSpecificOutput")
    if not isinstance(nested, dict):
        nested = {}
    prior_nested_event = nested.get("hookEventName")
    if not isinstance(prior_nested_event, str) or not prior_nested_event:
        nested["hookEventName"] = "SessionStart"
    # If the prior value is a different string, leave it (non-T6 hook
    # produced something we shouldn't silently rename). The substring
    # rule applies only to message strings; hookEventName is a
    # non-message string and conventionally the same across the path.
    prior_nested = nested.get("additionalContext")
    if isinstance(prior_nested, str) and prior_nested:
        nested["additionalContext"] = prior_nested + "\n\n" + bridge_msg
    else:
        nested["additionalContext"] = bridge_msg
    existing["hookSpecificOutput"] = nested

elif event == "PreToolUse":
    # Cutover-release shape. Merge into hookSpecificOutput; do NOT add
    # top-level additional_context (PreToolUse doesn't take one).
    nested = existing.get("hookSpecificOutput")
    if not isinstance(nested, dict):
        nested = {}
    prior_event = nested.get("hookEventName")
    if not isinstance(prior_event, str) or not prior_event:
        nested["hookEventName"] = "PreToolUse"
    nested["permissionDecision"] = "deny"
    prior_reason = nested.get("permissionDecisionReason")
    if isinstance(prior_reason, str) and prior_reason:
        nested["permissionDecisionReason"] = prior_reason + "\n\n" + bridge_msg
    else:
        nested["permissionDecisionReason"] = bridge_msg
    existing["hookSpecificOutput"] = nested

# Single write to stdout; trailing newline so concatenation in test
# harnesses stays well-formed.
sys.stdout.write(json.dumps(existing))
sys.stdout.write("\n")
PY
    return 0
  fi

  # node runtime
  ALIVE_HOOK_EVENT="$merger_event" \
  ALIVE_HOOK_BRIDGE_MSG="$bridge_msg" \
  ALIVE_HOOK_PAYLOAD="$payload" \
  node -e "
const event = process.env.ALIVE_HOOK_EVENT || '';
const msg = process.env.ALIVE_HOOK_BRIDGE_MSG || '';
const raw = process.env.ALIVE_HOOK_PAYLOAD || '';
let existing = {};
if (raw.trim()) {
  try {
    const d = JSON.parse(raw);
    if (d && typeof d === 'object' && !Array.isArray(d)) existing = d;
  } catch (e) { existing = {}; }
}
if (event === 'SessionStart') {
  const prior = existing['additional_context'];
  existing['additional_context'] = (typeof prior === 'string' && prior) ? prior + '\n\n' + msg : msg;
  let nested = existing['hookSpecificOutput'];
  if (!nested || typeof nested !== 'object' || Array.isArray(nested)) nested = {};
  if (typeof nested['hookEventName'] !== 'string' || !nested['hookEventName']) nested['hookEventName'] = 'SessionStart';
  const priorN = nested['additionalContext'];
  nested['additionalContext'] = (typeof priorN === 'string' && priorN) ? priorN + '\n\n' + msg : msg;
  existing['hookSpecificOutput'] = nested;
} else if (event === 'PreToolUse') {
  let nested = existing['hookSpecificOutput'];
  if (!nested || typeof nested !== 'object' || Array.isArray(nested)) nested = {};
  if (typeof nested['hookEventName'] !== 'string' || !nested['hookEventName']) nested['hookEventName'] = 'PreToolUse';
  nested['permissionDecision'] = 'deny';
  const priorR = nested['permissionDecisionReason'];
  nested['permissionDecisionReason'] = (typeof priorR === 'string' && priorR) ? priorR + '\n\n' + msg : msg;
  existing['hookSpecificOutput'] = nested;
}
process.stdout.write(JSON.stringify(existing));
process.stdout.write('\n');
" 2>/dev/null
  return 0
}

# find_world_or_warn <event_name>
# Bridge-release helper: calls find_world. On success, writes nothing
# to stdout (caller proceeds normally; advisory hint is surfaced by
# emit_advisory_hint below). On failure, emits ONE valid JSON object
# on stdout (event-aware shape), and returns 1 in-bash so the caller
# can `exit 0` cleanly.
#
# Sentinel acquisition is SessionStart-only. Non-SessionStart events
# emit pure {} (or preserved stdin payload unchanged) on failed-resolve
# regardless of dedup state.
#
# Reads optional pre-existing JSON payload from STDIN. Argv has only
# the event-name positional.
find_world_or_warn() {
  local event_name="${1:-}"

  if find_world; then
    # Successful resolve: helper does NOT consume stdin (caller didn't
    # need merging) and does NOT write to stdout. Caller proceeds with
    # WORLD_ROOT set as before.
    return 0
  fi

  # find_world failed; WORLD_ROOT_FAIL_REASON is set.
  local reason="${WORLD_ROOT_FAIL_REASON:-not_found}"
  local bridge_msg=""

  # Bridge messaging is SessionStart-only. Other events emit pure {}
  # (or preserved payload). PreToolUse remains message-less in the
  # warn release -- the cutover release wires find_world_or_die which
  # populates permissionDecisionReason.
  if [ "$event_name" = "SessionStart" ]; then
    if _alive_try_acquire_session_sentinel; then
      bridge_msg="$(_alive_bridge_message "$reason")"
    fi
    # Loser: bridge_msg stays empty -> emitter passes through payload.
  fi

  _alive_emit_hook_json "$event_name" "$bridge_msg"
  return 1
}

# find_world_or_die <event_name>
# Cutover-release helper. Same emit contract as find_world_or_warn but
# the message is the deny copy and PreToolUse events get the deny
# permissionDecision injected. Dedup is preserved (13 deny messages
# would wreck UX). All paths still `return 1` in-bash so the caller
# `exit 0`s cleanly -- the deny signal travels via the JSON contract,
# never via the process exit code.
#
# Reserved for the post-T6 cutover PR. Wired here so the bridge -> die
# swap is a mechanical rename across the 13 hooks. Today's 13 hooks
# call find_world_or_warn; the cutover PR replaces all of them with
# find_world_or_die in a single mechanical pass.
find_world_or_die() {
  local event_name="${1:-}"

  if find_world; then
    return 0
  fi

  local reason="${WORLD_ROOT_FAIL_REASON:-not_found}"
  local bridge_msg=""

  if [ "$event_name" = "SessionStart" ]; then
    if _alive_try_acquire_session_sentinel; then
      bridge_msg="$(_alive_bridge_message "$reason")"
    fi
  elif [ "$event_name" = "PreToolUse" ]; then
    # PreToolUse cutover always emits the deny message (no dedup --
    # different sentinel space; PreToolUse blocks must always surface
    # the reason or the user sees a silent deny).
    bridge_msg="$(_alive_bridge_message "$reason")"
  fi

  _alive_emit_hook_json "$event_name" "$bridge_msg"
  return 1
}

# emit_advisory_hint <event_name>
# Companion helper for the WORLD_ROOT_ADVISORY_REASON channel. Called
# AFTER a successful find_world (in-bash return 0) when an advisory
# is set. SessionStart-only, deduped via the same sentinel as the
# bridge-warn copy so the user sees the hint exactly once per session.
#
# Writes ONE JSON object to stdout when the hint fires; emits {} when
# there's nothing to surface (no advisory, wrong event, sentinel
# already taken).
#
# Hooks that have their own onboarding payload pipe it on stdin; this
# is the same merge contract as find_world_or_warn.
emit_advisory_hint() {
  local event_name="${1:-}"
  local advisory="${WORLD_ROOT_ADVISORY_REASON:-}"

  if [ -z "$advisory" ] || [ "$event_name" != "SessionStart" ]; then
    # Passthrough: payload unchanged (or {} when none). The caller
    # decides whether to invoke us at all; we treat "no advisory" as
    # a no-op shape rather than an error.
    _alive_emit_hook_json "$event_name" ""
    return 0
  fi

  if _alive_try_acquire_session_sentinel; then
    local msg
    msg="$(_alive_advisory_message "$advisory")"
    _alive_emit_hook_json "$event_name" "$msg"
    return 0
  fi

  # Lost the race; passthrough.
  _alive_emit_hook_json "$event_name" ""
  return 0
}
