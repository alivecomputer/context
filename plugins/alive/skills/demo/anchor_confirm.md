# anchor_confirm -- Stage 1 anchor moment confirmation loop

Sibling skill markdown for `/alive:demo`. Drives the per-moment 4-option
ratification UX after Stage 0 has written `spine.json` to the partial
directory and before Stage 2 prose subagents begin.

This file is prose for the **dispatching squirrel** (you). The Python
side lives at `skills/demo/stages/stage1.py`; this markdown tells you
how to call those helpers in lockstep with `AskUserQuestion` so the
human ratifies each anchor moment one at a time before the envelope is
frozen.

---

## When this runs

Right after Stage 0's `spine.json` lands and `preflight_spine` passes.
Before any Stage 2 entity-prose dispatch. Stage 1 is **UX-only** -- no
LLM dispatch happens INSIDE the loop body; option 2 (regenerate) does
fire a single Stage 0-style subagent, but the loop itself is in-session
prose + tool calls.

`anchor_confirm.md` is invoked from `create.md` (lands in fn-2-2zz.4+)
once Stage 0 returns successfully.

---

## State machine

The loop owns one file: `<partial>/_stage_outputs/anchor_moments.json`.
Every option mutates it via a `stage1.py` helper that does the atomic
write. You never Edit / Write this file directly -- go through the
helpers so the schema-version stamp + freeze invariants stay coherent.

Envelope shape:

```json
{
  "schema_version": "0.1",
  "confirmed": [<moment dict>, ...],
  "frozen": false,
  "frozen_at": null
}
```

After freeze the `frozen` flag flips to `true`, `frozen_at` is stamped
ISO 8601 UTC, and any subsequent mutation raises `Stage1Frozen`. From
that moment downstream stages cross-reference these anchor IDs without
the possibility of rename.

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

---

## The loop (canonical sequence)

For every spine anchor moment that is NOT yet confirmed (use
`stage1.pending_slugs(partial_dir)` to compute the list at the top of
each loop iteration):

### Step A -- render the moment block

Call `stage1.render_moment_block(moment)` and emit the returned string
inline. Example shape:

```
╭─ 🐿️ anchor moment: First cheque to ClientA
│  slug:  first-clienta-cheque
│  date:  2024-08-12
│
│  Lead-angel commitment finalized after two months of due diligence;
│  the founder had walked away from a worse term sheet the week before.
│
│  walnuts:  clienta
│  people:   investor-lead, founder-a, advisor-a
│
│  ▸ Accept / regenerate / edit prose / replace?
╰─
```

### Step B -- fire AskUserQuestion

Call the runtime's `AskUserQuestion` tool with EXACTLY these four
options (label text matters -- downstream branches read off it):

1. `Accept` -- the spine's draft is good as-is.
2. `Regenerate` -- you want a different angle on this moment.
3. `Edit prose` -- keep the moment, rewrite just the hook.
4. `Replace` -- full replacement (date, prose, entity refs).

Always include a fifth `Cancel` only if the parent flow already handed
you a cancel exit; the per-moment block does NOT offer cancel because
cancellation belongs to the create.md outer loop.

### Step C -- branch on the answer

#### Option 1 -- Accept

```python
stage1.accept_moment(partial_dir, moment_slug)
```

Then loop back to Step A with the next pending moment.

#### Option 2 -- Regenerate

Ask the human for free-text feedback first via `AskUserQuestion` with a
single open-text option (or via a follow-up turn if the runtime needs
prose input separately). Render this block before asking:

```
╭─ 🐿️ regenerate <slug> -- what's the angle?
│
│  ▸ One sentence on what you want different
│    (e.g. "more about the pivot, less about Marcos")
╰─
```

Then call:

```python
dispatch_envelope = stage1.regenerate_moment_prompt(
    partial_dir,
    moment_slug,
    feedback,
    world_root=world_root,
    plugin_root=plugin_root,
)
```

`dispatch_envelope` is `{subagent_type, description, prompt,
expected_output_path}`. Fire the runtime's subagent dispatch primitive
(named **Task** in the runtime tool surface, the "Agent tool" in skill
prose elsewhere in this codebase -- both refer to the same primitive,
matching `SKILL.md:165` and `stage0.py:1012`-style dispatch). Render
the call as:

```
Task(
    subagent_type=dispatch_envelope["subagent_type"],     # "general-purpose"
    description=dispatch_envelope["description"],         # one-line label
    prompt=dispatch_envelope["prompt"],                   # CONTEXT/TASK envelope
)
```

The dispatched subagent writes ONE anchor moment object (not a full
spine) to `expected_output_path`. After it returns its one-line
acknowledgement, call:

```python
stage1.apply_regenerated_moment(
    partial_dir,
    moment_slug,
    dispatch_envelope["expected_output_path"],
)
```

If `Stage1Validation` is raised, render the errors inline:

```
╭─ 🐿️ regenerate <slug> -- needs another pass
│
│  Errors:
│   • <error 1>
│   • <error 2>
│
│  ▸ Try again, edit prose by hand, or accept the original?
╰─
```

Then `AskUserQuestion` with `Try regenerate again`, `Edit prose`,
`Accept original`. Loop into the chosen option without leaving the
current moment.

#### Option 3 -- Edit prose

Ask the human for the new hook via prose-input prompt. Render:

```
╭─ 🐿️ edit prose for <slug>
│
│  Current:
│
│  <full current summary>
│
│  ▸ Paste the rewrite (80-150 words, second person, no em dashes)
╰─
```

When the human supplies the new text, call:

```python
stage1.edit_moment_prose(partial_dir, moment_slug, new_summary)
```

On `Stage1Validation`, surface the errors inside a bordered block and
re-prompt for another rewrite. The validator catches: word-count out of
band, em / en / horizontal-bar dash characters, first-person pronouns
("I", "we", contractions). Each is a distinct error string the human
can fix in one revision.

#### Option 4 -- Replace

Walk the human through five prose prompts (one per field) -- slug is
fixed at `moment_slug` (anchor IDs are immutable from this stage
forward). Render each prompt as its own bordered block:

```
╭─ 🐿️ replace <slug> -- name
│  ▸ Short human label (e.g. "ClientB pivot")
╰─
```

```
╭─ 🐿️ replace <slug> -- date
│  ▸ ISO 8601 date: YYYY-MM-DD
╰─
```

```
╭─ 🐿️ replace <slug> -- hook
│  ▸ 80-150 word second-person prose. No em dashes.
╰─
```

```
╭─ 🐿️ replace <slug> -- walnuts
│  ▸ Comma-separated walnut slugs from the spine roster
│    (e.g. "harbor-foods, family")
╰─
```

```
╭─ 🐿️ replace <slug> -- people
│  ▸ Comma-separated person slugs from the spine roster
│    (e.g. "priya-natarajan, alex-boring")
╰─
```

Assemble the dict:

```python
new_moment = {
    "slug": moment_slug,            # immutable
    "name": user_name,
    "date": user_date,
    "summary": user_hook,
    "walnut_slugs": [s.strip() for s in user_walnuts.split(",") if s.strip()],
    "people_slugs": [s.strip() for s in user_people.split(",") if s.strip()],
}
stage1.replace_moment(partial_dir, moment_slug, new_moment)
```

On `Stage1Validation`, surface the full error list and re-prompt the
specific failing fields. The validator catches: missing keys, malformed
slug, malformed date, hook out-of-band / wrong voice, entity refs that
don't resolve to spine roster entries.

---

## After every spine moment is confirmed

When `stage1.pending_slugs(partial_dir)` returns `[]`, render this
block:

```
╭─ 🐿️ all anchor moments confirmed
│
│  Ready to freeze the anchor envelope. After freeze, downstream
│  stages cross-reference these IDs as immutable narrative pivots.
│
│  ▸ Freeze and continue, or review one more time?
╰─
```

`AskUserQuestion` with `Freeze and continue to Stage 2`,
`Review again (re-loop)`, `Cancel`. On `Cancel`, the partial dir
stays on disk as a resumable handle (the user can run
`alive demo delete <ref>` later to destroy it explicitly); do NOT
auto-delete from this loop.

On `Freeze and continue to Stage 2`, call:

```python
stage1.freeze_anchors(partial_dir)
```

This stamps `frozen: true` and `frozen_at: <ISO UTC>`. It's the locked
transition. Render:

```
╭─ 🐿️ anchor envelope frozen
│
│  <N> moments confirmed. IDs are now immutable.
│  Stage 2 (entity prose) will start next.
╰─
```

On `Review again`, reset the loop position to the first moment and let
the human re-edit anything. The envelope stays unfrozen until they
explicitly choose freeze.

On `Cancel`, hand back to `create.md`'s cancel branch -- the partial
directory deletion is owned by the outer flow, not by this skill.

---

## Coherence-retry trigger (for fn-2-2zz.10)

If the human picks `Replace` and supplies entity refs that DO resolve
in the spine but produce a moment that is otherwise inconsistent with
the spine's bundle distribution or relationship graph,
`stage1.replace_moment` will still accept the change (it only checks
roster membership, not graph-level coherence). The cross-stage
validator (`validate.py`, fn-2-2zz.10) catches this when Stage 2 runs;
on its retry-with-feedback hook, Stage 0 / Stage 1 get a chance to
re-ground.

This is intentional: per the epic spec, anchor moments are LOAD-BEARING
and the human's edits must be honoured even when they create coherence
work for downstream stages. The validator does the work, not Stage 1.

---

## Errors you might surface

| Exception | When | Block to render |
| --- | --- | --- |
| `Stage1Frozen` | mutation after freeze | "anchor envelope already frozen -- run /alive:demo reset to start over" |
| `Stage1Validation` | voice / shape errors | full error list as bullets, re-prompt the failing field |
| `Stage1NotFound` | slug not in spine | "anchor moment not in spine -- re-run Stage 0?" |
| `Stage1Error` (base) | spine missing / unreadable | "spine.json not found -- Stage 0 didn't complete" |

In every case the bordered block carries the squirrel emoji + a
one-sentence explanation + a `▸` next-step prompt. Never let a Python
traceback escape into the human-facing surface.

---

## References

- `plugins/alive/skills/demo/stages/stage1.py` -- Python side.
- `plugins/alive/templates/demo/anchor_moment_examples.json` -- voice
  reference for hooks (consumed by Stage 0; the validator
  `stage1.validate_exemplars_file` keeps the file honest).
- `plugins/alive/templates/demo/exemplars/README.md` -- provenance,
  authorship, voice guide.
- `plugins/alive/templates/demo/stage_prompts/stage_0_spine.v1.md` --
  Stage 0's prompt; references the exemplars file.
- `plugins/alive/CLAUDE.md` -- visual contract.
- `.flow/specs/fn-2-2zz.md` -- epic plan, § "Approach", anchor-moment
  immutability rule.
- `.flow/tasks/fn-2-2zz.5.md` -- this stage's spec.
