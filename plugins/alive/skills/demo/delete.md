# alive:demo delete

Sibling skill page invoked when `$ARGUMENTS[0] == "delete"` and `$ARGUMENTS[1]` carries the world ref.

Removes a previously-promoted demo world from disk via `shutil.rmtree`. Refuses on the currently-active world (the user must deactivate first). Requires explicit `--confirm` on every other call so an accidental `alive demo delete <ref>` cannot silently destroy data.

## Irreversibility contract

This skill follows the global guidance on irreversible actions:

* The CLI returns `error.code == "needs_confirmation"` on every first call (regardless of `--confirm` absence) and renders an irreversibility surface that quotes the world's ULID, label, path, size, and creation date.
* The squirrel MUST dispatch `AskUserQuestion` with options `Delete now`, `Cancel`. The user picks `Delete now` explicitly before the squirrel re-runs with `--confirm`.
* Worker-level confirmation alone is not enough; the skill prose carries the user-visible safety net.

## Flow

1. Run `alive demo delete <ref>` (no `--confirm`). The CLI resolves the ref.
2. Branch on the JSON envelope:

   * `error.code == "refused_active"` -> the ref points at the currently-active demo world. Print the `rendered_block` (refusal surface). Hint: `Run /alive:demo deactivate first, then re-run delete.` Do NOT proceed.
   * `error.code == "needs_confirmation"` -> Print the `rendered_block` (irreversibility surface). Dispatch `AskUserQuestion` with options `Delete now`, `Cancel`. On `Delete now`, re-run `alive demo delete <ref> --confirm`.
   * `error.code == "ambiguous_ref"` -> Same picker pattern as `activate.md`. Render the picker block, dispatch `AskUserQuestion`, re-run with the picked ULID.
   * `error.code == "not_found"` -> Print the squirrel's own block carrying `error.message`.
   * `success: true` (only on `--confirm`) -> Print the `rendered_block` (deletion confirmation).

## Example invocations

```bash
# First call: get the irreversibility surface.
$ALIVE_PLUGIN_ROOT/bin/alive demo delete morgan-hayes-illustrator

# Second call (after AskUserQuestion confirmation): actually delete.
$ALIVE_PLUGIN_ROOT/bin/alive demo delete morgan-hayes-illustrator --confirm
```

## Confirmation surface

```
╭─ 🐿️ delete -- irreversible
│  About to PERMANENTLY DELETE this demo world:
│    wld_<ulid>  -  <label>
│    <path>
│    size: <human>, created: <ISO date>
│
│  This cannot be undone. The world directory will be removed
│  with shutil.rmtree.
│
│  Re-run with --confirm to proceed.
╰─
```

## Refusal surface (active world)

```
╭─ 🐿️ delete refused -- active world
│  Refusing to delete the currently-active demo world:
│    wld_<ulid>  -  <label>
│
│  Run /alive:demo deactivate first, then re-run delete.
╰─
```

## What happens on `--confirm`

`stages/delete_existing.run_delete(record, confirm=True)`:

1. Re-check that the ref does not match the live world-root pointer. (A user could activate the world in between calls; the active-world refusal must fire even on `--confirm`.)
2. `shutil.rmtree(record.path)`.
3. Update demo-state.json: matching `partial_generations` entry (if any) gets `status="failed"` and a fresh `last_updated` so audit trails reflect the deletion.

## Success surface

```
╭─ 🐿️ deleted
│  Demo world removed:
│    wld_<ulid>  -  <label>
│    <path>
╰─
```

## Filesystem race

If the world directory disappeared between resolve and rmtree, the CLI reports `success: true` with `result.deleted.already_gone == true`. The squirrel can surface a "world was already gone; demo-state cleaned up" note if helpful.
