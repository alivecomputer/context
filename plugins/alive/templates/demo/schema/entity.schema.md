# Stage 2 Entity Outputs — schema (v0.1)

Stage 2 of the `/alive:demo` generation pipeline fans out one parallel
subagent per entity (person, walnut, bundle) drawn from the frozen
spine + anchor envelope. Each subagent owns its own
`<partial>/_stage_outputs/entities/<slug>/` directory and writes the
files documented below. Per-slug directories are non-overlapping by
construction; race protection therefore reduces to atomic-write within
each slug (`_common.atomic_write_text` / `_common.atomic_write_json` —
temp + `os.replace`).

This document is the **human-readable canonical schema description**
the parent skill renders inline before dispatch and the validator
(`stage2.validate_entity_outputs`) enforces post-dispatch.

**Stdlib-only validation** is the locked epic policy
(`.flow/specs/fn-2-2zz.md` § "Why stdlib-only validation"). The Stage 2
validator parses YAML frontmatter via a small hand-rolled parser, never
imports `pyyaml` / `jsonschema` / similar. Field-level invariants land
in this stage's validator; cross-stage coherence (e.g. every Key
People wikilink resolves to a real person walnut directory) lands in
fn-2-2zz.10's `validate.py`.

## entity_type discriminator

The dispatcher classifies each spine slug into one of three entity
types before rendering the per-entity prompt:

* **person** — slug appears in `spine.people_roster[*].slug`.
* **walnut** — slug appears in `spine.walnut_roster[*].slug`.
* **bundle** — slug appears in `spine.bundle_distribution[*].slug`
  (compound id: per-walnut scope, so the dispatcher's slug-key uses
  `<walnut_slug>__<bundle_slug>` to disambiguate).

The same surface schema applies regardless of which spine roster the
entity came from; the shape variations below are keyed off
`entity_type`, not the originating roster.

## File layout per entity_type

Every entity writes its files into:

```
<partial>/_stage_outputs/entities/<slug>/
```

The slug satisfies `^[a-z0-9]+(-[a-z0-9]+)*$` per the global slug rule
(`stages/stage0.py:_ISO_DATE_RE` neighbourhood and `lib.is_valid_slug`).
For bundles, the slug is the compound `<walnut_slug>__<bundle_slug>`
form so a bundle named `seed-round` under both `marcos-clothings` and
`harbor-foods` produces two distinct directories.

### Person

File count: **3**. All present after Stage 2 success.

```
entities/<person_slug>/key.md         frontmatter + body
entities/<person_slug>/log.md         placeholder; populated in Stage 3
entities/<person_slug>/insights.md    placeholder; populated in Stage 4
```

`key.md` frontmatter (closed key set, all required):

| key          | type     | constraint                                               |
|--------------|----------|----------------------------------------------------------|
| `type`       | string   | literal `"person"`                                       |
| `name`       | string   | non-empty; the synthetic full name (first + made-up surname) |
| `slug`       | string   | matches `^[a-z0-9]+(-[a-z0-9]+)*$`; equals dirname slug   |
| `voice`      | string   | non-empty; one short clause (e.g. `"warm, precise, dry"`) |
| `role`       | string   | non-empty; the persona's relationship to the persona     |
| `links`      | list     | wikilinks to other person OR walnut slugs (`[[slug]]`); >= 1 entry. Bundle compound slugs (`<walnut>__<bundle>`) are NOT permitted -- bundles are reached via their walnut's `parent_walnut` reference, not via person `links` |
| `created`    | string   | strict `YYYY-MM-DD` ISO date (any date inside spine.time_span) |

`key.md` body (markdown):

* `# {{name}}` H1 heading.
* `## Voice` — 1-2 sentence prose anchoring the voice the downstream
  log + insights stages emulate.
* `## Role in the world` — 2-4 sentence prose describing how this
  person shows up in the persona's life.
* `## Connections`: bullet list, one bullet per `links[*]` entry,
  using `[[slug]]: short context` format. Separators must use colons
  / commas / periods / parens; em / en / horizontal-bar dashes are
  rejected by the body-prose validator.

`log.md` and `insights.md` are placeholders with frontmatter only:

* `log.md` carries `walnut: <name>`, `created: <date>`, `last-entry: <date>`,
  `entry-count: 0`, `summary: "Stage 2 placeholder; populated in Stage 3."`
* `insights.md` carries `walnut: <name>`, `updated: <date>`, plus a
  single H2 (`## Strategy`) the Stage 4 subagent appends to.

### Walnut

File count: **3**. Same shape as Person -- including the rule that
`links` accepts only person OR walnut slugs (NOT bundle compound
slugs). Bundles attach via their own manifest's `parent_walnut` field;
they are not link targets from a walnut's frontmatter. With these
additions in `key.md`:

| key       | constraint                                                            |
|-----------|------------------------------------------------------------------------|
| `type`    | one of `"venture"`, `"experiment"`, `"life-area"`, `"minimal-life"`  |
| `goal`    | non-empty; one-sentence north-star                                    |
| `rhythm`  | one of `"daily"`, `"weekly"`, `"sporadic"`                            |
| `parent`  | string or `null`                                                      |
| `people`  | list of person slugs (NOT wikilinks); subset of spine people_roster   |

`key.md` body extras:

* `## Key People`: bullet list, `**{full name}**, {short role context}. [[slug]]`
  for every person in the spine that this walnut's anchor moments
  reference. Per epic invariant, every venture walnut has at least
  one entry; minimal-life walnuts may have zero.
* `## Context` — 3-6 sentence standing context (what this walnut is,
  why it exists).

The validator confirms that every wikilink target inside `## Key People`
appears as a slug in this stage's people directory under the same
`entities/` parent (cross-reference resolution within Stage 2 outputs).

### Bundle

File count: **2**.

```
entities/<compound_slug>/context.manifest.yaml
entities/<compound_slug>/tasks.json
```

`context.manifest.yaml` frontmatter (mirrors
`templates/bundle/context.manifest.yaml`, closed key set):

| key             | constraint                                                  |
|-----------------|-------------------------------------------------------------|
| `name`          | non-empty string; the human-readable bundle name            |
| `goal`          | non-empty string; the bundle's one-sentence goal            |
| `species`       | one of `"outcome"`, `"evergreen"`                           |
| `phase`         | one of `"draft"`, `"prototype"`, `"published"`, `"done"`    |
| `parent_walnut` | string; equals the walnut slug (must resolve in Stage 2)    |
| `created`       | strict `YYYY-MM-DD` ISO date                                |
| `tags`          | list of strings (may be empty)                              |
| `people`        | list of person slugs; subset of spine people_roster         |

`tasks.json` shape:

```json
{
  "tasks": []
}
```

The validator enforces:

* `parent_walnut` resolves to an existing walnut directory under the
  same `entities/` parent.
* `tasks.json` parses; top-level is `{"tasks": [...]}`; the array is
  empty (Stage 5 backdates completed tasks via direct file writes,
  NOT via this Stage 2 scaffold).

## Voice / style invariants (every file the subagent writes)

1. **No em / en / horizontal-bar dashes** anywhere in user-visible
   prose (standing voice rule). Use commas, periods, parens, colons.
2. **Second-person where appropriate** — `key.md` body for persons /
   walnuts addresses the persona ("you walked"), matching the few-shot
   exemplars at `plugins/alive/templates/demo/anchor_moment_examples.json`.
3. **Synthetic surnames only.** Persons receive plausible-sounding
   invented surnames (the prompt carries an allowlist plus a
   "do not use real public-figure surnames" guard). The validator
   does NOT enforce this directly — the prompt is the load-bearing
   guard, the validator only checks that the frontmatter `name` is
   non-empty.
4. **ALIVE narrative tone** — vivid, concrete, sensory specificity,
   implicit forward tension over closure.

## What this stage does NOT validate

* **Coherence with anchor moments.** The dispatcher slices anchor
  moments per entity and passes them as context, but the validator
  here only checks that the entity files are well-formed. The full
  cross-stage coherence pass (every anchor's `walnut_slugs` resolves
  to a real walnut directory, etc.) lands in fn-2-2zz.10.
* **Stage 3 / 4 output presence.** `log.md` and `insights.md` are
  placeholders here; their populated versions are Stage 3 + 4.
* **completed.json / now.json.** These are Stage 5 outputs;
  Stage 2 never touches them.

## Post-install fate

`<partial>/_stage_outputs/entities/<slug>/` is a build-staging area, not
a permanent home. During Stage 5 activation, `step_6_install_entities`
(scaffold.py) consumes every entity directory the spine declares,
moves the files into the canonical walnut layout via `os.replace`,
and finally `shutil.rmtree`s `_stage_outputs/entities/` so that
`generate-index.py` does not double-count slugs in both the staging
and canonical paths.

Canonical destinations per `entity_type`:

* **walnut** — `<world>/<domain_dir>/<slug>/_kernel/{key.md, log.md, insights.md}`
  where `<domain_dir>` is the walnut's domain directory resolved from
  the spine roster (e.g. `04_Ventures/`, `05_Experiments/`,
  `02_Life/`).
* **person** — `<world>/02_Life/people/<slug>/_kernel/{key.md, log.md, insights.md, tasks.json, completed.json}`.
  People are always installed under `02_Life/people/` regardless of
  spine `domain_dir` (people roster has no `domain_dir`). `tasks.json`
  and `completed.json` are bootstrapped empty.
* **bundle** — `<world>/<domain_dir>/<walnut_slug>/<bundle_slug>/{context.manifest.yaml, tasks.json}`
  where `<walnut_slug>` is the bundle's `parent_walnut` and
  `<domain_dir>` is resolved by walking the parent walnut's spine
  entry.

World-level Stage 3/4 outputs (`_stage_outputs/log.md`,
`_stage_outputs/insights.md`) install to `<world>/.alive/log.md` and
`<world>/.alive/insights.md` respectively in the same step.

The install is idempotent: rerunning `step_6_install_entities` against
an already-installed world is a no-op (SHA-256 equality check on each
file before replace). Failures at any per-file move raise
`Stage5Error` with the missing source path; the world-root pointer
remains unchanged because Stage 5 commits only at step 11.
