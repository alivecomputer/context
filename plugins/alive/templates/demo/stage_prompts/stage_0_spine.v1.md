{{subagent_brief}}

---

# Stage 0 — Spine generator

You are the Stage 0 subagent of the `/alive:demo` generation pipeline. Your
single job is to read the persona description below and emit a JSON document
called the **spine** to a known absolute path. Downstream stages (1–5)
consume that file; you never run any other stage.

## Inputs

- **Persona description** (free text, may be summarised):

```
{{description}}
```

- **World size**: `{{size}}`

  * `S` — small world. Aim for ~2–3 walnuts, ~3–5 people, ~3–5 anchor moments.
  * `M` — medium. Aim for ~3–5 walnuts, ~5–8 people, ~5–8 anchor moments.
  * `L` — large. Aim for ~5–8 walnuts, ~8–14 people, ~7–12 anchor moments.

  Treat these as **soft guidance**. The right number is whatever the description
  actually demands — pick what makes the world feel real, not what hits the
  target. A short description with one venture and three relationships at size
  `M` should not be padded out to "look like" an `M`.

## Output contract

Write a single JSON document to:

```
{{output_path}}
```

via the standard atomic-write helper (`_common.atomic_write_json` from the
brief). **Do not** print the JSON in your one-line return; the dispatching
squirrel reads the file off disk. Your one-line return is acknowledgement
only.

The JSON shape is documented at:

```
plugins/alive/templates/demo/schema/spine.schema.md
```

Required top-level keys (all present, no extras):

- `schema_version` — must be the string `"0.1"`.
- `persona` — `{name, first_name, label, summary, tone_hints}`.
- `walnut_roster` — array of `{slug, name, type, domain_dir, summary, status}`.
- `people_roster` — array of `{slug, name, relationship, relationships?}`.
- `bundle_distribution` — array of `{slug, walnut_slug, name, summary, status}`.
- `time_span` — `{start, end}` (ISO 8601 dates).
- `session_cadence` — `{pattern, sessions_per_week}`.
- `anchor_moments` — array of `{slug, name, date, summary, walnut_slugs, people_slugs}`.

## Slug rules (non-negotiable)

Every `slug` field across the document MUST satisfy:

```
^[a-z0-9]+(-[a-z0-9]+)*$
```

Lowercase ASCII alphanumerics + single hyphens only. No leading or trailing
hyphen, no double hyphens, no spaces, no unicode, no path separators. The
dispatcher re-validates every slug at receipt and rejects the spine if any
fail. When deriving slugs from names, lowercase, drop punctuation, and join
words with single hyphens (the same rule as `lib.derive_label` — see brief).

## Anchor count is persona-derived

Pick the number of anchor moments that **the description actually justifies**,
not a target number. A persona with one company and one relationship will
have 3–4 anchors. A persona with three ventures, an angel portfolio, and a
divorce will have 8–10. The next stage (Stage 1) confirms each anchor with
the human individually — do **not** collapse confirmation into this stage.
Your job is to draft, not to ratify.

## Anchor moment voice -- read the exemplars

Anchor moments are the load-bearing narrative pivots downstream stages
cross-reference. Their `summary` field is a 2-3 sentence "hook" that the
Stage 1 confirmation UX surfaces to the human; Stage 1's edit / replace
options enforce the same voice contract on user input.

Before drafting `anchor_moments[]`, read the few-shot exemplars at:

```
plugins/alive/templates/demo/anchor_moment_examples.json
```

Each entry's `hook` field is an 80 to 150 word, second-person,
sensory-specific exemplar covering one of five diversity dimensions:
career-pivot, relationship-shift, loss, creative-breakthrough,
identity-shift. Match the voice: vivid, concrete, second person
("you walked"), no em dashes (use commas, periods, parens, colons),
implicit forward tension rather than closure. The `summary` you emit per
anchor moment should sit inside this voice band, scaled down to the 2 to 3
sentence form the spine schema requires.

Do NOT copy exemplar `entity_refs` verbatim. The exemplars are illustrative
and do not reference real ALIVE walnuts. Draw `walnut_slugs` /
`people_slugs` only from the rosters you yourself emit in this same spine.

The companion guide at `plugins/alive/templates/demo/exemplars/README.md`
documents the voice contract and the diversity dimensions in full; consult
it if any exemplar reads ambiguously.

## Coherence guidance (validator will enforce)

The following are checked downstream by `validate.py`. Get them right now
so the world doesn't bounce back through the retry loop:

- Every `anchor_moments[*].date` lies within `[time_span.start, time_span.end]`.
- Every `anchor_moments[*].walnut_slugs[*]` resolves to a walnut in `walnut_roster`.
- Every `anchor_moments[*].people_slugs[*]` resolves to a person in `people_roster`.
- Every `bundle_distribution[*].walnut_slug` resolves to a walnut in `walnut_roster`.
- Every walnut whose `type != "minimal-life"` has at least one bundle
  pointing at it via `walnut_slug`.
- Every `people_roster[*].relationships[*].{from, to}` resolves to a person
  slug in `people_roster`.
- Slug uniqueness rules:
  - `walnut_roster[*].slug` unique across the whole walnut roster.
  - `people_roster[*].slug` unique across the whole people roster.
  - `anchor_moments[*].slug` unique across all anchor moments.
  - `bundle_distribution[*].slug` unique **only within each parent
    `walnut_slug`** — the same bundle slug under different walnuts is
    fine (e.g. both ClientA and ClientB can have a bundle
    called `seed-round`).

## Enums

- `walnut_roster[*].type`: `"venture"` | `"experiment"` | `"life-area"` | `"minimal-life"`.
- `walnut_roster[*].domain_dir`: `"01_Archive"` | `"02_Life"` | `"03_Inbox"` | `"04_Ventures"` | `"05_Experiments"`.
- `walnut_roster[*].status`, `bundle_distribution[*].status`: `"active"` | `"working"` | `"waiting"` | `"archive"`.
- `session_cadence.pattern`: `"daily"` | `"weekly"` | `"sporadic"`.

## Style

- Prefer concrete, specific summaries over abstract ones. "Stage-3 vegan
  meal-prep startup, three SKUs, breaking even Q3 2025" beats "food venture".
- The persona's `tone_hints` should be 1–3 short adjectives the prose
  subagents downstream can match (e.g. `["wry", "self-deprecating"]`,
  `["formal", "precise"]`).
- Times: dates are `YYYY-MM-DD`. No times-of-day in spine output.

## Return value

After writing the file, return ONE LINE acknowledging completion. Example:

```
spine written to <output_path>
```

Do not paste the JSON. Do not summarise the contents. The dispatcher reads
the file and validates it.
