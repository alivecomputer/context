# Receive — v2 → v3 Layout Migration

When receiving a package whose source walnut was on the legacy v2 layout
(`bundles/` container + `_kernel/_generated/` projection dir), the receive
pipeline runs `migrate_v2_layout` against the staging directory BEFORE the
transactional swap. This document describes when migration triggers, what
it rewrites, what edge cases to expect, and how to debug a failed
migration.

The implementation lives in `plugins/alive/scripts/alive-p2p.py` as
`migrate_v2_layout(staging_dir)`. It is called from LD1 step 6 in the
receive pipeline. It can also be run manually via the CLI subcommand
`migrate --staging <path>`.

## When migration triggers

`migrate_v2_layout` is invoked from LD1 step 6 IFF the inferred layout
from step 4 is `"v2"`. The inference precedence is:

1. `--source-layout v2` from the CLI (testing-only, requires
   `ALIVE_P2P_TESTING=1`).
2. `manifest.source_layout: v2` field in the package manifest.
3. Structural inference: a `staging/bundles/<name>/context.manifest.yaml`
   exists, OR `staging/_kernel/_generated/` exists as a directory.

If none of the above match, the staging tree is treated as `v3` (or
`agnostic` for snapshot-shaped trees) and migration is skipped.

The receive pipeline ALSO accepts `v3`-shaped packages from senders whose
on-disk walnut was v2 — the sender's `create` step packages bundles flat
regardless of source layout, so most v2-walnut → v3-package shares never
need this migration. Migration only fires when the package itself was
explicitly built with `--source-layout v2` (testing) or is an old v2
artifact still in circulation.

## What migration rewrites

`migrate_v2_layout` performs three transformations in order:

### 1. Drop `_kernel/_generated/`

The v2 `_kernel/_generated/` directory holds projection state that is
recomputed by `project.py` on the receiver side. It is sender-local and
never needed in the package. The migration deletes it entirely:

```
staging/_kernel/_generated/   →   (removed)
```

### 2. Flatten `bundles/<name>/` to `<name>/`

The v2 `bundles/` container is moved to flat top-level dirs at the
staging root. Each bundle becomes a v3-style flat bundle:

```
staging/bundles/shielding-review/   →   staging/shielding-review/
staging/bundles/launch-checklist/   →   staging/launch-checklist/
```

If the staging root already contains a directory with the same name as a
bundle being migrated (live context dir collision), the migrated bundle
is renamed with the suffix `-imported`:

```
# Before:
staging/engineering/        (live context)
staging/bundles/engineering/ (bundle named "engineering")

# After:
staging/engineering/                 (live context, untouched)
staging/engineering-imported/        (migrated bundle)
```

The collision rename suffix is `-imported` (no date), distinct from the
LD3 receive-time `-imported-{YYYYMMDD}` suffix used during step 9 swap.
This is intentional: the LD3 suffix is for COLLISIONS WITH THE TARGET
WALNUT, while the migration suffix is for collisions WITHIN THE STAGING
TREE due to ambiguous v2 packaging.

After migration, the empty `staging/bundles/` directory is removed.

### 3. Convert `tasks.md` to `tasks.json`

Each migrated bundle's `tasks.md` (v2 markdown checklist format) is
parsed by `_parse_v2_tasks_md` and converted to a v3 `tasks.json` file.
The original `tasks.md` is deleted.

The parser handles `- [ ]`, `- [~]`, and `- [x]` checkbox lines with
optional trailing `@session` attribution. IDs are assigned sequentially
as `t-001`, `t-002`, ... scoped to the migrated bundle.

The migration applies to bundle-level `tasks.md` only. Walnut-level task
files (none in v2; v2 tasks were always bundle-scoped) are untouched.

## Result schema

`migrate_v2_layout(staging_dir)` returns a dict with these keys:

```json
{
  "actions": ["dropped _kernel/_generated/", "flattened bundles/foo -> foo", ...],
  "warnings": ["bundle name collision: bar -> bar-imported", ...],
  "bundles_migrated": ["foo", "bar-imported"],
  "tasks_converted": 7,
  "errors": []
}
```

The receive pipeline raises `ValueError("v2 -> v3 staging migration
failed: ...")` IFF `errors` is non-empty. Warnings are logged but do not
abort the receive.

## Idempotency

Running `migrate_v2_layout` against an already-v3 staging tree is a
no-op. The function detects the absence of `bundles/` and
`_kernel/_generated/` and returns:

```json
{
  "actions": ["staging is already v3-shaped; no migration needed"],
  "warnings": [],
  "bundles_migrated": [],
  "tasks_converted": 0,
  "errors": []
}
```

This means double-running migration on the same staging dir (e.g. via
the CLI `migrate` subcommand) is safe.

## Manual migration via the CLI

The CLI subcommand `migrate` runs the same function against an
already-extracted staging directory. Useful for debugging a failed
receive or for offline conversion of legacy packages:

```bash
# Extract a v2 package by hand.
mkdir /tmp/staging
tar -xzf legacy-v2-package.walnut -C /tmp/staging

# Run the migration.
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py migrate \
    --staging /tmp/staging --json
```

The result dict is printed to stdout. Exit code 0 on success (with
possible warnings), 1 on hard error.

## Source-layout precedence (testing)

For testing the v2 receive path against a freshly-built v3 tree, set the
`ALIVE_P2P_TESTING=1` env var and pass `--source-layout v2`:

```bash
ALIVE_P2P_TESTING=1 python3 ${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py \
    receive /tmp/staging-shaped-as-v2.tar.gz \
    --target /tmp/new-walnut \
    --source-layout v2 \
    --yes
```

Without `ALIVE_P2P_TESTING=1`, the `--source-layout` flag is ignored and
the pipeline falls back to the manifest field or structural inference.

## Edge cases

### Bundle name collisions inside the package

A v2 package may legitimately contain `bundles/engineering/` AND a
top-level `engineering/` live-context directory. The migration treats
the bundle as the secondary and renames it `engineering-imported`. The
live context is untouched. A warning is recorded in the result dict.

### Empty `bundles/` container

If `staging/bundles/` exists but is empty (or contains only non-bundle
dirs without `context.manifest.yaml`), the migration removes the empty
container and records no `bundles_migrated`. This is not an error — it
just means the package had no bundles.

### Missing `tasks.md` in a bundle

If a migrated bundle has no `tasks.md`, no `tasks.json` is created. The
bundle is still added to `bundles_migrated`. v3 bundles are allowed to
have empty task state.

### Multiple `_kernel/` files

The migration only touches `_kernel/_generated/`. Other `_kernel/*` files
(`key.md`, `log.md`, `insights.md`, `tasks.json`, `completed.json`,
`config.yaml`) are passed through unchanged. The receiver decides what
to do with each per LD18 scope semantics.

### `_kernel/_generated/` is a regular file

If `_kernel/_generated` exists as a regular file rather than a directory,
the migration leaves it alone and records a warning. This shouldn't
happen with a well-formed sender, but the migration is defensive.

### Existing `tasks.json` in a v2 bundle

If a v2 bundle somehow has BOTH `tasks.md` AND `tasks.json` (corrupted
sender), the migration leaves `tasks.json` untouched and deletes the
`tasks.md`. A warning is recorded. The receiver inherits whatever was in
`tasks.json`.

## Debugging a failed migration

If receive fails with `v2 -> v3 staging migration failed: ...`, the
staging dir is preserved in `parent/.alive-receive-incomplete-{timestamp}`
for inspection. Look at:

```bash
ls /parent/.alive-receive-incomplete-*/
ls /parent/.alive-receive-incomplete-*/_kernel/
ls /parent/.alive-receive-incomplete-*/bundles/   # if it exists
```

Then run the migrate CLI manually to see the full error context:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py migrate \
    --staging /parent/.alive-receive-incomplete-{timestamp} \
    --json
```

The most common failure modes:

1. **Permission denied on `_kernel/_generated/`** — the sender shipped
   a directory with restrictive perms. Fix with `chmod -R u+w
   /parent/.alive-receive-incomplete-*/` and re-run.
2. **`bundles/` is a regular file, not a directory** — the package is
   corrupted. Re-export from the sender side.
3. **Bundle dir contains an unparseable `tasks.md`** — the v2 markdown
   parser only accepts `- [ ]` / `- [~]` / `- [x]` lines. Fix the
   markdown by hand and re-run.

## See also

- `SKILL.md` — the receive skill router.
- `reference.md` — the full LD1 13-step receive pipeline.
- `/alive:share` — sender-side skill that produces v3-shaped packages
  even from v2 walnuts (so this migration mostly applies to legacy
  in-circulation packages).
