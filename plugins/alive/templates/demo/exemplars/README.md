# Anchor moment exemplars -- provenance + voice guide

This directory documents the few-shot exemplars consumed by the
`/alive:demo` Stage 0 spine generator and referenced by the Stage 1
confirmation UX. The exemplars themselves live one level up at
`templates/demo/anchor_moment_examples.json` so the JSON file sits
next to the other Stage 0 inputs (`prefix_table.md`,
`stage_prompts/`, `schema/`). This README is the human-side ledger:
who wrote them, when they were reviewed, what voice they encode, and
how to regenerate or extend them.

---

## Authorship

| Field | Value |
| --- | --- |
| `authored_by` | alive-team |
| `reviewed_by` | alive-team |
| `reviewed_at` | `2026-04-29T00:00:00Z` (initial v3.2 set) |
| `schema_version` | `0.1` |

The alive team authors the exemplars by hand and reviews them before merge.
Stage 0 subagents only ever read them; no automated generator writes
back to this file. Edits go through git review like any other piece
of source code, and any new or revised entry MUST land with a fresh
`reviewed_at` stamp.

The merged v3.2 set was reviewed on 2026-04-29 alongside the Stage 1
implementation. When extending or revising the exemplars, update both
`reviewed_at` (ISO 8601 UTC timestamp) and `reviewed_by` so provenance
remains accurate at the JSON-file level (and is not deferred to git
history alone).

The Stage 1 helper `stage1.validate_exemplars_file` does NOT enforce
non-null `reviewed_at` in v3.2 (so a fresh fork can ship without
blocking on review); a future version may add a CI gate.

---

## Voice guide

Anchor moments in the ALIVE narrative tone are:

- **Second person** -- "you sit in the driver's seat", never "I sat" or
  "they sat". The first-person validator in `stage1._validate_hook_prose`
  rejects any first-person pronoun or contraction (I, me, my, mine,
  myself, we, us, our, ours, ourselves, I'm, I've, I'd, I'll, we're,
  we've, we'd, we'll). Word-boundary matched, case-insensitive.
- **80 to 150 words** inclusive. Below 80 reads as a beat rather than
  a moment; above 150 reads as scene rather than anchor.
- **Concrete and sensory** -- the time of day, the smell, the object
  in the hand, the broken streetlight, the cold coffee. Specificity
  is the whole point. A reader should be able to picture the room.
- **No em dashes, no en dashes, no horizontal bars.** Standing
  voice rule. Use commas, periods, parens, semicolons, or colons.
  The validator catches all three dash characters and rejects the
  hook with a clear error.
- **Implicit forward tension, never closure.** An anchor is a pivot,
  not a wrap-up. End on the next call, the next silence, the next
  drive -- something that pulls forward into the world that follows.
- **Slightly literary, never mannered.** The tone matches good
  long-form journalism rather than fiction or autobiography. Specific
  but not ornamental. No simile-of-the-week.

If you find yourself writing a hook that reads like an apology or a
moral, throw it out and start again. The voice is the hardest part of
this file; the diversity dimensions are the easy part.

---

## Diversity coverage

Five locked dimensions, one exemplar each, all required by the
Stage 1 validator:

| Dimension | Exemplar id | One-line gist |
| --- | --- | --- |
| `career-pivot` | `career-pivot-illustrative-001` | The morning the law-firm offer arrived (and the half-finished startup deck in the spare room). |
| `relationship-shift` | `relationship-shift-illustrative-001` | The walk back from the school play, when the question about the new flat finally gets asked. |
| `loss` | `loss-illustrative-001` | The Wednesday the print studio is locked, two weeks earlier than promised. |
| `creative-breakthrough` | `creative-breakthrough-illustrative-001` | The 3 a.m. playback in reverse order, when the bass line from May turns out to be the spine. |
| `identity-shift` | `identity-shift-illustrative-001` | Nineteen minutes in the neurology car park, after the diagnosis names the last decade. |

Adding a new exemplar:

1. Pick or extend a dimension. New dimensions are allowed (the
   required set is a floor, not a ceiling) but require a one-line
   note here in the table.
2. Author the hook. Run `stage1.validate_exemplars_file` against the
   updated JSON to confirm word count, voice, dashes, refs.
3. Set `reviewed_at` on the file once the reviewer has read the new entry.
4. Update the diversity coverage table above.

---

## Regeneration workflow

There is no auto-regen for exemplars in v3.2. The file is hand-edited
source. To revise an existing entry:

1. Open `plugins/alive/templates/demo/anchor_moment_examples.json`.
2. Edit the entry's `hook` (and `name` / `date` / `entity_refs` if
   the angle has shifted).
3. Run the validator:

   ```bash
   cd plugins/alive
   python3 -c "
   import sys, importlib.util
   sys.path.insert(0, 'scripts')
   spec = importlib.util.spec_from_file_location('s1', 'skills/demo/stages/stage1.py')
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   errs = m.validate_exemplars_file('templates/demo/anchor_moment_examples.json')
   print(errs or 'OK')
   "
   ```

4. Run the unit suite:

   ```bash
   python3 -m pytest tests/test_demo_stage1.py -v
   ```

5. Commit with a `docs(demo): revise <dimension> exemplar` message.

---

## Schema reference

```json
{
  "schema_version": "0.1",
  "authored_by": "alive-team",
  "reviewed_by": "alive-team",
  "reviewed_at": "<ISO 8601 UTC timestamp>",
  "examples": [
    {
      "id": "<slug>",
      "diversity_dimension": "<one of: career-pivot, relationship-shift, loss, creative-breakthrough, identity-shift, ...>",
      "name": "<short human label>",
      "date": "<YYYY-MM-DD, illustrative only>",
      "hook": "<80 to 150 word second-person prose, no em dashes>",
      "entity_refs": ["<illustrative ref>", ...]
    }
  ]
}
```

The `_note` top-level key is allowed and ignored by the validator. It
exists so future readers of the JSON file (humans and tools) can
self-orient without bouncing to this README.

`entity_refs` are illustrative -- the exemplars do not name real ALIVE
walnuts or real people. The Stage 0 prompt teaches the subagent to
draw refs from the spine's actual rosters, not to copy the example
strings.

---

## Files in this directory

- `README.md` -- this document.

(Future contents: per-domain voice notes, archived versions of
revised exemplars, regeneration log.)

---

## See also

- `plugins/alive/skills/demo/anchor_confirm.md` -- Stage 1 UX prose.
- `plugins/alive/skills/demo/stages/stage1.py` -- Stage 1 helpers,
  including the `validate_exemplars_file` validator.
- `plugins/alive/templates/demo/stage_prompts/stage_0_spine.v1.md` --
  Stage 0 prompt, references this exemplars file.
- `.flow/specs/fn-2-2zz.md` -- epic plan, anchor-moment immutability.
- `.flow/tasks/fn-2-2zz.5.md` -- task spec for this stage.
