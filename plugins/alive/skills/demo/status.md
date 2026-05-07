# alive:demo status

Sibling skill page invoked when `$ARGUMENTS[0] == "status"`. Prints a 5-7 line bordered block summarising the active demo world (if any), the cached previous world-root pointer, and a hint pointing at deactivate or create.

## Flow

1. Run `alive demo status`. The CLI loads demo-state.json, runs the self-heal pass against the live world-root pointer, and emits a JSON envelope with `success`, `schema_version`, `active_world`, `previous_world_root`, `partial_generations`, and a fully-rendered `rendered_block`.
2. Print the `rendered_block` value verbatim.

## Example invocation

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo status
```

## Output shapes

### Active demo

```
╭─ 🐿️ demo status
│  ulid:                wld_01j5hk7yvgkw0zfg9k3vh4p9xv
│  label:               alex-boring-angel-investor
│  path:                /Users/.../.alive-demos/wld_01j5hk7yvgkw0zfg9k3vh4p9xv
│  activated_at:        2026-04-29T14:00:00Z
│  previous_world_root: /Users/.../alivecontext
│  hint:                /alive:demo deactivate to restore the previous world
╰─
```

### No active demo

```
╭─ 🐿️ demo status
│  active_world:        (none)
│  previous_world_root: (none)
│  hint:                /alive:demo to create a new demo world
╰─
```

## Errors

Same envelope vocabulary as `list.md`:

* `error.code == "schema_version_mismatch"` (exit 3): demo-state.json is older than the running plugin. Surface the version-mismatch UX from `SKILL.md` and offer `/alive:demo reset`.
* `error.code == "lock_timeout"` (exit 5): another `/alive:demo` session is updating demo-state. Wait and retry.
* `error.code == "demo_state_corrupt"` (exit 1): demo-state.json is unreadable; route the user to `/alive:demo reset`.

## Self-heal note

`status` is the canonical "what does the system think is active?" surface. The CLI runs `state.load_state()`, which executes the world-root self-heal:

* If `active_world.path` matches `~/.config/alive/world-root`, no change.
* If they disagree and the pointer names another demo world (recognized by `<world>/.alive/_demo-build-log.md`), demo-state rebuilds `active_world` from the build-log frontmatter.
* If the pointer names a non-demo world or is missing, demo-state's `active_world` is cleared.

So `alive demo status` always reports the truth: world-root is authoritative, demo-state is a read-through cache.
