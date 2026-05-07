# alive:demo activate

Sibling skill page invoked when `$ARGUMENTS[0] == "activate"` and `$ARGUMENTS[1]` carries the world ref.

Re-activates a previously-promoted demo world by re-running the tail of the Stage 5 transaction (steps 8 / 9 / 10) against an existing `<base>/wld_<ULID>/` directory. The pre-check fires before the commit so a live world with uncommitted work is surfaced for explicit confirmation.

## Flow

1. Run `alive demo activate <ref>` (no `--confirm` yet). The CLI resolves the ref and runs the activation pre-check.
2. Branch on the JSON envelope:

   * `success: true` plus `result.status == "ok"` -> ref resolved cleanly AND the live world had no uncommitted findings AND the activation completed in one shot. Print the `rendered_block` (a "restart Claude Code" surface) and stop.
   * `error.code == "needs_confirmation"` -> the pre-check found unsaved work on the current world. Print the `rendered_block` (an "activation -- uncommitted work" warning) and dispatch `AskUserQuestion` with options `Activate anyway`, `Cancel`. On `Activate anyway`, re-run the same command with `--confirm` appended.
   * `error.code == "ambiguous_ref"` -> more than one world matched. Print the `rendered_block` (the picker surface). Dispatch `AskUserQuestion` with one option per candidate (label and ULID) plus `Cancel`. On selection, re-run `alive demo activate <picked-ulid>`. Recursive resolve handles the result.
   * `error.code == "not_found"` -> no match. Print the squirrel's own block carrying `error.message` and `error.hint` (e.g. `Run alive demo list to see available demo worlds.`).
   * Other errors (`activate_error`, etc.) -> surface `error.message` and `error.hint` inline.

3. On a successful activation, the CLI's `rendered_block` is the post-activation restart surface. Do not append further blocks; the user needs to restart Claude Code (or `/alive:world`) for the new pointer to take effect.

## Example invocations

```bash
# Resolve by label, succeed on a clean live world.
$ALIVE_PLUGIN_ROOT/bin/alive demo activate alex-boring-angel-investor

# Resolve by ULID prefix; supply --confirm to acknowledge findings.
$ALIVE_PLUGIN_ROOT/bin/alive demo activate 01j5hk7y --confirm

# Full ULID also accepted.
$ALIVE_PLUGIN_ROOT/bin/alive demo activate wld_01j5hk7yvgkw0zfg9k3vh4p9xv
```

## Resolution rules (`lib.resolve_ref`)

1. **Exact label match.** Case-sensitive comparison against each record's `label` field. One match -> resolve. Multiple matches -> `AmbiguousMatch`.
2. **ULID prefix match.** Case-insensitive prefix against each record's `ulid` (both the `wld_<prefix>` form and the bare `<prefix>` are tested). Prefix must be at least 3 characters; shorter refs that fail step 1 raise `LookupError` with a friendly message.
3. **No match.** Raise `LookupError` with the ref echoed.

The interactive picker (`AskUserQuestion`) is squirrel-level. The worker emits the `ambiguous_ref` envelope plus `rendered_block` and `candidates`; the squirrel drives the choice.

## Activation pre-check

Three predicates from `lib.activation_pre_check`:

1. `<world>/.alive/_squirrels/*.yaml` has `saves: 0` AND its `transcript:` file exceeds 4 KB.
2. `<walnut>/_kernel/log.md` mtime newer than `<walnut>/_kernel/now.json` mtime.
3. `<world>/.alive/_squirrels/*.yaml` has `saves: 0` AND non-null `recovery_state`.

Findings render as bullet entries inside the warning surface. The user MUST explicitly confirm via `AskUserQuestion` before the squirrel re-runs the command with `--confirm`.

## Steps run on `--confirm`

The CLI's `_activate_handler` calls `stages/activate_existing.run_activate(record, confirm=True)`. The ordering is locked so that user-visible metadata only changes AFTER the world-root pointer commits:

1. **Stage 5 step 9**: cache previous world-root pointer into `previous_world_root`; populate `active_world` in demo-state.json. State-layer exceptions (`SchemaVersionMismatch`, `FlockTimeoutError`, `DemoStateError`) propagate UNCHANGED so the CLI emits the documented envelopes (`schema_version_mismatch` / `lock_timeout` / `demo_state_corrupt`).
2. **Stage 5 step 10**: atomic write of `~/.config/alive/world-root` via `_world_root_io.write_world_root_file`. THE single commit point.
3. **Build-log refresh** (Stage 5 step 8 analogue): rewrite the frontmatter `activated_at` line and append a `Re-activations` bullet to `_demo-build-log.md`. This runs ONLY AFTER step 10 succeeds. A failure here is post-commit and surfaces as `result.build_log_warning` in the success envelope rather than rolling back a successful activation.

Crash-consistency invariants:

* A failure at step 9 leaves both the world-root pointer and the build-log file untouched. The caller's prior live world is unchanged; demo-state's self-heal converges any half-staged metadata back to the pointer on the next load.
* A failure at step 10 leaves the build log untouched (it has not been rewritten yet). demo-state may name the new world after step 9 but the pointer still names the old one; self-heal reconciles on next load.
* A failure at the build-log refresh leaves the world genuinely activated at the pointer level. The audit-trail entry can be added by hand or via a future `/alive:demo touch` flow; the squirrel surfaces `build_log_warning` to the user.

## Already-active short-circuit

Per codex review round 3, re-activating a world that is ALREADY the live world-root is a no-op:

* `result.status == "already_active"` -> Print the `rendered_block` (an "already active" surface). No demo-state writes; `previous_world_root` is preserved. This prevents the bug where re-activating the live demo would overwrite `previous_world_root` with the demo's own path, destroying the cached path back to the original live world.

## Restart surface (post-success)

The CLI's success `rendered_block`:

```
╭─ 🐿️ activated -- restart Claude Code
│  Demo world activated:
│    wld_<ulid>  -  <label>
│    <path>
│
│  The session-start hook injects WORLD_INDEX once per session. To pick
│  up the new world index, restart Claude Code (Cmd+Q + relaunch).
│
│  Or: run /alive:world to re-render against the new pointer.
╰─
```

Print this verbatim. The squirrel cannot magic-relaunch Claude Code in v3.2 (deferred to v4 per spec).

### Build-log warning (post-commit)

When the build-log refresh fails AFTER the world-root pointer commits, the activation is genuinely successful at the pointer level but the audit-trail entry in `_demo-build-log.md` could not be written. The CLI surfaces this in two places:

* `result.build_log_warning` carries the underlying error message.
* `rendered_block` includes a `WARNING: <message>` section under the standard restart copy.

The squirrel prints the rendered_block verbatim (the warning is already inside it). The world IS active; the user can repair the build log manually if downstream tooling needs an accurate `activated_at`.
