---
type: reference
description: "Full 9-step interactive flow for /alive:share. Loaded on demand from SKILL.md."
---

# Share — full 9-step interactive flow

Long-form reference for `/alive:share`. The router in `SKILL.md` covers the
common bash one-liners; this file walks through every decision point with
the prompts the squirrel surfaces and the actions it takes.

The flow is identical for full / bundle / snapshot scopes; only steps 2 and 3
differ. Each step references the relevant LD (locked design) from epic
fn-7-7cw for traceability.

---

## Step 1 — Confirm walnut + scope

Read the active walnut from session state. Refuse early if:

- No walnut is loaded → tell the human to run `/alive:load-context` first.
- The walnut path lives under `01_Archive/` → archived walnuts are
  intentionally not shareable; surface "this walnut is archived; resurrect it
  via /alive:system-cleanup if you need to share".

Surface scope choice:

```
╭─ alive:share
│  Walnut: {walnut_name}
│  Path:   {walnut_path}
│
│  ▸ What kind of share?
│  1. Full walnut       — kernel + bundles + live context
│  2. Specific bundle   — pick one or more named bundles
│  3. Snapshot          — identity only (key.md + insights)
│  4. Cancel
╰─
```

Record the choice; everything downstream branches off it.

---

## Step 2 — Bundle selection (bundle scope only)

Skip for full/snapshot. For bundle scope, enumerate top-level bundles via
the CLI:

```bash
BUNDLES_JSON=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" \
    list-bundles --walnut "$WALNUT" --json)
```

The JSON shape is:

```json
[
  {"name": "shielding-review", "relpath": "shielding-review", "abs_path": "...", "top_level": true},
  {"name": "launch-checklist", "relpath": "launch-checklist", "abs_path": "...", "top_level": true},
  {"name": "bundle-x", "relpath": "archive/old/bundle-x", "abs_path": "...", "top_level": false}
]
```

Filter to `top_level: true` entries before showing the picker. Nested bundles
are NOT shareable per LD8 -- surface them as a one-line warning if any
exist:

```
Warning: 1 nested bundle is not shareable: archive/old/bundle-x
(Move it to walnut root or an archive via /alive:bundle to share it.)
```

Then show the picker:

```
╭─ alive:share — bundle selection
│
│  ▸ Pick bundles to ship (comma-separated numbers, or "all"):
│  1. shielding-review
│  2. launch-checklist
│  3. cancel
╰─
```

Record `--bundle <leaf>` flags for each picked bundle.

---

## Step 3 — Bundle task counts (bundle scope only)

Pull task counts via `tasks.py summary` so the human can see what's pending
in each bundle before shipping it:

```bash
TASKS_JSON=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tasks.py" \
    summary --walnut "$WALNUT" --include-items 2>/dev/null || echo "{}")
```

Shape: `{bundles: {<relpath>: {urgent, active, todo, blocked, done, items: [...]}}}`.

Display a per-bundle summary:

```
╭─ alive:share — bundle preview
│
│  shielding-review
│    urgent: 0   active: 1   todo: 3   blocked: 0   done: 5
│  launch-checklist
│    urgent: 1   active: 2   todo: 0   blocked: 0   done: 8
│
│  ▸ Continue?
│  1. Yes, ship these bundles
│  2. Edit selection
│  3. Cancel
╰─
```

For full/snapshot scope, this step is replaced with a brief content summary
(file count, total size).

---

## Step 4 — Preset selection + per-peer exclusions

Load `.alive/preferences.yaml` via the CLI's preferences loader (it's
internal -- the human-facing skill just passes `--preset NAME` through). The
share presets live in the `p2p.share_presets` section per LD17.

Show the available presets:

```
╭─ alive:share — preset
│
│  Presets configured in .alive/preferences.yaml:
│
│  ▸ Pick a preset (or none):
│  1. internal      — drops observations only
│  2. external      — drops observations, pricing, invoices, salary, strategy
│  3. (none)        — only baseline stubs apply
│  4. cancel
╰─
```

If the human picks a preset, append `--preset <name>` to the create command.

If a relay is configured AND the destination is a known peer, ALSO offer the
per-peer exclusion picker (`--exclude-from <peer>`):

```
╭─ alive:share — per-peer exclusions
│
│  Send to a specific peer's relay? Their per-peer exclusions in
│  ~/.alive/relay/relay.json will be applied additively to the preset.
│
│  ▸ Pick a peer:
│  1. benflint        (5 patterns)
│  2. willsupernormal (0 patterns)
│  3. (none)
│  4. cancel
╰─
```

See `presets.md` for the full per-peer exclusion schema.

---

## Step 5 — Encryption choice

Three modes per LD21:

```
╭─ alive:share — encryption
│
│  ▸ Encrypt the package?
│  1. None        — anyone with the file can read it
│  2. Passphrase  — AES-256-CBC, you set the passphrase
│  3. RSA hybrid  — encrypt for one or more peers' public keys
│  4. cancel
╰─
```

Branch:

- **None**: nothing extra to do.
- **Passphrase**: ask for the env var name holding the passphrase. Default
  to `WALNUT_PASSPHRASE`. Verify the env var is set BEFORE running create
  (export it in the same session if not).
  Append: `--encrypt passphrase --passphrase-env <ENV_VAR>`.
- **RSA hybrid**: surface the recipient picker -- the user picks one or
  more peers from `~/.alive/relay/relay.json` `peers.<name>` whose public
  keys live under `~/.alive/relay/keys/peers/<name>.pem`.
  Append: `--encrypt rsa --recipient <peer1> --recipient <peer2> ...`.
  RSA hybrid encryption lands in task .11; until then surface "RSA hybrid
  encryption lands in task .11" and offer a passphrase fallback.

---

## Step 6 — Signature choice

```
╭─ alive:share — signature
│
│  ▸ Sign the manifest with your private key?
│  1. Yes  — recipients can verify you sent it
│  2. No
│  3. cancel
╰─
```

If yes:

- Verify `p2p.signing_key_path` is set in preferences. If not, refuse with
  the actionable error "configure p2p.signing_key_path in
  .alive/preferences.yaml first".
- Verify the configured key file exists.
- Append: `--sign`.

The CLI will surface a warning if the signing pipeline is still in legacy
v2 mode (this is a known cross-task gap; full RSA-PSS signing of v3
manifests lands with the FakeRelay tests in task .11).

---

## Step 7 — Interactive preview with sensitive filename flagging

Before running the actual create command, dry-run the preview by enumerating
what would be shipped (without writing the package). The squirrel surfaces:

- Total file count
- Estimated size
- Sensitive filename matches (regex on `**/pricing*`, `**/invoice*`,
  `**/salary*`, `**/strategy*`, `**/secret*`, `**/credential*`,
  `**/.env*`)
- Substitutions applied (baseline stubs)
- Exclusions applied (preset + flags + per-peer)

Example:

```
╭─ alive:share — preview
│
│  Walnut:  test-walnut
│  Scope:   full
│  Files:   47
│  Size:    ~340 KB
│
│  Substitutions:
│    _kernel/log.md      → baseline-stub
│    _kernel/insights.md → baseline-stub
│
│  Exclusions:
│    **/observations.md  (3 files)
│    **/pricing*         (0 files)
│
│  ⚠ Sensitive filename matches NOT excluded:
│    engineering/strategy.md
│    marketing/pricing-2026.md
│
│  ▸ Continue?
│  1. Ship anyway
│  2. Edit exclusions
│  3. Cancel
╰─
```

If the human picks "edit exclusions", loop back to step 4.

---

## Step 8 — Create the package

Run the actual create command with all the flags collected so far. The
command form:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope "$SCOPE" \
    --walnut "$WALNUT" \
    $BUNDLE_ARGS \
    $PRESET_ARGS \
    $EXCLUDE_ARGS \
    $ENCRYPT_ARGS \
    $SIGN_ARGS \
    --output "$OUTPUT" \
    --yes
```

Capture the JSON output:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
    --scope full --walnut "$WALNUT" --json --yes
```

Parse and surface:

```
╭─ alive:share — done
│
│  Package: ~/Desktop/test-walnut-full-2026-04-07.walnut
│  Size:    340 KB
│  ID:      a1b2c3d4e5f67890
│
│  ▸ What next?
│  1. Open in Finder
│  2. Copy file to clipboard
│  3. Push to relay
│  4. Done
╰─
```

---

## Step 9 — Relay push (optional)

If the user picks "push to relay", invoke `/alive:relay push <package>`
which handles:

- Cloning the destination peer's relay (sparse, only their inbox)
- Verifying the encryption + signature satisfy any relay-side requirements
- Copying the package to `inbox/<sender>/<filename>`
- Committing + pushing
- Cleanup

If no relay is configured, surface the discovery hint (LD16) once and exit
cleanly. The human can still send the package via any other channel (email,
AirDrop, USB) -- the package format is identical.

---

## Append-only logging

After a successful share, the squirrel logs the share into the sender's
walnut log via the standard save protocol. Mid-session writes only happen
through `/alive:save`; do NOT freestyle a log entry from the share skill.

The log entry should include:

- Share scope
- Bundle list (if scope=bundle)
- Package size + import_id
- Recipient (if relay push)
- Encryption mode
- Signature flag

This makes the share visible in `_kernel/log.md` so future sessions can see
the history without inspecting the file system.
