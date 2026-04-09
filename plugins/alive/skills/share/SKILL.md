---
name: alive:share
version: 3.1.0
user-invocable: true
description: "Share a walnut, bundle, or snapshot via P2P. Encrypted, signed, and relay-pushable. Produces a portable .walnut package any peer can receive."
---

# Share

Package walnut context into a portable `.walnut` file for sharing via any
channel -- email, AirDrop, Slack, USB, or the GitHub relay. Three scopes
(`full`, `bundle`, `snapshot`), optional encryption (passphrase or RSA),
optional signing, and an audit trail in the manifest.

The router below handles the common decision tree. Long-form details live in
`reference.md` (the full 9-step interactive flow) and `presets.md` (share
preset + per-peer exclusion configuration).

## Prerequisites

- A walnut is loaded in the current session (via `/alive:load-context`).
- The walnut is NOT under `01_Archive/` (refuse to share archived walnuts).
- `python3` is available on PATH; the share pipeline is a stdlib-only CLI.
- For relay push: `~/.alive/relay/relay.json` exists. If not, the user is
  redirected to `/alive:relay setup` before sharing.

## Decision tree

Surface this to the human and route to the matching section:

```
╭─ alive:share
│
│  ▸ What kind of share?
│  1. Full walnut       — everything (with default stubs)  → Section A
│  2. Specific bundle   — one or more named bundles        → Section B
│  3. Snapshot          — identity-only (key.md + insights) → Section C
╰─
```

Default share baseline (always applies unless `--include-full-history`):

- `_kernel/log.md` is **stubbed** -- the receiver gets a placeholder pointing
  back to the sender.
- `_kernel/insights.md` is **stubbed** -- same reason.
- `_kernel/now.json`, `_kernel/_generated/`, `_kernel/imports.json`,
  `.alive/_squirrels/` are excluded entirely.
- The receiver always gets the real `key.md`, `tasks.json`, `completed.json`
  (full scope), bundles, and live context.

## Section A — Full walnut share

Use when the human wants to ship everything (kernel + bundles + live context).

```bash
WALNUT="$(python3 -c "import os; print(os.path.abspath('$WALNUT_PATH'))")"
SCOPE="full"

# Pick a preset if the human has share presets configured.
# (Surface available presets via reference.md step 4.)
PRESET_ARGS=""
# Example: PRESET_ARGS="--preset external"

# Optional: encrypt with a passphrase.
ENCRYPT_ARGS=""
# Example:
#   export MY_PASS="correct horse battery staple"
#   ENCRYPT_ARGS="--encrypt passphrase --passphrase-env MY_PASS"

# Optional: include the real log/insights (DANGEROUS -- shares history).
HISTORY_ARGS=""
# Example: HISTORY_ARGS="--include-full-history"

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope "$SCOPE" \
    --walnut "$WALNUT" \
    $PRESET_ARGS \
    $ENCRYPT_ARGS \
    $HISTORY_ARGS \
    --yes
```

The output path defaults to `~/Desktop/{walnut}-{scope}-{date}.walnut`. Pass
`--output PATH` to override. The CLI prints a summary block including the
import_id (first 16 chars), file size, and any warnings.

For the full 9-step interactive flow (preset selection, sensitivity preview,
peer picker, encryption choice, signature choice, relay push), see
`reference.md` Section A.

## Section B — Bundle share

Use when the human wants to ship one or more specific bundles. Bundle leaves
must be top-level (v3 flat or v2 `bundles/` container); nested bundles are
NOT shareable.

```bash
WALNUT="$(python3 -c "import os; print(os.path.abspath('$WALNUT_PATH'))")"

# Step 1: enumerate top-level bundles.
BUNDLES_JSON=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" \
    list-bundles --walnut "$WALNUT" --json)

# Surface the list to the human, ask which to ship.
# The JSON shape is: [{"name":..., "relpath":..., "abs_path":..., "top_level":true|false}]
# Filter to top_level==true entries before presenting.

# Step 2: get task counts per bundle (the share preview shows them).
TASKS_JSON=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tasks.py" \
    summary --walnut "$WALNUT" --include-items 2>/dev/null || echo "{}")

# Step 3: ship the picked bundles.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope bundle \
    --walnut "$WALNUT" \
    --bundle "shielding-review" \
    --bundle "launch-checklist" \
    --yes
```

`--bundle` is repeatable. At least one is required. The package always
includes `_kernel/key.md` so the receiver can verify the bundle belongs to
the right walnut (LD18 identity check).

For the interactive task-count preview and per-bundle confirmation, see
`reference.md` Section B.

## Section C — Snapshot share

Use when the human wants to share identity only -- key.md + a stubbed
insights. No history, no tasks, no bundles, no live context. Useful for
introductions ("here's what this walnut is about, but I'm not handing over
the work").

```bash
WALNUT="$(python3 -c "import os; print(os.path.abspath('$WALNUT_PATH'))")"

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope snapshot \
    --walnut "$WALNUT" \
    --yes
```

The resulting package contains exactly:

- `manifest.yaml`
- `_kernel/key.md`
- `_kernel/insights.md` (stubbed)

For the snapshot-specific preview (no task counts, no bundle list), see
`reference.md` Section C.

## Quick commands

The most common one-liners. Drop into a session when the human asks for
something specific:

```bash
# Full walnut, default stubs, default output path.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope full --walnut "$WALNUT" --yes

# Full walnut, with the external preset (drops observations + pricing files).
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope full --walnut "$WALNUT" --preset external --yes

# Single bundle, encrypted with a passphrase.
export MY_PASS="..."
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope bundle --walnut "$WALNUT" \
    --bundle shielding-review \
    --encrypt passphrase --passphrase-env MY_PASS \
    --yes

# Snapshot to a custom location.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope snapshot --walnut "$WALNUT" \
    --output ~/Desktop/intro.walnut --yes

# Inspect what bundles are shareable.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" \
    list-bundles --walnut "$WALNUT" --json
```

## Discovery hints

The first time a human runs `/alive:share` and `p2p.discovery_hints` is `true`
in `.alive/preferences.yaml` (default), drop a one-line hint that points at
relay setup if no relay is configured:

```
Tip: set up a private GitHub relay via /alive:relay so peers can pull
packages automatically. Skip this step if you're sharing via file transfer.
```

The hint auto-retires after first successful relay setup. Show it AT MOST
once per session.

## See also

- `reference.md` — full 9-step interactive flow with peer picker, encryption
  decision tree, signature choice, relay push, and sensitivity preview.
- `presets.md` — share preset schema (`.alive/preferences.yaml`),
  per-peer exclusion config (`~/.alive/relay/relay.json`), and discovery
  hints behaviour.
- `/alive:relay` — set up the private GitHub relay for automatic delivery.
- `/alive:receive` — the matching import skill on the receiver side.
