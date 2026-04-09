#!/usr/bin/env bash
# alive-relay-check.sh -- SessionStart relay state probe (LD16, fn-7-7cw).
#
# Runs once at session-start (matchers: startup, resume) to refresh
# ~/.alive/relay/state.json so the user sees up-to-date pending package
# counts. Rate-limited to once every 10 minutes via the top-level
# `last_probe` field in state.json (LD17 -- the field replaces the prior
# `last_sync` name).
#
# Exit code policy (LD16, exact):
#   0 -- success: probe ran OR within cooldown OR relay not configured.
#        Per-peer failures are recorded INSIDE state.json as data; the
#        hook still exits 0 because peer-level failures are routine
#        (offline, quota, rate-limited).
#   1 -- hard local failure: cannot read relay.json, cannot write
#        state.json, gh CLI missing. Rare; the session continues either
#        way because Claude Code only treats exit 2 as a chain block.
#   NEVER 2 -- exit 2 would block the SessionStart hook chain. This is
#              a notification hook, not a guard.
#
# Sources alive-common.sh for read_hook_input/find_world; the hook reads
# stdin (Claude Code passes the session JSON) but does not require any
# field beyond the implicit cwd.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=alive-common.sh
source "$SCRIPT_DIR/alive-common.sh"

# Drain stdin so Claude Code's hook chain does not block on a closed pipe.
# read_hook_input reads stdin once; subsequent helpers read from $HOOK_INPUT.
read_hook_input

# Resolve world root for the optional discovery-hint codepath. Probe itself
# does not depend on a world being loaded -- relay config is per-user, not
# per-world -- so a missing world root is fine.
find_world || true

RELAY_DIR="${HOME}/.alive/relay"
RELAY_JSON="${RELAY_DIR}/relay.json"
STATE_JSON="${RELAY_DIR}/state.json"
COOLDOWN_SECONDS=600  # 10 minutes per LD16

# ---------------------------------------------------------------------------
# Optional: discovery hint when relay not configured.
# ---------------------------------------------------------------------------
maybe_discovery_hint() {
  # Only print the hint if the user opted in via preferences.yaml top-level
  # `discovery_hints: true`. Hint goes to stderr (informational, never
  # blocks). Hook still exits 0 from the main flow.
  if [ -z "${WORLD_ROOT:-}" ]; then
    return 0
  fi
  local prefs="${WORLD_ROOT}/.alive/preferences.yaml"
  if [ ! -f "$prefs" ]; then
    return 0
  fi
  # Cheap grep -- we are not parsing YAML here, just spotting the opt-in.
  # Lines starting with '#' are skipped (commented defaults).
  if grep -E '^[[:space:]]*discovery_hints:[[:space:]]*true' "$prefs" >/dev/null 2>&1; then
    printf '# alive: P2P relay available -- run /alive:relay setup\n' >&2
  fi
}

if [ ! -f "$RELAY_JSON" ]; then
  # Not configured. LD16: exit 0. Optionally hint the feature exists.
  maybe_discovery_hint
  exit 0
fi

# ---------------------------------------------------------------------------
# Cooldown check: read state.json `last_probe` and skip if within window.
# ---------------------------------------------------------------------------
if [ -f "$STATE_JSON" ]; then
  LAST_PROBE_RAW=""
  if [ "$ALIVE_JSON_RT" = "python3" ]; then
    LAST_PROBE_RAW=$(python3 - <<'PY' 2>/dev/null
import json, sys, os
path = os.environ.get("ALIVE_RELAY_STATE_JSON")
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(data.get("last_probe") or "")
except Exception:
    print("")
PY
)
  elif [ "$ALIVE_JSON_RT" = "node" ]; then
    LAST_PROBE_RAW=$(ALIVE_RELAY_STATE_JSON="$STATE_JSON" node -e "
try {
  const d = JSON.parse(require('fs').readFileSync(process.env.ALIVE_RELAY_STATE_JSON, 'utf8'));
  process.stdout.write(d.last_probe || '');
} catch (e) {
  process.stdout.write('');
}
" 2>/dev/null)
  fi
  # Compute age in seconds. We accept ISO-8601 with trailing Z.
  if [ -n "$LAST_PROBE_RAW" ] && [ "$ALIVE_JSON_RT" = "python3" ]; then
    AGE_SECONDS=$(ALIVE_RELAY_LAST_PROBE="$LAST_PROBE_RAW" python3 - <<'PY' 2>/dev/null
import os, datetime
raw = os.environ.get("ALIVE_RELAY_LAST_PROBE", "")
if not raw:
    print(-1)
    raise SystemExit(0)
try:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    t = datetime.datetime.fromisoformat(raw)
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    print(int((now - t).total_seconds()))
except Exception:
    print(-1)
PY
)
    if [ -n "$AGE_SECONDS" ] && [ "$AGE_SECONDS" -ge 0 ] 2>/dev/null; then
      if [ "$AGE_SECONDS" -lt "$COOLDOWN_SECONDS" ]; then
        # Within cooldown -- skip silently. LD16 exit 0.
        exit 0
      fi
    fi
  fi
fi

export ALIVE_RELAY_STATE_JSON="$STATE_JSON"

# ---------------------------------------------------------------------------
# Run the probe in the background. We do not block session start on the
# network round-trip -- the next session reads whatever the probe wrote.
# ---------------------------------------------------------------------------
PROBE_SCRIPT="${SCRIPT_DIR}/../../scripts/relay-probe.py"
if [ ! -f "$PROBE_SCRIPT" ]; then
  # Plugin layout drift -- not a user-fixable issue. Surface as exit 1.
  printf 'alive-relay-check: probe script missing at %s\n' "$PROBE_SCRIPT" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf 'alive-relay-check: python3 not on PATH\n' >&2
  exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
  # gh missing is a soft failure -- the user might be on a machine without
  # gh installed. Record nothing, exit 0 so the session-start chain runs.
  exit 0
fi

# Background fire. Discard stdout (we have nothing to say); keep stderr so
# diagnostic messages from relay-probe.py reach the session log if the user
# is running with hook output enabled.
(
  python3 "$PROBE_SCRIPT" probe --all-peers --output "$STATE_JSON" >/dev/null 2>&1 || true
) &

# Detach from the background job so the hook returns immediately.
disown 2>/dev/null || true

exit 0
