# alive demo resume

Failure-recovery walkthrough. The companion to the three failure-mode handlers in `lib.py` (`report_validation_double_failure`, `report_projection_failure`, `report_atomic_write_failure`). Read this when a `/alive:demo` run errored out and the human wants to pick up where they left off.

## What gets flagged for resume

A partial generation (`partial_generations[*]` in `~/.config/alive/demo-state.json`) is resumable when:

- `status` is `in_progress` (so it's not already promoted, not explicitly written off).
- `failed_at_stage` is set to one of `0_spine`, `2_entities`, `3_timeline`, `4_insights`, `5_promote`.

The failure handlers set the marker pair atomically under flock when their corresponding mode triggers:

| Handler | failed_reason | failed_at_stage |
|---|---|---|
| `report_validation_double_failure` (15a) | `validation_double_failure` | `<stage>_<label>` (e.g. `2_entities`) |
| `report_projection_failure` (15b) | `projection_failure` | `5_promote` |
| `report_atomic_write_failure` (15c) | (no state mutation; see below) | (no state mutation) |

Mode 15c does not mutate state because the mutation is what failed. The user surface explicitly notes that demo-state.json is intact.

## How to invoke

```bash
# List every resumable partial (no arg).
alive demo resume

# Get the retry plan for a specific partial (one arg).
alive demo resume wld_01h7m20...
```

## Three response shapes

### Nothing to resume

```
╭─ 🐿️ nothing to resume
│  No partial generations are flagged for resume.
│
│  Run `alive demo list` to see all partial + active worlds.
│  Run /alive:demo to start a fresh generation.
╰─
```

The CLI returns `{"success": true, "resumable": [], "rendered_block": ...}`. Print the block, suggest `/alive:demo` if the human wants a fresh world.

### Exactly one resumable

The CLI returns:

```json
{
  "success": true,
  "partial_id": "wld_01h...",
  "label": "alex-boring",
  "failed_at_stage": "2_entities",
  "failed_reason": "validation_double_failure",
  "failed_at": "2026-04-29T14:00:00Z",
  "suggested_action": "Re-run `alive demo stage2 retry-dispatch ...`",
  "rendered_block": "..."
}
```

Print the block. Then drive the suggested action:

- For LLM stages (0 / 2 / 3 / 4): re-fire the subagent dispatch the suggested action references. The CLI cannot do this autonomously because the dispatch callable lives in the runtime.
- For Stage 5: re-run `alive demo stage5 run --partial <partial-dir>` (or `alive demo activate <ref>` for an already-renamed world).
- For atomic-write failures: the human resolves the disk / permission cause, then re-runs the failing command. demo-state.json is intact.

After the retry succeeds, the per-stage freeze (or `step_9_stage_demo_state` for stage 5) overwrites the partial's marker fields, clearing the failure pair implicitly.

### Multiple resumable, no partial_id

```
╭─ 🐿️ multiple resumable partials
│  3 resumable partial generation(s):
│
│    1. alex-boring  -  wld_01h...  (failed at 2_entities, reason: validation_double_failure)
│    2. nova-station  -  wld_01g...  (failed at 5_promote, reason: projection_failure)
│    3. some-other  -  wld_01f...  (failed at 0_spine, reason: validation_double_failure)
│
│  Pick one and re-run:
│    alive demo resume <ulid>
╰─
```

Render the picker via `AskUserQuestion`. Each option maps to its ulid. The user picks; squirrel re-runs `alive demo resume <ulid>` and proceeds with the single-resumable flow above.

## Abandoning a partial

If the user does not want to retry, the partial directory and demo-state entry can be cleared:

```bash
alive demo delete <ulid>
```

`delete` resolves the ref via the same 3-step fallback as `activate` (label / ULID prefix / ambiguous picker) and refuses if the world is currently active. Because resumable partials are not active (they failed mid-pipeline), delete proceeds after `--confirm`.

## Mode 15c: atomic-write failure recovery

This mode is qualitatively different. The failure handler does NOT update demo-state.json (because the mutation that failed IS the demo-state mutation). The block tells the human:

- Demo-state.json is intact.
- Common errno causes (28 ENOSPC, 30 EROFS, 13 EACCES) and how to triage.
- Re-run the failing command once the cause is resolved.

There's no `failed_at_stage` marker for 15c, so `alive demo resume` will not see it. The recovery is the human resolving the disk / permission issue and re-running the prior `alive demo` command verbatim.

## Issue tracker

If the same input keeps failing across retries, file a bug at `https://github.com/alivecontext/alive/issues/new`. Include:

- The bordered failure block printed by the CLI.
- The partial directory path (the block surfaces this).
- The raw subagent output path (15a only).

The handlers all reference this URL inline so the human can copy it.
