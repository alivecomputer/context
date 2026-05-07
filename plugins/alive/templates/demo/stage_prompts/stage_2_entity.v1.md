{{subagent_brief}}

---

# Stage 2 — Entity prose subagent

You are one of N parallel Stage 2 subagents in the `/alive:demo` generation
pipeline. The dispatching squirrel has frozen the spine and the anchor
moments; each remaining entity (one person, one walnut, or one bundle)
gets one of you. You own one slug. You write into one directory. You do
not read or write any other entity's files.

Downstream stages cross-reference what you write. Be coherent, vivid,
and small. The schema doc at:

```
plugins/alive/templates/demo/schema/entity.schema.md
```

is the binding contract; this prompt summarises it for one entity.

## Inputs

- **Entity type**: `{{entity_type}}` (one of `person`, `walnut`, `bundle`).
- **Entity data** (a slice of the spine for THIS entity only — do not
  hallucinate fields not present here):

```json
{{entity_data}}
```

- **Anchor moments referencing this slug** (filtered from the frozen
  envelope so you know what narrative pivots to honour):

```json
{{anchor_moment_refs}}
```

## Output contract

Write your files atomically into:

```
{{output_dir}}
```

Use the standard helpers from the brief
(`_common.atomic_write_text` / `_common.atomic_write_json`). Do NOT
write outside this directory. Do NOT read or modify any sibling
`entities/<other-slug>/` directory: that is another subagent's
exclusive zone.

Per `entity_type`, the file set is:

### entity_type: person

Three files:

1. `key.md` — frontmatter + body. Frontmatter (closed key set, all
   required, in this order):

   ```yaml
   ---
   type: person
   name: "<synthetic full name>"
   slug: <this slug>
   voice: "<short clause: e.g. 'wry, precise, self-deprecating'>"
   role: "<the persona's relationship to this person>"
   links:
     - "[[<related-slug>]]"
     - "[[<related-slug>]]"
   created: <YYYY-MM-DD inside spine.time_span>
   ---
   ```

   Body:

   ```markdown
   # <name>

   ## Voice
   <one to two short sentences anchoring the voice>

   ## Role in the world
   <two to four sentences in second person describing how this person
   shows up in the persona's life>

   ## Connections
   - [[<slug>]]: <short context>
   - [[<slug>]]: <short context>
   ```

   Every `links[*]` wikilink MUST appear in the `## Connections`
   bullets, and vice versa. Link targets MUST be other person OR
   walnut slugs from the spine; bundle compound slugs of the form
   `<walnut>__<bundle>` are NOT permitted in `links` (bundles attach
   via their own manifest's `parent_walnut`, not via person/walnut
   links).

2. `log.md` — placeholder; frontmatter only:

   ```markdown
   ---
   walnut: <name>
   created: <YYYY-MM-DD>
   last-entry: <YYYY-MM-DD>
   entry-count: 0
   summary: "Stage 2 placeholder; populated in Stage 3."
   ---
   ```

3. `insights.md` — placeholder; frontmatter + a single empty H2:

   ```markdown
   ---
   walnut: <name>
   updated: <YYYY-MM-DD>
   ---

   ## Strategy
   ```

### entity_type: walnut

Three files (same names as person). The `key.md` adds walnut-specific
frontmatter and a `## Key People` body section.

`key.md` frontmatter (closed key set, all required):

```yaml
---
type: <venture | experiment | life-area | minimal-life>
name: "<walnut name>"
slug: <this slug>
goal: "<one-sentence north-star>"
rhythm: <daily | weekly | sporadic>
parent: <slug | null>
people:
  - <person-slug>
  - <person-slug>
links:
  - "[[<other-walnut-or-person-slug>]]"
created: <YYYY-MM-DD inside spine.time_span>
---
```

`links` accepts other walnut OR person slugs ONLY; do not link to
bundle compound slugs (`<walnut>__<bundle>`). Bundles attach via
their own manifest's `parent_walnut`.

Body:

```markdown
# <name>

<two to three sentence persona-anchored summary>

## Key People

<!-- Pull every person referenced by this walnut's anchor moments. Each
     bullet uses the format `**Full Name**, short role context. [[slug]]`.
     Use commas, periods, parens, or colons to separate clauses; never
     em / en / horizontal-bar dashes (the validator rejects them in
     body prose). -->
- **<full name>**, <short context>. [[<slug>]]
- **<full name>**, <short context>. [[<slug>]]

## Context

<three to six sentences standing context: what this walnut is, why it
exists, what counts as success>
```

Every wikilink target in `## Key People` MUST refer to a person slug
in the spine's `people_roster`; the validator rejects unresolved
wikilinks.

`log.md` and `insights.md` are the same placeholders documented under
`entity_type: person`.

### entity_type: bundle

Two files:

1. `context.manifest.yaml` — front-matter only YAML file matching
   `plugins/alive/templates/bundle/context.manifest.yaml`. Closed
   key set:

   ```yaml
   ---
   name: "<bundle name>"
   goal: "<one-sentence goal>"
   species: <outcome | evergreen>
   phase: <draft | prototype | published | done>
   parent_walnut: <walnut-slug>
   created: <YYYY-MM-DD inside spine.time_span>
   tags: []
   people:
     - <person-slug>
   ---
   ```

   `parent_walnut` MUST equal the entity_data's `walnut_slug` field;
   the validator rejects mismatches.

2. `tasks.json` — JSON document, exactly:

   ```json
   {
     "tasks": []
   }
   ```

   Stage 5 will populate `completed.json` directly with backdated
   tasks; Stage 2 leaves the active queue empty.

## Voice and style (CRITICAL)

1. **No em / en / horizontal-bar dashes anywhere.** Use commas,
   periods, parens, colons. This rule covers prose, frontmatter
   strings, and bullet content. (Standing rule per Patrick's voice
   memory.)
2. **Second person where the schema asks for it** — body sections
   like `## Role in the world` address the persona ("you walked",
   "your morning"), matching the anchor-moment exemplars at
   `plugins/alive/templates/demo/anchor_moment_examples.json`.
3. **ALIVE narrative tone** — vivid, concrete, sensory specificity,
   implicit forward tension over closure. Match the anchor-moment
   hooks in the few-shot exemplars.
4. **Synthetic surnames only.** When you mint a person's full name,
   pair the first name from `entity_data.name` with a plausible
   invented surname drawn from this allowlist:

   ```
   Okata, Voss, Renard, Castellanos, Halvorsen, Mwangi, Tanaka,
   Bellamy, Forsythe, Kovac, Marchetti, Ostrowski, Quintero,
   Rasmussen, Strand, Vermeulen, Yamazaki, Zwart, Imani, Lindgren
   ```

   You may invent your own, but it MUST sound plausible and MUST
   NOT match any real public figure's surname (no Mandela, Obama,
   Tesla, Spielberg, Musk, Kennedy, Beyonce, Lennon, Trump, Hawking,
   Curie, etc). When in doubt, pick from the allowlist. The
   validator does not catch real surnames; you are the load-bearing
   guard.
5. **No real public-figure surnames.** Repeat for emphasis: this
   includes politicians, celebrities, scientists, athletes, and
   contemporary tech founders. Use the allowlist or invent.

## Slug rules (non-negotiable)

Every slug field in your output must satisfy:

```
^[a-z0-9]+(-[a-z0-9]+)*$
```

The `slug` frontmatter field MUST equal your assigned slug exactly
(it appears verbatim in `{{output_dir}}` so the directory name and
the field stay in lock-step).

## Return value

After writing your files, return ONE LINE acknowledging completion in
this exact shape so the dispatcher can parse it:

```
wrote entities/<slug>/{file1, file2, ...}
```

Example for a person:

```
wrote entities/marcos-rivera/{key.md, log.md, insights.md}
```

Do NOT paste your file contents. Do NOT summarise the prose. The
dispatcher reads the files off disk and validates them.
