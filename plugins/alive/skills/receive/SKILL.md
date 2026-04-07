---
name: alive:receive
version: 3.1.0
user-invocable: true
description: "Import a .walnut package into the world. Decrypts, validates, migrates v2 layouts, and appends an import entry. Supports direct file, 03_Inbox/ scan, and relay pull."
---

# Receive

Import a `.walnut` package into the world. The receive pipeline is atomic
end-to-end: it extracts to a staging dir on the same filesystem as the target,
validates checksums and the manifest, dedupes against the import ledger,
infers and migrates layouts, swaps under an exclusive walnut lock, appends a
log entry, and regenerates `_kernel/now.json` via an explicit subprocess.

Three entry points:

- **direct file** -- the human points at a `.walnut` file on disk
- **inbox scan** -- the skill enumerates `03_Inbox/*.walnut` and asks which
- **relay pull** -- fetches encrypted packages from a peer's GitHub relay

The router below handles the common decision tree. Long-form details live in
`reference.md` (the full 13-step LD1 pipeline) and `migration.md` (v2 → v3
layout migration semantics).

## Prerequisites

- The target walnut path is known (full / snapshot scopes need a NEW path;
  bundle scope needs an EXISTING walnut).
- For relay pull: `~/.alive/relay/relay.json` exists. If not, redirect to
  `/alive:relay setup`.
- For passphrase-encrypted packages: the human has set the passphrase in an
  env var (the skill never asks for it inline).
- For RSA-encrypted packages: the human has a private key at
  `~/.alive/relay/keys/private.pem`. RSA decryption lands in task .11 -- the
  current pipeline raises a clear NotImplementedError until then.

## Entry points

```
╭─ alive:receive
│
│  ▸ Where is the package?
│  1. Direct file path        — point at a .walnut file        → Section A
│  2. Inbox scan              — pick from 03_Inbox/            → Section B
│  3. Relay pull              — fetch from peer's relay        → Section C
╰─
```

For each entry point the receive command itself is the same shell call --
only the input source differs.

## Decision tree

After picking an input source, route by the package's declared `scope`:

```
╭─ alive:receive (scope decision)
│
│  ▸ What is the package scope?
│  1. Full        — fresh walnut (target must NOT exist)       → Section D
│  2. Bundle      — add bundles to an EXISTING walnut          → Section E
│  3. Snapshot    — minimal identity-only walnut (NOT exist)   → Section F
╰─
```

The pipeline reads the manifest and refuses if `--scope` does not match the
manifest exactly. If the human is unsure, run `alive-p2p.py info <file>`
first (Section H below) to inspect the manifest before receiving.

## Section A — Direct file

```bash
PACKAGE="/path/to/file.walnut"
TARGET="/path/to/new-or-existing-walnut"

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive "$PACKAGE" \
    --target "$TARGET" \
    --yes
```

The pipeline prints a preview block before any swap (scope, bundles, file
count, sensitivity, encryption, signer). `--yes` is REQUIRED for
non-interactive use; without it the pipeline aborts after printing the
preview so the human can review.

## Section B — Inbox scan

The receive skill always reads from `03_Inbox/` (NOT `03_Inputs/` -- v3
rebrand). List packages, ask the human which to receive, then route to
Section A with that path.

```bash
WORLD_ROOT="$(python3 -c "import os; p=os.getcwd();
while p != os.path.dirname(p):
    if os.path.isdir(os.path.join(p, '.alive')): print(p); break
    p = os.path.dirname(p)")"

ls "$WORLD_ROOT/03_Inbox/"*.walnut 2>/dev/null
```

Surface the list to the human. After they pick a file, build the receive
command from Section A.

## Section C — Relay pull

```bash
# This is a thin wrapper -- the relay pull logic lives in /alive:relay.
# After /alive:relay pulls packages into 03_Inbox/, route to Section B.
```

The relay subsystem deposits packages in `03_Inbox/<sender>/` and the
receive skill picks them up via Section B. Cleanup of the relay inbox is
the relay skill's job, not this skill.

## Section D — Full scope receive

Use when the package's manifest declares `scope: full` and the human wants
to import an entire walnut as a NEW walnut on their machine.

```bash
PACKAGE="/path/to/full.walnut"
TARGET="/path/to/new-walnut"   # MUST NOT exist; parent dir MUST exist

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive "$PACKAGE" \
    --target "$TARGET" \
    --scope full \
    --yes
```

Rules (LD18):
- Target path MUST NOT exist (refuses if even an empty dir is present).
- Parent directory MUST exist and be writable. Receive does NOT auto-create
  parent dirs.
- Rollback on swap failure: `shutil.rmtree(target_dir)` (the target was
  freshly created so this is always safe).
- All `_kernel/*` files from the package land in a fresh `_kernel/` at the
  target. All bundle dirs land flat at the target root. All live context
  files land at the target root.

If the package was encrypted with a passphrase:

```bash
export MY_PASS="..."
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive "$PACKAGE" \
    --target "$TARGET" \
    --scope full \
    --passphrase-env MY_PASS \
    --yes
```

If the package is RSA-encrypted, the receive currently raises
`NotImplementedError` -- the RSA decrypt path lands in task .11. Until then
the matching share path also blocks RSA-encrypt for outbound packages, so
this is symmetric.

## Section E — Bundle scope receive

Use when the package's manifest declares `scope: bundle` and the human
wants to add one or more bundles to an EXISTING walnut.

```bash
PACKAGE="/path/to/bundle.walnut"
TARGET="/path/to/existing-walnut"   # MUST exist with valid _kernel/key.md

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive "$PACKAGE" \
    --target "$TARGET" \
    --scope bundle \
    --yes
```

Rules (LD18):
- Target walnut MUST exist with a valid `_kernel/key.md`.
- The package's `_kernel/key.md` is compared byte-for-byte against the
  target's existing `_kernel/key.md`. If they differ, receive REFUSES with
  a "cross-walnut grafting" error. Set `ALIVE_P2P_ALLOW_CROSS_WALNUT=1` in
  the env to override (NOT recommended).
- Bundles land FLAT at `$TARGET/{bundle_name}/`, NOT
  `$TARGET/bundles/{bundle_name}/` (v3 layout).
- The target's `_kernel/{key.md, log.md body, insights.md, tasks.json,
  completed.json}` are NEVER touched (beyond the LD12 log append).
- Bundle name collisions REFUSE by default. Add `--rename` to apply LD3
  deterministic chaining (`{name}-imported-{YYYYMMDD}` then `-2`, `-3`, ...).

To pick a subset of bundles from a bundle-scope package:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive "$PACKAGE" \
    --target "$TARGET" \
    --scope bundle \
    --bundle shielding-review \
    --bundle launch-checklist \
    --yes
```

`--bundle` is repeatable. Each name must be a top-level bundle leaf in the
package (use `info` from Section H to enumerate).

## Section F — Snapshot scope receive

Use when the package declares `scope: snapshot` -- a minimal identity-only
import (key.md + insights.md only).

```bash
PACKAGE="/path/to/snapshot.walnut"
TARGET="/path/to/new-walnut"   # MUST NOT exist

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive "$PACKAGE" \
    --target "$TARGET" \
    --scope snapshot \
    --yes
```

The resulting walnut contains exactly:
- `_kernel/key.md`
- `_kernel/insights.md`
- `_kernel/log.md` (created with canonical frontmatter + the import entry)
- `_kernel/tasks.json` (empty)
- `_kernel/completed.json` (empty)
- `_kernel/imports.json` (the dedupe ledger)

No bundles, no live context, no history.

## Section G — Dedupe and idempotency

The receive pipeline is idempotent. Re-running the same receive on an
already-imported package is a STRICT NO-OP (per LD2 subset-of-union
semantics):

```
$ alive-p2p.py receive same-package.walnut --target /existing --yes
noop: already imported on prior receive; all requested bundles already applied
```

For partial bundle receives, the pipeline tracks which bundles have been
applied across all prior receives with the same `import_id`. Receiving
bundles `{A, B}` after a prior receive of `{A}` only applies `B`.

The ledger lives at `{target}/_kernel/imports.json`. If it gets corrupted,
recover via `alive-p2p.py log-import` (Section H).

## Section H — Auxiliary CLI verbs

### `info` -- inspect a package

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" info /path/to/file.walnut
```

For unencrypted packages prints the full manifest summary. For encrypted
packages WITHOUT the matching credential it prints envelope-only metadata
(file size + encryption mode) and exits 0 -- info is a discovery tool, not
a verifier. Add `--passphrase-env <ENV_VAR>` or `--private-key <PATH>` to
get the full manifest. `--json` for structured output.

### `verify` -- check signature + checksums

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" verify --package /path/to/file.walnut
```

Extracts to a temp dir, runs all validation steps, prints PASS/FAIL per
check, exits 0 if all pass. Cleans up on exit even if the verify fails.

### `log-import` -- recover from a failed log edit

If LD1 step 10 (log edit) failed during a prior receive, the walnut is
structurally correct but missing the log entry. Recovery:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" log-import \
    --walnut /path/to/walnut \
    --import-id <16-or-64-char-import-id-from-ledger> \
    --sender patrickSupernormal \
    --scope bundle \
    --bundles shielding-review,launch-checklist
```

Reads `{walnut}/_kernel/imports.json` to find the import_id, then appends a
canonical entry to `_kernel/log.md` after the YAML frontmatter.

### `unlock` -- release a stuck walnut lock

If a prior receive crashed without releasing its lock, the next receive
refuses with `busy: another operation holds the walnut lock (pid X)`.
Recovery:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" unlock --walnut /path/to/walnut
```

Inspects the lock holder's PID. If the process is dead, removes the lock
file or directory. If it's alive, refuses with a clear message.

## Error paths

Common failures and the actionable response:

- **`Target path '...' already exists`** (full/snapshot scope) -- choose a
  different `--target` path. Receive refuses to write into an existing dir.
- **`Parent directory '...' does not exist`** -- create the parent first or
  pick a different target.
- **`Target walnut missing _kernel/key.md`** (bundle scope) -- the target
  is not a valid walnut. Use full scope to create it first.
- **`Package key.md does not match target walnut key.md`** -- the bundle
  was exported from a different walnut. Pick a different target or set
  `ALIVE_P2P_ALLOW_CROSS_WALNUT=1` to override (NOT recommended).
- **`Bundle name collision at target`** -- re-run with `--rename` to apply
  LD3 chaining, or move the existing bundle aside first.
- **`busy: another operation holds the walnut lock`** -- another receive
  or share is in progress. Wait, or run `unlock` if stuck.
- **`Cannot decrypt package -- wrong passphrase or unsupported format`** --
  the LD5 fallback chain exhausted all known openssl modes. Try `openssl
  enc -d` manually with each fallback to debug.
- **`RSA hybrid decryption lands in task .11`** -- this packageʼs envelope
  is RSA-encrypted; use a passphrase or unencrypted package until .11
  ships.
- **`Package tar failed safety check`** -- the package contains a path
  traversal, symlink, or other unsafe member. Zero files were written to
  staging.
- **Non-fatal warnings** (LD1 steps 10/11/12) -- the swap succeeded but
  log/ledger/now.json hit an error. The walnut is structurally correct.
  Run `log-import` or `python3 .../scripts/project.py --walnut <target>`
  to recover. Add `--strict` if you want these warnings to fail the
  receive.

## Quick commands

```bash
# Receive an unencrypted full package into a new walnut.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive /path/file.walnut \
    --target /new/path --yes

# Receive a passphrase-encrypted bundle package into an existing walnut.
export MY_PASS="..."
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive /path/file.walnut \
    --target /existing --scope bundle \
    --passphrase-env MY_PASS --yes

# Receive with rename on collision.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" receive /path/file.walnut \
    --target /existing --scope bundle --rename --yes

# Inspect a package without receiving it.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" info /path/file.walnut

# Verify checksums + signature.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" verify --package /path/file.walnut

# Force-release a stuck lock.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" unlock --walnut /existing
```

## See also

- `reference.md` -- the full 13-step LD1 pipeline broken down step by step
  with failure semantics, journal lifecycle, and exit code matrix.
- `migration.md` -- v2 → v3 layout migration: when it triggers, what it
  rewrites, and how to debug a failed migration.
- `/alive:share` -- the matching outbound skill on the sender side.
- `/alive:relay` -- set up the private GitHub relay for automatic delivery.
