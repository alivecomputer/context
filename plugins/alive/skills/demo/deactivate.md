# alive:demo deactivate

Sibling skill page invoked when `$ARGUMENTS[0] == "deactivate"`. Restores the world-root pointer to the value cached as `previous_world_root` in demo-state.json, then clears `active_world` and `previous_world_root` so a follow-up activation starts clean.

## Flow

1. Run `alive demo deactivate`. The CLI reads demo-state.json, atomically writes `~/.config/alive/world-root` to the cached `previous_world_root` value, and clears the cache.
2. Branch on the JSON envelope:

   * `success: true`, `result.status == "ok"` -> Print the `rendered_block` (a "restart Claude Code" surface). The world-root pointer has been flipped back; demo-state has been cleared.
   * `success: true`, `result.status == "no_demo_active"` -> Print the `rendered_block`. Nothing was done; no demo was active.
   * `error.code == "no_previous_world"` -> The active demo was activated cold (no live world existed at activation time). Restoring None would leave the pointer at the demo. Print the `rendered_block` (cold-demo surface) and route the user to `/alive:demo` (create a new demo) or have them set the world-root pointer manually.
   * `error.code == "deactivate_error"` -> Surface `error.message` inline.

## Example invocation

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo deactivate
```

## Success surface

```
╭─ 🐿️ deactivated -- restart Claude Code
│  Demo world deactivated:
│    wld_<ulid>  -  <label>
│
│  World-root restored to:
│    /Users/.../alivecontext
│
│  Restart Claude Code (Cmd+Q + relaunch) so the session picks up
│  the restored world index.
╰─
```

## No-demo-active surface

```
╭─ 🐿️ deactivate -- no demo active
│  No demo world is currently active. Nothing to do.
╰─
```

## Cold-demo surface

```
╭─ 🐿️ deactivate -- cold demo
│  The active demo world has no cached previous world-root.
│  This happens when the demo was activated against an empty pointer.
│
│  Active demo: wld_<ulid> -- <label>
│
│  Either run /alive:demo to create a new demo, or set the
│  world-root pointer manually to a real world.
╰─
```

## Atomicity

The pointer flip is the single commit point. `stages/deactivate.run_deactivate`:

1. Acquires the demo-state lock.
2. Reads demo-state for `active_world` and `previous_world_root`.
3. If a previous world is cached, calls `_world_root_io.write_world_root_file(previous)`. THIS is the commit.
4. Clears `active_world` and `previous_world_root` in demo-state.
5. Releases the lock; demo-state is rewritten on clean exit.

A failure at step 3 leaves demo-state untouched (the lock is held; `with_locked_state` does not save on exception). The user can retry without a self-heal divergence.
