# alive:demo list

Sibling skill page invoked when `$ARGUMENTS[0] == "list"`. Lists every promoted demo world under `$ALIVE_DEMO_BASE_DIR` (default `~/.alive-demos/`) plus any in-flight partial generations from `demo-state.json`.

## Flow

1. Run `alive demo list` via `$ALIVE_PLUGIN_ROOT/bin/alive` (or the `python3 scripts/cli.py` fallback). The CLI emits a JSON envelope with `success`, `records`, `partials`, `active_world`, `active_ulid`, and a fully-rendered `rendered_block` field.
2. Print the `rendered_block` value verbatim. Do not re-render the table from `records`. The CLI is the source of truth on column widths, active-flag formatting, and bordered-block envelope.

## Example

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo list
```

Sample envelope (abbreviated):

```json
{
  "success": true,
  "active_ulid": "wld_01j5hk7yvgkw0zfg9k3vh4p9xv",
  "records": [
    {
      "ulid": "wld_01j5hk7yvgkw0zfg9k3vh4p9xv",
      "label": "alex-boring-angel-investor",
      "path": "/Users/.../.alive-demos/wld_01j5hk7yvgkw0zfg9k3vh4p9xv",
      "created_at": "2026-04-29T14:00:00Z",
      "last_activated_at": "2026-04-29T14:00:00Z",
      "disk_size_bytes": 482304,
      "status": "active",
      "persona_name": null
    }
  ],
  "rendered_block": "..."
}
```

## Output shape

The `rendered_block` looks like this when at least one world exists:

```
╭─ 🐿️ demo list
│  LABEL                       ULID                                SIZE       CREATED     LAST_ACTIVATED  STATUS
│  ─────────────────────────   ──────────────────────────────────  ────────   ──────────  ──────────────  ────────
│  alex-boring-angel-investor  wld_01j5hk7yvgkw0zfg9k3vh4p9xv      471.0 KiB  2026-04-29  2026-04-29      *active
│  morgan-hayes-illustrator    wld_01j5h3sz9p4q7t2x8c1m6n8w2k      388.5 KiB  2026-04-22  -               available
╰─
```

Active worlds carry a `*` prefix in the STATUS column. The squirrel does not need to add or remove formatting; the CLI bakes the `*active` cell.

When no worlds exist:

```
╭─ 🐿️ demo list
│  No demo worlds found.
│
│  Run /alive:demo to create one (preset or custom).
╰─
```

## Errors

* `error.code == "schema_version_mismatch"` (exit 3): demo-state.json was written by an older release. Surface the version-mismatch UX from `SKILL.md` and offer `/alive:demo reset`.
* `error.code == "lock_timeout"` (exit 5): another `/alive:demo` session is updating demo-state. Wait a few seconds and retry; the CLI hint says the same.
* `error.code == "demo_state_corrupt"` (exit 1): demo-state.json is unreadable. Surface the message + hint and route the user to `/alive:demo reset`.

In all error cases, the CLI does NOT emit a `rendered_block`; the squirrel renders its own block carrying `error.message` + `error.hint`.

## Sort order

Records are returned active-first, then by `created_at` descending. The squirrel does not re-sort.

## Notes

* `disk_size_bytes` is computed from a deep walk of the world directory (regular files only). On read errors it falls back to `-1`, which renders as `?` in the SIZE column.
* `last_activated_at` mirrors `demo-state.json[active_world].activated_at` when this record is the active world; for non-active worlds it stays empty (rendered as `-`).
* The CLI honours `$ALIVE_DEMO_BASE_DIR` so tests can isolate via `tmp_path`.
