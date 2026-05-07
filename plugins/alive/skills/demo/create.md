# create -- custom-path orchestrator

Sibling skill markdown for `/alive:demo`. Walks the human through the
custom (persona-driven) path end-to-end: persona intake -> partial-dir
mint -> Stage 0 spine -> Stage 1 anchor confirmation -> Stage 2 entity
prose -> Stage 3 timeline -> Stage 4 insights -> Stage 5 activation
transaction -> post-activation restart cue.

This file is prose for the **dispatching squirrel** (you). Every user
decision flows through `AskUserQuestion`; every status surface is a
bordered block. The Python side lives in `cli_register.py`,
`stages/stage0.py`, `stages/stage1.py`, `stages/stage2..5.py`, and
`scaffold.py`. This markdown ties them together in lockstep.

---

## When this runs

Right after the no-args router in `SKILL.md` fires `AskUserQuestion`
and the human picks `Custom (persona-driven)`. Before any
`partial_generations[*]` entry exists in demo-state.json, before any
LLM dispatch.

If the human picked `Preset (sandbox-testing)` instead, control stays
in `SKILL.md` and never reaches this file.

---

## Visual contract (mandatory)

Every block printed inline by this skill follows the canonical ALIVE
shape from `plugins/alive/CLAUDE.md` § "Visual Conventions":

- Three characters: `╭ │ ╰`. Open right side.
- `🐿️` after the top corner.
- `▸` for prompts the human must answer.
- `>` for system reads.

Numbered options inside a block are advisory; the actual choice flows
through `AskUserQuestion`, never inline `1. / 2. / 3.` answer text.

No em dashes, no en dashes, no horizontal-bar dashes anywhere in
prose surfaces. The voice contract is enforced by the test snapshot on
this file.

---

## Section 1: Persona intake

Render the intake block inline, then dispatch `AskUserQuestion` for
the size hint and the persona text:

```
╭─ 🐿️ /alive:demo custom path
│
│   Two inputs to scaffold a demo world:
│
│    1. A one-paragraph persona description
│       (who they are, what they work on, who shows up in
│        their day, what is on their mind right now)
│
│    2. A size hint
│        small   ~3 anchor moments,  ~3 walnut roster
│        medium  ~5 anchor moments,  ~5 walnut roster
│        large   ~8 anchor moments, ~10 walnut roster
│
│   ▸ Pick a size, then paste the persona description.
╰─
```

Fire `AskUserQuestion` for the size with options
`Small (~90 seconds)`, `Medium (~3 minutes)`, `Large (~5 minutes)`,
`Cancel`. On `Cancel`, render the cancellation block:

```
╭─ 🐿️ cancelled
│
│   No demo world created. Run /alive:demo again any time.
╰─
```

Then ask for the persona description via a follow-up prose-input
prompt (`AskUserQuestion` with one open-text option, or a follow-up
turn if the runtime needs prose input separately):

```
╭─ 🐿️ persona description
│
│   ▸ Paste one paragraph (60-600 words) describing the person:
│     who they are, what ventures or experiments they run, who
│     shows up most often (partner, collaborators, family), and
│     what is on their plate this quarter.
│
│   No real public-figure surnames. Synthetic surnames only.
╰─
```

Persist the raw text to a temp path so the CLI bridge can read it
back deterministically. The convention is:

```
~/.config/alive/.demo-pending-description-<short-stamp>.md
```

where `<short-stamp>` is an ISO 8601 UTC string with colons replaced
by dashes (e.g. `2026-04-30T072315Z`). The CLI bridge reads this path
verbatim; the orchestrator does NOT pass the description over the
command line directly (size hint argument is fine; the description
body is too long for argv on edge cases).

---

## Section 2: Prepare

Once the description is on the temp path and the size is picked,
invoke the prepare CLI:

```bash
"$ALIVE_PLUGIN_ROOT/bin/alive" demo create prepare \
  --description-file <abs-temp-path> \
  --size <small|medium|large>
```

The CLI handler atomically:

1. Validates the description file exists and is non-empty.
2. Validates the size enum.
3. Mints a fresh `<base>/wld_<ulid>.partial/` directory via
   `lib.mint_partial_dir` (with `os.makedirs(..., exist_ok=False)`).
4. Persists the description verbatim at
   `<partial>/_input/persona-description.md`.
5. Stages a `partial_generations[*]` entry in demo-state.json under
   the demo-state flock with `stage: "0_spine"`,
   `status: "in_progress"`, plus the new optional fields (`size`,
   `description_path`, `partial_dir`).
6. Emits the standard CLI envelope.

Render the `rendered_block` field verbatim. Capture the `partial_dir`
and `partial_ulid` from the JSON envelope; both are needed for every
subsequent stage hand-off.

On failure (`description_not_found`, `description_empty`,
`invalid_size`, `partial_dir_exists`, `partial_dir_unwritable`,
`schema_version_mismatch`, `lock_timeout`, `demo_state_corrupt`),
surface a bordered block carrying the CLI's `error.message` and
`error.hint` and stop. Do NOT proceed to Stage 0 dispatch on a
prepare failure.

---

## Section 3: Stage 0 driver loop

Stage 0 is the spine generator. It produces
`<partial>/_stage_outputs/spine.json` with the persona, walnut roster,
people roster, anchor moments, bundle distribution, and time span.

The orchestrating squirrel owns the Agent tool call. The Python
helper at `stages/stage0.py:run_stage0` blocks on a `dispatch`
callable that the squirrel supplies. The dispatch callable closes over
the runtime's Agent tool:

```python
def dispatch(prompt, *, subagent_type, description):
    # Squirrel-side: fire ONE Agent tool call with these arguments and
    # return the one-line acknowledgement string. The actual artefact
    # is the file the subagent writes to disk.
    ...
```

Render the dispatch status block before firing:

```
╭─ 🐿️ stage 0 dispatch
│
│   single subagent (sequential, full-spine coherence)
│
│   Output: _stage_outputs/spine.json
╰─
```

Call `run_stage0` with `surface_failure_blocks=True` so a
double-failure returns a structured envelope (rather than raising
`Stage0RetryExhausted`). The envelope shape is what the orchestrator
needs to render the 3-option AskUserQuestion picker; it also auto-
stamps `failed_at_stage = "0_spine"` + `failed_reason =
"validation_double_failure"` on the partial-generation row in
demo-state.json (the helper drives the state mutation; the
orchestrator does not call `mark_partial_failed` itself).

The CLI prepare handler stored the user's size choice verbatim
(`"small"`, `"medium"`, `"large"`), but Stage 0's spine prompt
expects the internal selector (`"S"`, `"M"`, `"L"`). Map at the
orchestrator boundary via `lib.spine_size_for(size_hint)`:

```python
spine_size = lib.spine_size_for(size)  # "small" -> "S", etc.
result = stage0.run_stage0(
    description=description_text,
    size=spine_size,
    partial_dir=partial_dir,
    world_root=partial_dir,
    dispatch=dispatch,                # the squirrel's Agent tool closure
    plugin_root=plugin_root,
    surface_failure_blocks=True,
)
```

Inside the runner, each Agent tool dispatch fires with:

- `subagent_type: "general-purpose"`
- `description: "alive-demo stage 0 spine"` (or
  `"alive-demo stage 0 description summarise"` for the optional
  pre-summariser dispatch on long persona text).
- `prompt`: the body returned by `stage0.render_spine_prompt(...)`,
  already wrapped in the canonical CONTEXT/TASK envelope by
  `run_stage0`.

Wait for the one-line acknowledgement. Never read disk before the
runtime returns; `run_stage0` does the file read on your behalf.

The `run_stage0` runner internally:

1. Persists the persona description at
   `_input/persona-description.md` (idempotent with the prepare CLI's
   write).
2. Triages length: if the description exceeds the token budget, it
   dispatches a summariser subagent first.
3. Dispatches the spine subagent.
4. Pre-flights `spine.json` via `preflight_spine`.
5. Runs `validate.py` Stage 0 coherence checks.
6. On first-pass failure: re-renders the prompt with feedback and
   dispatches ONE retry.
7. On second failure (with `surface_failure_blocks=True`): returns a
   structured envelope carrying
   `failure_mode == "validation_double_failure"`,
   `rendered_block` (the bordered-block surface), the second-attempt
   error list, the raw spine.json output path, and the partial dir
   path.

On a clean (`failure_mode` not present in result) return, surface
success inline:

```
╭─ 🐿️ stage 0 frozen
│
│   spine.json on disk; preflight + coherence pass.
│   Next: stage 1 anchor confirmation.
╰─
```

The `run_stage0` runner advances the partial-generation row's
`stage` field to `"1_anchor"` via `state.advance_partial_stage` on
success, so `alive demo status` and `alive demo resume` reflect the
new in-flight stage automatically. The orchestrator does NOT need
to call `state.advance_partial_stage` itself for any stage; the
freeze helpers (`stage1.freeze_anchors`, `stage2.freeze_stage`,
`stage3.freeze_stage`, `stage4.freeze_stage`) each advance the row
to the next in-flight stage on a successful freeze.

On a double-failure envelope (`result.get("failure_mode") ==
"validation_double_failure"`), print `result["rendered_block"]`
verbatim, then fire `AskUserQuestion` with the three options:

- `Accept partial` (proceed with current spine despite remaining
  errors)
- `Retry full` (re-loop the top of this section: re-dispatch Stage 0
  from scratch)
- `Cancel` (stop the orchestrator; the partial directory + the
  `failed_at_stage: "0_spine"` marker stay on disk so a later
  `alive demo resume` can offer a retry)

The squirrel owns the `AskUserQuestion` call; the helper has already
mutated demo-state. Do NOT call `state.mark_partial_failed` from
prose -- the helper's
`lib.report_validation_double_failure` drives that write atomically
under the demo-state flock.

---

## Section 4: Stage 1 anchor confirmation hand-off

Once Stage 0 freezes (`spine.json` on disk + preflight passed), hand
control to `anchor_confirm.md`. Render this read-line first so the
human sees the transition:

```
> spine.json on disk; entering anchor_confirm.md
```

Then follow `anchor_confirm.md` § "When this runs" verbatim:
the loop walks every spine anchor moment one at a time, fires
`AskUserQuestion` per moment with `Accept`, `Regenerate`,
`Edit prose`, `Replace`, and on completion emits
`anchor_moments.json` with `frozen: true`.

After `anchor_confirm.md` returns control, render the transition
inline:

```
╭─ 🐿️ stage 1 frozen
│
│   anchor_moments.json on disk with frozen: true.
│   Next: stage 2 entity prose dispatch.
╰─
```

If the human cancels inside `anchor_confirm.md`, the cancel branch
hands back here; stop the chain. The partial directory stays on
disk as a resumable handle so the human can come back to it via
`alive demo resume <ref>` (or destroy it explicitly via
`alive demo delete <ref>`). The orchestrator does NOT delete the
partial from this branch and does NOT call
`state.mark_partial_failed`; the partial-generations row stays at
its current `stage` field with `status = "in_progress"`.

---

## Section 5: Stage 2 entity prose driver

Stage 2 fans out one Agent tool call per spine entity (walnut,
person, bundle). Follow the existing driver loop documented in
`SKILL.md` § "Stage 2 -- entity prose subagents (parallel)" verbatim.

Numbered hand-off:

1. `alive demo stage2 prepare --partial <partial>` -> dispatch
   descriptors.
2. Render the dispatch status block; fire all Agent tool calls in a
   single message in parallel for the first batch of `DEFAULT_BATCH_SIZE`.
3. `alive demo stage2 collect-validate --partial <partial>`.
4. On clean: `alive demo stage2 freeze --partial <partial>`.
5. On findings: `alive demo stage2 retry-dispatch --partial <partial>`,
   re-fire, re-collect-validate, then freeze. Second failure surfaces
   the 3-option AskUserQuestion per the SKILL.md pattern.

Do not re-implement the loop here; the SKILL.md section is canonical.

---

## Section 6: Stage 3 timeline driver

Hand off to `SKILL.md` § "Stage 3: timeline materialisation (single
subagent)". Numbered hand-off:

1. `alive demo stage3 prepare --partial <partial>` -> single
   dispatch descriptor.
2. Fire ONE Agent tool call.
3. `alive demo stage3 collect-validate --partial <partial>`.
4. Freeze on clean; retry-dispatch on findings; second failure
   surfaces the 3-option AskUserQuestion.

Same shape as Stage 2 but a single subagent rather than a fan-out.

---

## Section 7: Stage 4 insights driver

Hand off to `SKILL.md` § "Stage 4: insights synthesis (single
subagent)". Numbered hand-off:

1. `alive demo stage4 prepare --partial <partial>`.
2. Fire ONE Agent tool call.
3. `alive demo stage4 collect-validate --partial <partial>`.
4. Freeze on clean; retry-dispatch on findings; second failure
   surfaces the 3-option AskUserQuestion.

After freeze, all four LLM stages are done. Stage 5 is deterministic
Python.

---

## Section 8: Stage 5 activation

Hand off to `SKILL.md` § "Calling Stage 5 from this skill". Numbered
hand-off:

1. `alive demo stage5 prepare --partial <partial>` for the dry-run
   plan + activation pre-check.
2. If the response carries `plan.needs_confirmation == true`,
   surface the bordered block from the envelope and fire
   `AskUserQuestion` with `Activate anyway`, `Cancel`. On
   `Cancel`, render the cancellation block and stop.
3. `alive demo stage5 run --partial <partial> [--confirm]`. Drop
   `--confirm` when the pre-check returned no findings.
4. The 11-step transaction runs `os.rename(<partial>, <world>)`,
   generates preferences/squirrels/completed.json, installs the
   Stage 0-4 outputs into the canonical walnut layout, runs
   `project.py` + `generate-index.py`, writes the build log,
   stages demo-state.json, and as the final commit point flips
   `~/.config/alive/world-root` via
   `_world_root_io.write_world_root_file`.

After step 11 commits, render the post-activation block (next
section). On any failure before step 11, the world-root pointer is
guaranteed unchanged.

---

## Section 9: Post-activation

After Stage 5 commits successfully, render the restart block from
`SKILL.md` § "Restart-Claude-Code instruction (post-activation)":

```
╭─ 🐿️ activated -- restart Claude Code
│
│   Demo world activated:
│     <ulid>  ·  <label>
│     <path>
│
│   The session-start hook injects WORLD_INDEX once per session. To
│   pick up the new world index, restart Claude Code (Cmd+Q +
│   relaunch).
│
│   Or: run /alive:world to re-render against the new pointer.
╰─
```

The orchestrator's job ends here. Subsequent context-grounded queries
against the new world (the proof moment) are run by the human in a
fresh session.

---

## Failure surfaces

Each stage's double-failure is rendered as the 3-option block
documented above. The orchestrator never fires `AskUserQuestion` from
inside a Python helper; the squirrel does, after the helper returns
the failure envelope up.

demo-state mutation on failure is owned by the per-stage Python
helpers, not by the prose. The helpers fan out the same atomic write
under the demo-state flock:

- Stage 0: `run_stage0(..., surface_failure_blocks=True)` calls
  `lib.report_validation_double_failure` which stamps
  `failed_at_stage = "0_spine"` +
  `failed_reason = "validation_double_failure"` on the matching
  partial row.
- Stage 2/3/4: each stage's `surface_double_failure(...)` helper
  writes the same markers (with the appropriate `failed_at_stage`
  label) before returning the failure envelope.
- Stage 5: `scaffold.activate(..., surface_failure_blocks=True)`
  stamps `failed_at_stage = "5_promote"` on a projection / atomic-
  write failure.

The orchestrator does NOT call `state.mark_partial_failed` directly
from prose. Trust the helper's return envelope: it carries
`rendered_block`, `failure_mode`, and the data needed to fire
AskUserQuestion.

The 3-option AskUserQuestion wording stays consistent across all
stages: `Accept partial`, `Retry full`, `Cancel`. On `Cancel`, the
partial directory is left on disk (the human can inspect it or run
`alive demo delete <ref>` later); on `Retry full`, loop back to the
top of the failing stage's section; on `Accept partial`, continue
the chain.

---

## References

- `plugins/alive/skills/demo/SKILL.md` -- router, dispatch
  primitives, Stage 2-5 driver shapes, validation hook, post-
  activation restart block.
- `plugins/alive/skills/demo/anchor_confirm.md` -- Stage 1 UX loop.
- `plugins/alive/skills/demo/stages/stage0.py` -- `run_stage0`,
  `render_spine_prompt`, dispatch contract.
- `plugins/alive/skills/demo/stages/stage1.py` -- anchor envelope IO,
  `freeze_anchors`.
- `plugins/alive/skills/demo/stages/stage2.py`,
  `stages/stage3.py`, `stages/stage4.py` -- per-stage CLI handlers.
- `plugins/alive/skills/demo/scaffold.py` -- Stage 5 11-step
  activation transaction.
- `plugins/alive/skills/demo/cli_register.py` -- `alive demo create
  prepare` handler, demo-state staging, partial-dir mint surface.
- `plugins/alive/skills/demo/lib.py` -- `mint_partial_dir`,
  `format_block`, `derive_label`, `new_world_ulid`.
- `plugins/alive/skills/demo/state.py` -- `with_locked_state`,
  `mark_partial_failed`, `_validate_partial` (custom-path optional
  fields).
- `plugins/alive/CLAUDE.md` -- visual contract.
- `.flow/specs/fn-2-2zz.md` -- the epic plan; § "Approach" is
  canonical for the activation transaction.
