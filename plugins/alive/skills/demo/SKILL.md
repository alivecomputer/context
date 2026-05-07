---
name: alive:demo
description: "Generate a believable, lived-in ALIVE world from a free-text persona description (custom path) or a deterministic sandbox preset. Routes the create/list/activate/deactivate/delete/status surface and orchestrates the 5-stage subagent generation pipeline."
user-invocable: true
---

# alive:demo -- generative demo-world skill

This is the router. Route on `$ARGUMENTS[0]` to a sibling `.md` file (when present) or to the interactive `create` flow. Six subcommands + an interactive default:

| `$ARGUMENTS[0]` | Routes to | Status |
| --- | --- | --- |
| _empty_ / `create` | Interactive: preset vs. custom; `create.md` for the custom path | wired |
| `list` | `list.md` | wired |
| `activate <ref>` | `activate.md` | wired |
| `deactivate` | `deactivate.md` | wired |
| `delete <ref>` | `delete.md` | wired |
| `status` | `status.md` | wired |
| `reset` | rebuilds demo-state.json (recovery from schema mismatch) | wired |
| `spike-test` | retired (fn-2-2zz.1 spike) | removed below |

All five non-create sibling `.md` files (`list.md`, `activate.md`, `deactivate.md`, `delete.md`, `status.md`) invoke their CLI subcommand under `$ALIVE_PLUGIN_ROOT/bin/alive demo <name>` and print the CLI's `rendered_block` field verbatim. `create.md` is the custom-path orchestrator: it walks Stage 0 -> Stage 1 hand-off -> Stage 2/3/4/5 driver chain end-to-end. The interactive default below routes the human into either the preset path or `create.md` based on their pick.

The router emits bordered blocks INLINE as markdown — matches the convention in `plugins/alive/skills/world/SKILL.md` and `plugins/alive/skills/save/SKILL.md`. There is no Python `render_block` bridge: the LLM emits its own markdown; only the CLI uses `lib.format_block` (per the codex review which rejected forcing LLM formatting from Python).

User choices use `AskUserQuestion`, never inline `1. / 2. / 3.` answer text. Numbered options inside a bordered block are advisory; the actual decision flows through the question tool.

---

## State + paths (canonical)

| Concern | Path | Override? |
| --- | --- | --- |
| Demo state file | `~/.config/alive/demo-state.json` | NO. Tests set `HOME` to `tmp_path`. |
| Demo state lock | `~/.config/alive/.demo-state.lock` | NO. |
| World-root pointer | `~/.config/alive/world-root` | NO — owned by `_world_root_io.write_world_root_file` (#64). |
| Demo worlds + partials | `~/.alive-demos/` | YES via `$ALIVE_DEMO_BASE_DIR`. |

`$ALIVE_DEMO_BASE_DIR` is the only env override the demo skill reads. It controls where promoted worlds (`<base>/wld_<ulid>/`) and in-flight partials (`<base>/wld_<ulid>.partial/`) live. The state file and the world-root pointer stay canonical so the existing system-wide readers (`_common.py`, `alive-common.sh`, `world/SKILL.md`) keep working unchanged.

---

## Router — interactive default (`/alive:demo` with no args)

When invoked with no subcommand, render this block inline and dispatch via `AskUserQuestion`:

```
╭─ 🐿️ /alive:demo
│
│   No demo subcommand given. Two paths:
│
│    1. preset       (sandbox-testing, deterministic, ~10s)
│    2. custom       (persona-driven, LLM-backed, ~1-2 min)
│
│   What would you like to do?
╰─
```

Then:

- `AskUserQuestion` with options `Preset (sandbox-testing)`, `Custom (persona-driven)`, `Cancel`.
- On `Preset` -> the "Preset path (sandbox testing)" section below; the squirrel calls `alive demo preset run --preset realistic-seeded` after the activation pre-check confirmation.
- On `Custom` -> jump into `create.md` (sibling of this file). `create.md` owns the persona intake, partial-dir mint, Stage 0 driver loop, Stage 1 hand-off to `anchor_confirm.md`, and the Stage 2/3/4/5 driver chain end-to-end.
- On `Cancel` -> render the cancellation block and stop:

```
╭─ 🐿️ cancelled
│
│   No demo world created. Run /alive:demo again any time.
╰─
```

When routing to `create.md`, render this one-line cue inline before jumping in so the human sees the routing decision:

```
> routing to create.md (custom persona path)
```

---

## Subcommand routing

The CLI is the only path that touches `demo-state.json` and the world-root pointer. Each subcommand below shells out to `$ALIVE_PLUGIN_ROOT/bin/alive demo <name>` and prints the CLI's `rendered_block` field verbatim. Sibling `.md` files carry the per-subcommand UX detail; SKILL.md keeps the per-subcommand entry points short.

### `list`

Routes to `list.md`. Prints a 6-column bordered-block table of every promoted demo world plus partial generations. Active world is flagged with `*active` in the STATUS column.

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo list
```

See `list.md` for envelope shape and error handling.

### `activate <ref>`

Routes to `activate.md`. Resolves `<ref>` via 3-step fallback (exact label, ULID prefix, ambiguous-match envelope), runs the activation pre-check, and (with `--confirm`) runs the tail of the Stage 5 transaction (build-log refresh + step 10 demo-state staging + step 11 world-root commit) against the existing world.

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo activate <ref>
$ALIVE_PLUGIN_ROOT/bin/alive demo activate <ref> --confirm
```

`<ref>` is the lowercase derived label (e.g. `alex-boring-angel-investor`) OR a ULID prefix of at least 3 characters (e.g. `01j5hk7y` or `wld_01j5hk7y`).

The squirrel drives the picker on `error.code == "ambiguous_ref"`: render the picker block from the envelope, dispatch `AskUserQuestion`, re-run with the picked ULID.

The squirrel drives confirmation on `error.code == "needs_confirmation"`: render the warning block, dispatch `AskUserQuestion` (`Activate anyway`, `Cancel`), re-run with `--confirm`.

See `activate.md` for full flow and resolution rules.

### `deactivate`

Routes to `deactivate.md`. Restores the world-root pointer to the cached `previous_world_root` value, then clears `active_world` and `previous_world_root` in demo-state.json.

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo deactivate
```

Three result branches: `ok` (pointer flipped, restart Claude Code), `no_demo_active` (nothing to do), and `no_previous_world` (cold demo; user must create another or set the pointer manually).

See `deactivate.md` for the cold-demo case and atomicity contract.

### `delete <ref>`

Routes to `delete.md`. Resolves `<ref>` (same rules as activate), refuses on the currently-active world, and (with `--confirm`) calls `shutil.rmtree`. Without `--confirm`, the CLI returns `error.code == "needs_confirmation"` carrying an irreversibility surface so the squirrel dispatches `AskUserQuestion` before the destructive call.

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo delete <ref>          # dry run; surfaces irreversibility block
$ALIVE_PLUGIN_ROOT/bin/alive demo delete <ref> --confirm  # actually delete
```

Active worlds raise `error.code == "refused_active"`; the user must `/alive:demo deactivate` first.

See `delete.md` for the irreversibility contract.

### `status`

Routes to `status.md`. Prints a 5-7 line bordered block summarising the active demo world, the cached previous world-root, and the next-step hint.

```bash
$ALIVE_PLUGIN_ROOT/bin/alive demo status
```

The CLI runs `state.load_state()` which executes the world-root self-heal -- demo-state converges to whatever `~/.config/alive/world-root` actually points at. The `status` envelope is the canonical "what does the system think is active?" surface.

See `status.md` for output shapes and the self-heal contract.

### `reset`

This subcommand is router-owned, not a CLI handler. When the human runs `/alive:demo reset`:

1. Read `~/.config/alive/world-root` via `_world_root_io.read_world_root_file()` to learn the live pointer (if any).
2. Atomically write a fresh `demo-state.json` matching `state.default_state()`. If the pointer names a directory previously promoted by the demo skill (recognizable by `<world>/.alive/_demo-build-log.md`), populate `active_world` from the build log. Otherwise leave it null.
3. Render this block:

```
╭─ 🐿️ demo state reset
│
│   demo-state.json rebuilt from world-root pointer.
│   Schema version: 0.1
│
│   active_world: <ulid|none>
╰─
```

Reset is the documented escape hatch when `alive demo status` raises `schema_version_mismatch` — the CLI's hint points here.

---

## Schema-version mismatch UX

When `alive demo status` (or any other CLI handler) returns:

```json
{"success": false, "error": {"code": "schema_version_mismatch", ...}}
```

surface this exactly:

```
╭─ 🐿️ demo state — version mismatch
│
│   demo-state.json was written by an older release.
│   Found: <found-version>
│   Expected: 0.1
│
│   Fix: run /alive:demo reset
╰─
```

Then `AskUserQuestion` with `Run reset now`, `Show file path`, `Cancel`. On `Run reset now` → invoke the `reset` flow above. On `Show file path` → render the canonical state path inline.

---

## Dispatch primitives (consumed by fn-2-2zz.4 → .9)

The following primitives lived in this file's fn-2-2zz.1 stub and survive into the full router. fn-2-2zz.4–.9 stage bodies reference them by name; do not rename without bumping every consumer.

Both primitives use the runtime's subagent dispatch tool (named **Task** in the runtime tool surface, "the Agent tool" in skill prose throughout this codebase — `plugins/alive/skills/session-history/heavy-revive.md:9`, `plugins/alive/skills/system-cleanup/SKILL.md:63`, etc.). The parameter that selects subagent class is `subagent_type`:

- `"general-purpose"` — write-capable; used for stages 0, 2, 3, 4 (every stage that emits JSON to disk).
- `"Explore"` — read-only; not used by demo stages but documented for future audits.

### Prompt-rendering wrapper (mandatory)

Per `plugins/alive/rules/squirrels.md:335-340`, brief injection is manual and the wrapper is non-negotiable. Before every dispatch:

1. **Read** `plugins/alive/templates/subagent-brief.md` once per session and cache the content as `brief_content`.
2. **Substitute** `{WORLD_ROOT}` (active world root, or partial dir if no live world) and `{PLUGIN_ROOT}` (resolved plugin root via `_common.resolve_plugin_root`).
3. **Wrap** every per-stage prompt body as the literal string:

```
CONTEXT:
{brief_content}

TASK:
{actual_task}
```

This exact `CONTEXT:` / `TASK:` wrapper is what every dispatched subagent receives. Do not improvise: subagents that don't see the brief in this shape will not know walnut/bundle/tasks.py conventions and will make mistakes.

### Primitive A — single-subagent dispatch (stages 0, 3, 4)

Render a single Task tool call with:

- `subagent_type: "general-purpose"`
- `description`: one-line label, e.g. `"alive-demo stage 0 spine"`
- `prompt`: the wrapper above, where `{actual_task}` is the stage-specific body. Stage 0's body lives at `plugins/alive/templates/demo/stage_0_spine.v1.md` (created by fn-2-2zz.4). All bodies use `{PARTIAL_DIR}` for the partial directory (`<$ALIVE_DEMO_BASE_DIR>/wld_<ulid>.partial/`) and substitute it before dispatch.

The dispatching squirrel reads the on-disk file the subagent writes; the one-line return is acknowledgement only, never the artifact.

Slug rules (enforced by stage emissions and re-validated by `validate.py` in fn-2-2zz.10): `^[a-z0-9]+(-[a-z0-9]+)*$`. No leading hyphen, no double hyphens, no unicode, no path separators.

### Primitive B — parallel-subagent dispatch (stage 2)

In a single assistant turn, emit one Task tool call per Stage 2 descriptor returned by `stages/stage2.py:prepare_dispatches`. The descriptor list spans every spine entity: `walnut_roster[*]` walnuts, `people_roster[*]` persons, AND `bundle_distribution[*]` bundles (bundles use a compound `<walnut_slug>__<bundle_slug>` directory key so the same bundle name under two walnuts never collides). Each call:

- `subagent_type: "general-purpose"`
- `description`: the descriptor's `description` field, e.g. `"alive-demo stage 2 walnut marcos-clothings"`, `"alive-demo stage 2 person john-park"`, or `"alive-demo stage 2 bundle marcos-clothings__seed-round"`.
- `prompt`: the descriptor's `prompt` field — the per-entity body wrapped in the canonical `CONTEXT:` / `TASK:` envelope. The body has `{{entity_type}}`, `{{entity_data}}`, `{{anchor_moment_refs}}`, and `{{output_dir}}` already substituted; the parent does not modify the prompt.

The output for each subagent is a **directory** at:

```
{PARTIAL_DIR}/_stage_outputs/entities/<slug>/
```

containing multiple files per the `entity_type`:

- **person / walnut** — `key.md`, `log.md`, `insights.md` (3 files).
- **bundle** — `context.manifest.yaml`, `tasks.json` (2 files).

The full per-file contract is in `plugins/alive/templates/demo/schema/entity.schema.md` and the in-prompt summary at `plugins/alive/templates/demo/stage_prompts/stage_2_entity.v1.md`. Per-slug paths are non-overlapping by construction (one subagent owns one directory). Batch by `DEFAULT_BATCH_SIZE = 6` (Anthropic's ~7 concurrent guidance with one headroom slot for the parent itself); fire one batch per assistant turn and wait for all acknowledgements before starting the next. The on-disk handoff is the source of truth; subagents return a one-line acknowledgement only.

Within each subagent, individual file writes use the atomic-write helpers (`_common.atomic_write_text` / `_common.atomic_write_json`, temp + `os.replace`).

### Validation hook (fn-2-2zz.10)

After Stage 0 / 2 / 3 / 4 each, run `validate.py` (lives at `plugins/alive/skills/demo/validate.py`, lands in fn-2-2zz.10) on the partial dir. On failure:

1. **Auto-retry once.** Re-fire the SAME stage's primitive with a truncated `previous_output` + `validation_errors` list as feedback context appended to `{actual_task}`.
2. **On second failure**, surface this block:

```
╭─ 🐿️ stage <N> validation failed twice
│
│   Errors:
│    • <error 1>
│    • <error 2>
│
│   What now?
╰─
```

Then `AskUserQuestion` with `Accept partial (continue)`, `Retry full from stage <N>`, `Cancel`. On `Cancel`, the partial dir stays on disk as a resumable handle (the user can run `alive demo delete <ref>` later to destroy it explicitly); the failure marker is preserved so `alive demo resume` can re-offer the retry.

Stage 1 is in-session UX — no LLM, no validator. Stage 5 is deterministic Python — own integrity checks at the activation transaction level (fn-2-2zz.9).

---

## Preset path (sandbox testing) — fn-2-2zz.11

When the human picks "preset (sandbox-testing)" from the create-flow branch (or invokes `alive demo preset run` directly), the skill skips Stages 0..4 entirely. The preset content is hand-authored at `plugins/alive/skills/demo/preset/realistic-seeded/`; the activation transaction copies that tree to `<base>/wld_<ULID>/` and runs the Stage 5 transactional sequence (steps 3, 7, 8, 10, 11) verbatim. Steps 5 and 6 are skipped (preset ships pre-baked `completed.json` and a fully-canonical walnut tree from copytree); steps 4 and 9 are preset-specific.

Three CLI entry points:

```bash
# 1. Validate + emit plan (no writes).
alive demo preset prepare --preset realistic-seeded

# 2. Execute the activation transaction. Pass --confirm if step 1 reports
#    pre-check findings on the live world.
alive demo preset run --preset realistic-seeded [--confirm]

# 3. Post-activation Read-Before-Speaking + pointer/state verification.
alive demo preset verify --world <path>
```

Use the same restart-Claude-Code block (documented later in this file) after `run` succeeds so the new world index is picked up.

If the preset directory or its `_world_meta.json` is missing, the CLI returns `error.code = "preset_not_found"` with a `hint` pointing the user at the custom path. Surface this inline:

```
╭─ 🐿️ preset path unavailable
│
│   The realistic-seeded preset is not present in this build.
│   Use the custom (persona-driven) path instead:
│
│   ▸ /alive:demo create  (then choose Custom)
╰─
```

## Stage 2 — entity prose subagents (parallel)

Once Stage 1's anchor envelope is frozen, fan out one Agent tool call per
entity (person walnut, venture/experiment/life-area walnut, bundle) so the
prose generation runs concurrently. The deterministic per-slug paths are
documented in `plugins/alive/templates/demo/schema/entity.schema.md` and
the prompt in `plugins/alive/templates/demo/stage_prompts/stage_2_entity.v1.md`.

Driver loop the parent squirrel runs:

1. Call `alive demo stage2 prepare --partial <partial>` (CLI subcommand
   below) — emits a JSON list of dispatch descriptors built from the
   spine + anchor envelope. Bundle slugs are compounded as
   `<walnut>__<bundle>` so two `seed-round` bundles under different
   walnuts never collide on disk.
2. Render this status block inline before firing:

   ```
   ╭─ 🐿️ stage 2 dispatch
   │
   │   <N> entities to generate (P persons, W walnuts, B bundles)
   │   batched <M> calls per assistant turn
   │
   │   Output: _stage_outputs/entities/<slug>/
   ╰─
   ```

3. **Fire all Agent tool calls in a single message in parallel** for the
   first batch. Each call uses `subagent_type: "general-purpose"`,
   `description` and `prompt` from the descriptor. Wait for all
   acknowledgements; never read the disk before the runtime returns.
   Repeat per batch (default 6 per Anthropic concurrent guidance).
4. Call `alive demo stage2 collect-validate --partial <partial>` —
   walks the per-slug directories, validates each via the hand-rolled
   stdlib validator (no `pyyaml`, no `jsonschema`), and emits a
   findings list keyed on slug.
5. **On clean validation:** call `alive demo stage2 freeze --partial <partial>`
   to write `_stage_outputs/stage2_done.json`. Surface the success
   block:

   ```
   ╭─ 🐿️ stage 2 frozen
   │
   │   <N> entity directories validated
   │   _stage_outputs/stage2_done.json written
   │
   │   Next: stage 3 timeline materialisation (fn-2-2zz.7)
   ╰─
   ```

6. **On findings (one auto-retry per slug, per epic):** call
   `alive demo stage2 retry-dispatch --partial <partial>` to receive
   retry descriptors with the per-slug findings appended as feedback,
   fire those Agent calls in a single message in parallel, then
   re-collect-validate. Second failure surfaces the 3-option
   `AskUserQuestion` (accept partial / retry full / cancel) per the
   pattern documented earlier under "Validation hook (fn-2-2zz.10)".

The `prepare` / `collect-validate` / `retry-dispatch` / `freeze` JSON
envelopes are the only path the skill uses to read or write the partial
directory; never Read or Edit `entities/<slug>/` files from the parent
session. The CLI envelope shape is:

```json
{
  "success": true,
  "stage": "2",
  "partial_dir": "<abs path>",
  "dispatches": [<descriptor>, ...]   // for prepare
  "coverage": {<slug>: {...}, ...}     // for collect-validate
  "findings": [<finding>, ...]         // for collect-validate
  "marker": {<frozen marker>}          // for freeze
}
```

Voice / style enforcement: the prompt template carries the
`no-em-dashes` rule, the synthetic-surname allowlist, the
`no-real-public-figure-surnames` guard, second-person voice in body
sections, and ALIVE narrative tone (vivid, specific, no closure). The
validator rejects em / en / horizontal-bar dashes in body prose; the
synthetic-surname rule is enforced by the prompt itself (the validator
cannot tell a real surname from an invented one).

## Stage 3: timeline materialisation (single subagent)

Once Stage 2's entity envelope is frozen (`stage2_done.json` on disk),
fan in to ONE Agent tool call that writes the full log timeline. Stage 3
is the longest single pass; one head holds the whole world in context
so cross-references stay coherent. Outputs land at
`<partial>/_stage_outputs/log.md` (world-level), and per-slug under
`<partial>/_stage_outputs/people-logs/<slug>.md` and
`<partial>/_stage_outputs/walnut-logs/<slug>.md`. Bundle activity is
recorded inside the parent walnut's log (matches v3 layout).

Driver loop the parent squirrel runs:

1. Call `alive demo stage3 prepare --partial <partial>` (CLI subcommand
   below). The CLI reads spine + anchors + stage2_done.json, gates on
   `frozen=true`, and emits a single dispatch descriptor (one prompt,
   one subagent_type, one description, plus the expected output paths).
2. Render this status block inline before firing:

   ```
   ╭─ 🐿️ stage 3 dispatch
   │
   │   single subagent (sequential, full-timeline coherence)
   │
   │   Output: _stage_outputs/log.md
   │           _stage_outputs/people-logs/<slug>.md
   │           _stage_outputs/walnut-logs/<slug>.md
   ╰─
   ```

3. **Fire ONE Agent tool call** with `subagent_type: "general-purpose"`,
   `description` and `prompt` from the descriptor. Wait for the
   one-line acknowledgement (`wrote N world entries, M people logs,
   K walnut logs`); never read disk before the runtime returns.
4. Call `alive demo stage3 collect-validate --partial <partial>`. This
   walks the world log + per-slug log files and emits a findings list.
5. **On clean validation:** call `alive demo stage3 freeze --partial <partial>`
   to write `_stage_outputs/stage3_done.json`. Surface the success
   block:

   ```
   ╭─ 🐿️ stage 3 frozen
   │
   │   <N> entries across world + <P> person + <W> walnut logs
   │   _stage_outputs/stage3_done.json written
   │
   │   Next: stage 4 insights synthesis (fn-2-2zz.8)
   ╰─
   ```

6. **On findings (one auto-retry per epic locked decision):** call
   `alive demo stage3 retry-dispatch --partial <partial>` to receive a
   single retry descriptor with the validator findings appended as
   feedback, fire that Agent call, then re-collect-validate. Second
   failure surfaces the 3-option `AskUserQuestion` (accept partial /
   retry full / cancel) per the pattern documented earlier under
   "Validation hook (fn-2-2zz.10)".

The `prepare` / `collect-validate` / `retry-dispatch` / `freeze` JSON
envelopes are the only path the skill uses to read or write the partial
directory; never Read or Edit `log.md` / `people-logs/` / `walnut-logs/`
files from the parent session.

Voice / style enforcement: the prompt template carries the no-em-dashes
rule, the synthetic-surname continuation, the deterministic
squirrel-ID hashing function (SHA-256 over `date + entity_hash`), the
Decision-WHY rule, the anchor coverage rule (every anchor moment yields
>=3 entries that reference its title or hook entities), the cross-walnut
rule (regular entries cite at most one walnut), and the entity-ref
resolution rule (every `[[slug]]` resolves to a real walnut/person; bundle
compound slugs are not permitted as wikilink targets; one-off proper
nouns appear as plain text matching the documented color allowlist
regex). The validator at `stages/stage3.py:validate_timeline` enforces
all of the above.

Stage 3 OVERWRITES the placeholder log.md frontmatter that Stage 2 leaves
in `entities/<slug>/log.md`? NO. Stage 3 writes a NEW set of log files
under `_stage_outputs/log.md`, `_stage_outputs/people-logs/`, and
`_stage_outputs/walnut-logs/`. The Stage 2 placeholder log.md files in
`entities/<slug>/` stay where Stage 2 wrote them; the activation
transaction (Stage 5) is responsible for resolving the two into the
world's final per-walnut log layout.

## Stage 4: insights synthesis (single subagent)

Once Stage 3's timeline is frozen (`stage3_done.json` on disk), fan in
to ONE Agent tool call that writes the standing insights. Stage 4 is
shorter than Stage 3 but still load-bearing: every insight must cite
at least one log entry by `(YYYY-MM-DD, squirrel:<8-hex>)` so the demo
proof point holds (the agent answers context-reliant questions by
citing entries). Outputs land at
`<partial>/_stage_outputs/insights.md` (world-level) and per-walnut
under `<partial>/_stage_outputs/walnut-insights/<slug>.md`. Per-walnut
files are CONDITIONAL: produce one only where the timeline supports a
real recurring pattern.

Driver loop the parent squirrel runs:

1. Call `alive demo stage4 prepare --partial <partial>` (CLI subcommand
   below). The CLI reads spine + anchors + stage3_done.json, gates on
   `frozen=true`, and emits a single dispatch descriptor (one prompt,
   one subagent_type, one description, plus the expected output paths).
2. Render this status block inline before firing:

   ```
   ╭─ 🐿️ stage 4 dispatch
   │
   │   single subagent (cross-walnut + per-walnut insights)
   │
   │   Output: _stage_outputs/insights.md
   │           _stage_outputs/walnut-insights/<slug>.md  (conditional)
   ╰─
   ```

3. **Fire ONE Agent tool call** with `subagent_type: "general-purpose"`,
   `description` and `prompt` from the descriptor. Wait for the
   one-line acknowledgement (`wrote insights.md (N insights), K
   walnut-insights files`); never read disk before the runtime returns.
4. Call `alive demo stage4 collect-validate --partial <partial>`. This
   walks the world insights file + walnut-insights/ dir and emits a
   findings list.
5. **On clean validation:** call `alive demo stage4 freeze --partial <partial>`
   to write `_stage_outputs/stage4_done.json`. Surface the success
   block:

   ```
   ╭─ 🐿️ stage 4 frozen
   │
   │   <N> insights across world + <K> walnut-insights files
   │   _stage_outputs/stage4_done.json written
   │
   │   Next: stage 5 deterministic activation (fn-2-2zz.9)
   ╰─
   ```

6. **On findings (one auto-retry per epic locked decision):** call
   `alive demo stage4 retry-dispatch --partial <partial>` to receive a
   single retry descriptor with the validator findings appended as
   feedback, fire that Agent call, then re-collect-validate. Second
   failure surfaces the 3-option `AskUserQuestion` (accept partial /
   retry full / cancel) per the pattern documented earlier under
   "Validation hook (fn-2-2zz.10)".

The `prepare` / `collect-validate` / `retry-dispatch` / `freeze` JSON
envelopes are the only path the skill uses to read or write the
partial directory; never Read or Edit `insights.md` /
`walnut-insights/` files from the parent session.

Voice / style enforcement: the prompt template carries the
no-em-dashes rule, the citation format
`(YYYY-MM-DD, squirrel:<8-hex>)`, the section vocabulary from
`templates/walnut/insights.md` (Strategy / Process / Technical /
People / Patterns / Tensions / Open Questions / Other), the coherence
rule (every bullet has a citation; either an anchor session or a
recurring pattern across two or more entries), and the restraint rule
(per-walnut insights only where the timeline supports a real recurring
pattern). The validator at `stages/stage4.py:validate_insights` enforces
citation FORMAT and presence; full citation RESOLUTION (does the cited
entry actually exist?) is the fn-2-2zz.10 validator's job.

## Stage 5: deterministic activation transaction

Stage 5 is **deterministic Python**, no LLM. It takes the fully-baked Stage 0-4 partial directory and activates it as the live ALIVE world via an 11-step transaction with **exactly one commit point at step 11**. Failure at any step 1-10 leaves `~/.config/alive/world-root` unchanged.

The 11 steps are documented at `plugins/alive/skills/demo/scaffold.py`. Briefly:

1. `activation_pre_check` — flag findings on the current live world (`saves: 0` + transcript >4KB; log.md mtime > now.json mtime; `saves: 0` + non-null `recovery_state`).
2. `os.rename(<partial>, <base>/wld_<ULID>/)` — atomic same-FS promotion.
3. Generate `<world>/.alive/preferences.yaml`.
4. Generate `<world>/.alive/_squirrels/*.yaml` per session in the world log.
5. Synthesize `<walnut>/_kernel/completed.json` (80 % of tasks, backdated).
6. `step_6_install_entities` — move Stage 0-4 outputs from `_stage_outputs/` into the canonical walnut layout (per-walnut `_kernel/{key,log,insights}.md`, per-person `02_Life/people/<slug>/_kernel/...`, per-bundle `<walnut>/<bundle>/{context.manifest.yaml,tasks.json}`, world-level `.alive/{log,insights}.md`); `os.replace` per-file + final `rmtree` of the entities dir; idempotent via SHA-256 equality checks.
7. Shell out `project.py --walnut <abs>` per walnut → `_kernel/now.json`.
8. Shell out `generate-index.py <world-root>` → `.alive/_index.{yaml,json}`.
9. Write `<world>/.alive/_demo-build-log.md` (provenance frontmatter for `state.py` self-heal).
10. Atomically update `~/.config/alive/demo-state.json` (flock + atomic-write): cache previous pointer, mark `partial_generations[N].status = "promoted"`, populate `active_world`. **Pre-commit metadata staging.**
11. **`_world_root_io.write_world_root_file(<world>)`** — THE single commit point.

### Calling Stage 5 from this skill

The router calls Stage 5 via the CLI (never via direct Python imports — the skill prose layer never reaches into modules):

```bash
# Step 1: dry-run plan + pre-check.
alive demo stage5 prepare --partial <abs-path>
```

If the response carries `plan.needs_confirmation == true`, surface this bordered block AND fire `AskUserQuestion`:

```
╭─ 🐿️ activation — uncommitted work on current world
│
│   The current live world has unsaved work the demo activation will
│   replace:
│
│   <findings as one bullet per finding>
│
│   Activate anyway? Your unsaved work stays on disk; the agent will
│   see the demo world after restart.
│
│   ▸  1. Yes, activate    2. Cancel
╰─
```

Use `AskUserQuestion` with options `Activate anyway`, `Cancel`. On cancel, render a cancellation block and stop. On confirm, proceed:

```bash
# Step 2: run the 11-step transaction.
alive demo stage5 run --partial <abs-path> --confirm
```

(Drop `--confirm` when the pre-check returned no findings.)

### Post-activation block (mandatory)

After step 11 commits successfully, render the restart-Claude-Code block (documented later in this skill). The agent in the current session continues to read the OLD `WORLD_INDEX` injection until restart.

### Failure surfaces

The CLI emits `success: false` envelopes on:

* `stage5_not_ready` — stage{0..4}_done markers missing or not frozen. Hint: run Stages 0-4 first.
* `needs_confirmation` — pre-check found unsaved work; re-run with `--confirm`.
* `stage5_error` — any error in steps 2-11 (rename failure, project.py crash, write-permission denied, ...). On these, the world-root pointer is GUARANTEED unchanged (the only writer is step 11, which is the last step). The partial dir's state on disk depends on which step failed; the user can re-run `alive demo stage5 run` to retry from scratch (the rename in step 2 is idempotent only if the partial still exists — failures at step 2 leave the partial untouched, failures after step 2 leave the freshly-renamed world directory at `<base>/wld_<ULID>/` for inspection / cleanup).

## What this router does NOT do (deferred to siblings)

- **Custom-path orchestration** -- `create.md` (sibling of this file) drives Stage 0 through Stage 5 end-to-end for the persona-driven path. The router only emits the no-args picker and routes the human into `create.md` on `Custom`.
- **Stage bodies** -- Stage 0, Stage 1, Stage 2, Stage 3, Stage 4, Stage 5 all shipped.
- **Validator** -- `validate.py` (stdlib-only).
- **Sandbox preset** -- shipped (see § "Preset path (sandbox testing)" above; lean Nova Station preset under `preset/realistic-seeded/`).
- **list / activate / deactivate / delete / status bodies** -- shipped (sibling `.md` files + `cli_register.py` handlers + `lib.py` helpers + `stages/{activate_existing,deactivate,delete_existing}.py`).
- **Failure-mode hardening + fixture regen** -- shipped.

## Restart-Claude-Code instruction (post-activation)

After Stage 5 commits the new world-root pointer (fn-2-2zz.9), the active session's existing `WORLD_INDEX` injection is stale. The skill MUST surface this block before any further action:

```
╭─ 🐿️ activated — restart Claude Code
│
│   Demo world activated:
│     <ulid>  ·  <label>
│     <path>
│
│   The session-start hook injects WORLD_INDEX once per session. To pick
│   up the new world index, restart Claude Code (Cmd+Q + relaunch).
│
│   Or: run /alive:world to re-render against the new pointer.
╰─
```

(The skill cannot magic-relaunch Claude Code in v3.2 — that's deferred to v4 per spec § "Out of scope".)

---

## Coherence retry contract (fn-2-2zz.10)

Every LLM-driven stage (0, 2, 3, 4) is gated by `validate.py:validate_stage(<stage_id>, <partial_dir>)` immediately after the subagent writes its outputs. Stage 1 is UX-only and has no coherence check.

The validator returns a `ValidationResult` envelope with one of three statuses:

- `ok` -- proceed to the next stage.
- `retryable` -- one auto-retry. The dispatcher has two compatible options:
  - **Direct**: append `validate.format_retry_feedback(result)` (a preformatted markdown block) to the original stage prompt and re-dispatch the subagent. This is the path for callers that hold the original prompt string in hand.
  - **Stage-native**: pass the per-stage findings list directly into the stage's existing `retry_dispatch()` helper (e.g. `stage3.retry_dispatch(descriptor, stage3_findings)`). The stage helpers consume their own native finding shape; convert by reading the per-stage findings off `validate_stage`'s underlying call (or by re-calling the stage's own validator). This is the path that re-uses the `retry_dispatch()` API verbatim.

  Either path: re-run the subagent, re-run `validate_stage`. If the second run is also `retryable` or `fatal`, the squirrel surfaces the second-failure user prompt below.
- `fatal` -- structural / schema-version failure that re-dispatch will not fix. Surface the second-failure user prompt immediately.

### Second-failure user surface

When validation fails twice (or returns `fatal` on the first run), the squirrel emits this block inline and fires `AskUserQuestion`:

```
╭─ 🐿️ stage <N> validation: second failure
│
│   <count> error(s) remain after one retry.
│
│   First three:
│   - [<code>] <where>: <evidence>
│   ...
│
│   ▸ Three options:
│   1. Accept partial   (proceed with current outputs)
│   2. Retry full       (re-dispatch the whole stage from scratch)
│   3. Cancel           (abandon this demo world)
╰─
```

Render via `validate.three_option_surface_block(result)`. The squirrel calls `AskUserQuestion` with the three options exactly as rendered. **Workers cannot fire AskUserQuestion themselves** -- the parent skill at the squirrel level does, after the worker's stage dispatcher returns the failure result up.

### Cross-stage citation resolution (Stage 4 -> Stage 3)

`validate.py:_validate_stage_4` adds a check the per-stage `stage4.validate_insights` intentionally defers: every insight bullet's `(YYYY-MM-DD, squirrel:<8-hex>)` citation must resolve to a real Stage 3 log entry. The 8-hex prefix is matched against the 16-hex squirrel-ids in `world log + people-logs/*.md + walnut-logs/*.md`. Format-only checks live in Stage 4; resolution lives here.

The validator also enforces the **anchor-or-pattern** rule (per-bullet): every insight bullet either cites an anchor-moment date OR cites >=3 distinct resolved log entries (by `(sid_8, date)` pair) on its own. Distinct sessions on the same day count as distinct refs; the same `(sid, date)` pair cited twice counts once. A one-off bullet that rides on the section's other bullets does not earn its insight slot.

### CLI entry point

`alive demo validate <stage_id> --partial <path>` emits the `ValidationResult` JSON for the parent squirrel. Stage IDs: `0`, `2`, `3`, `4`. Stage 1 returns a `usage` error (UX-only stage).

---

## Failure recovery (fn-2-2zz.13)

Three failure modes (15a / 15b / 15c per the epic spec) surface a bordered block plus a state mutation so a later `alive demo resume` can offer a retry. Each mode is rendered via `lib.py`'s failure handlers. Print the `rendered_block` field verbatim; do not re-wrap it.

### 15a. Validation fails twice (any LLM stage)

Stages 0 / 2 / 3 / 4 each ship one auto-retry. On second failure the orchestrator calls the per-stage `surface_double_failure(...)` (or `run_stage0(..., surface_failure_blocks=True)` returns the envelope inline). The bordered block carries:

- `\U0001f6d1` glyph in the title (renders as a stop-sign octagon).
- The first 5 errors with truncated evidence; total error count.
- The raw output path (the failing subagent file on disk).
- The partial directory path so the user can inspect intermediate stage outputs.
- Instructions: inspect, run `alive demo resume`, file a bug at `https://github.com/alivecontext/alive/issues/new`.

`demo-state.json` is updated transactionally: the partial's entry gets `failed_at_stage = "<stage>_<label>"`, `failed_reason = "validation_double_failure"`, and `failed_at = <iso ts>`. The entry's `status` stays `in_progress` so `find_resumable_partials` lists it.

### 15b. Stage 5 projection step crashes

Steps 7 (`project.py --walnut <walnut>`) and 8 (`generate-index.py <world>`) shell out to deterministic scripts. Either failing means the activation transaction cannot reach the step-11 commit point. Call `scaffold.activate(..., surface_failure_blocks=True)`; on subprocess crash or nonzero exit the function returns an envelope with `status: "failed"` and `failure_mode: "projection_failure"`. The pointer is GUARANTEED untouched (step 11 has not fired). The partial generation is marked `failed_at_stage = "5_promote"`, `failed_reason = "projection_failure"`.

### 15c. Atomic write fails (disk full / read-only / permission denied)

Step 10 (`step_10_stage_demo_state`) is the only step in the activation transaction that mutates demo-state.json under flock. Wrapped in try/except OSError; the failure handler renders a block but does NOT mutate state (because the mutation is what failed). Demo-state.json is preserved by the atomic-rename contract: either the new content lands fully or the previous content stays intact.

The block calls out the specific errno + cause, lists common errno triages (28 / 30 / 13), and points at `df -h` + permission inspection.

### `alive demo resume` subcommand

```
alive demo resume                # list resumable partials
alive demo resume <partial-id>   # show retry plan for one
```

- Zero resumable: friendly "nothing to resume" block.
- One resumable: returns `{partial_id, failed_at_stage, failed_reason, suggested_action, rendered_block}`. The squirrel's prose drives the actual retry by calling the right stage entry point (the CLI cannot re-fire LLM dispatches autonomously).
- Multiple resumable + no `partial_id`: ambiguous-list block listing each by `<label>  -  <ulid>  (failed at <stage>, reason: <reason>)`.

See `resume.md` (sibling of this file) for the prose walkthrough.

---

## What this stub deliberately does NOT do (carried over from fn-2-2zz.1)

The fn-2-2zz.1 stub had a `spike-test` subcommand that is now retired — its job (validating the dispatch primitives empirically) was discharged by the live gate (see `_spike.md` § "Gate result"). If a human invokes `/alive:demo spike-test`, route to:

```
╭─ 🐿️ spike-test — retired
│
│   The fn-2-2zz.1 spike gate already PASSED (2026-04-29). Dispatch
│   primitives are now codified in this router under § "Dispatch
│   primitives". Use /alive:demo (no args) for the create flow.
╰─
```

`_spike.md` itself stays in this directory until fn-2-2zz.14 / merge prep, then is deleted.

---

## References

- `plugins/alive/skills/demo/state.py` — demo-state.json IO + self-heal.
- `plugins/alive/skills/demo/lib.py` — `format_block`, `format_table`, `new_world_ulid`, `derive_label`.
- `plugins/alive/skills/demo/cli_register.py` — `alive demo` argparse wiring.
- `plugins/alive/templates/demo/prefix_table.md` — v4 ULID prefix namespace.
- `plugins/alive/scripts/_common.py` — `atomic_write_json`, `flock_file`, `resolve_plugin_root`, `iso_now`.
- `plugins/alive/scripts/_world_root_io.py` — `read_world_root_file`, `write_world_root_file` (Stage 5 commit).
- `plugins/alive/_vendor/ulid/` — vendored python-ulid 3.1.0.
- `plugins/alive/templates/subagent-brief.md` — substitution template for the dispatch wrapper.
- `.flow/specs/fn-2-2zz.md` — the epic plan; § "Approach" is canonical for the activation transaction.
- `.flow/tasks/fn-2-2zz.3.md` — this task's spec.
