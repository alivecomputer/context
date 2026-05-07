# Stage 0 Spine — schema (v0.1)

Stage 0 of the `/alive:demo` generation pipeline emits a single JSON document
to `<partial>/_stage_outputs/spine.json`. That file is the **spine** — the
skeletal world plan the downstream stages flesh out into prose, timelines,
and insights.

This document is the **human-readable canonical schema description**. The
sibling file `spine.schema.json` carries the **machine-readable Draft
2020-12 descriptor** for downstream consumers (test harnesses, future
external tools); the two are kept in sync. Both describe the same
contract.

**Validation is stdlib-only** — Stage 0 enforces this schema via a
hand-rolled validator at
`plugins/alive/skills/demo/stages/stage0.py:preflight_spine`, NOT via
`jsonschema.Draft202012Validator`. This is a locked epic-level decision
documented in `.flow/specs/fn-2-2zz.md` § "Why stdlib-only validation":
vendoring `jsonschema` would pull `attrs`, `referencing`,
`jsonschema-specifications`, and `rpds-py` (the last has a Rust extension
— not pure-Python), violating the plugin's stdlib-only posture inherited
from `_common.py`. The plan's earlier reference to `Draft202012Validator`
is replaced. The `.json` artifact remains useful as a portable
descriptor — anyone wanting to validate a spine.json without importing
the plugin can run their own `jsonschema` against it.

**Stage 0 runs the full structural pre-flight** before handing off to
the next stage:

  1. The file parses as JSON.
  2. The top-level value is an object.
  3. Required keys present, no extra keys (`additionalProperties: false`
     enforced at every object level).
  4. `schema_version == "0.1"`.
  5. Every enum value (walnut.type / domain_dir / status, bundle.status,
     session_cadence.pattern) is one of the documented options.
  6. Every date (`time_span.start` / `time_span.end` /
     `anchor_moments[*].date`) parses as `YYYY-MM-DD` ISO 8601 and
     `time_span.start <= time_span.end`.
  7. `session_cadence.sessions_per_week` is a number in `(0, 14]`.
  8. Every slug satisfies `^[a-z0-9]+(-[a-z0-9]+)*$`.
  9. **Slug uniqueness within roster** (JSON Schema Draft 2020-12 cannot
     natively express field-level uniqueness; the `.json` descriptor
     encodes these as `x-alive-uniqueSlugAcross` /
     `x-alive-uniqueSlugPerParent` extension annotations on the relevant
     array properties):
       - `walnut_roster[*].slug` unique across the whole walnut roster
         (`x-alive-uniqueSlugAcross: "slug"`).
       - `people_roster[*].slug` unique across the whole people roster
         (`x-alive-uniqueSlugAcross: "slug"`).
       - `bundle_distribution[*].slug` unique within each parent walnut
         — same bundle slug across different walnuts is fine
         (`x-alive-uniqueSlugPerParent: {slugField: "slug", parentField: "walnut_slug"}`).
       - `anchor_moments[*].slug` unique across all anchor moments
         (`x-alive-uniqueSlugAcross: "slug"`).

What pre-flight does NOT check (deferred to `validate.py`, fn-2-2zz.10):

  * **Cross-reference integrity**: every `bundle.walnut_slug` /
    `anchor_moments[*].walnut_slugs[*]` / `.people_slugs[*]` /
    `relationships[*].{from,to}` resolves to a roster entry.
  * `anchor_moments[*].date` lies within
    `[time_span.start, time_span.end]`.
  * Walnut-must-have-bundle for non-`minimal-life` walnut types.
  * Cross-roster slug collisions (e.g. a walnut and a person sharing a
    slug — the schema doesn't require that, but a downstream materialiser
    that wants disjoint namespaces would).

Split rationale: pre-flight catches anything wrong with one object's
shape; `validate.py` catches anything wrong about how objects refer to
each other. Both share this schema document as their source of truth.

---

## Top-level object

| Key                   | Type    | Notes                                                                                                                                         |
| --------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`      | string  | Required. Always `"0.1"` for this spec. Stage 0 stamps this verbatim from `stage0.SCHEMA_VERSION` so a future bump is a single-source change. |
| `persona`             | object  | Required. The persona summary derived from the description. See § Persona below.                                                              |
| `walnut_roster`       | array   | Required. Each entry is a Walnut object (see below). Length depends on persona complexity + size.                                             |
| `people_roster`       | array   | Required. Each entry is a Person object (see below). May be empty for very minimal personas.                                                  |
| `bundle_distribution` | array   | Required. Each entry is a Bundle object (see below) tagged to a parent walnut by slug.                                                        |
| `time_span`           | object  | Required. ISO 8601 dates (YYYY-MM-DD). See § Time span below.                                                                                 |
| `session_cadence`     | object  | Required. Describes how often squirrel sessions happened. See § Session cadence.                                                              |
| `anchor_moments`      | array   | Required. **Persona-derived count** — Stage 0 picks. Stage 1 confirms. See § Anchor moments.                                                  |

`additionalProperties: false`. No other top-level keys.

---

## Persona

Object describing the human at the centre of the world.

| Key            | Type   | Notes                                                                                                  |
| -------------- | ------ | ------------------------------------------------------------------------------------------------------ |
| `name`         | string | Full name as it would appear in `_kernel/key.md`. Required.                                            |
| `first_name`   | string | First-name fragment for `<persona>'s squirrel` (used by Stage 5 to mint the named squirrel). Required. |
| `label`        | string | Filesystem-safe slug — must satisfy `^[a-z0-9]+(-[a-z0-9]+)*$`. Derived via `lib.derive_label`.        |
| `summary`      | string | One-sentence summary of who they are + what they do.                                                   |
| `tone_hints`   | array  | Up to 3 short strings describing voice / tone the prose-stage subagents should match (e.g. "dry", "warm", "wry"). |

`additionalProperties: false`.

---

## Walnut roster

Each entry describes one walnut — a venture, a person walnut, an experiment,
or a recurring life area. **All slugs MUST satisfy
`^[a-z0-9]+(-[a-z0-9]+)*$`** — this is enforced at emit time by Stage 0
via `lib.is_valid_slug`. Anything that fails is rejected before the spine
is written.

| Key           | Type   | Notes                                                                                                       |
| ------------- | ------ | ----------------------------------------------------------------------------------------------------------- |
| `slug`        | string | Walnut directory name. Must be unique across the roster. Slug regex enforced.                               |
| `name`        | string | Human-readable name (for `_kernel/key.md`).                                                                 |
| `type`        | string | One of `"venture"`, `"experiment"`, `"life-area"`, `"minimal-life"`. Drives bundle-must-exist invariant.    |
| `domain_dir`  | string | One of `"01_Archive"`, `"02_Life"`, `"03_Inbox"`, `"04_Ventures"`, `"05_Experiments"`. ALIVE domain folder. |
| `summary`     | string | One-sentence summary of what this walnut tracks.                                                            |
| `status`      | string | One of `"active"`, `"working"`, `"waiting"`, `"archive"`. Mirrors ALIVE status vocabulary.                  |

`additionalProperties: false`.

---

## People roster

Each entry describes a person who appears in this world. Stage 5 will
materialise these into person walnuts under `02_Life/people/<slug>/`.

| Key             | Type    | Notes                                                                                                  |
| --------------- | ------- | ------------------------------------------------------------------------------------------------------ |
| `slug`          | string  | Person walnut slug. Slug regex enforced. Unique across `people_roster`.                                |
| `name`          | string  | Full name.                                                                                             |
| `relationship`  | string  | Free-text label (e.g. "co-founder", "wife", "investor").                                               |
| `relationships` | array   | Optional. Each entry is `{from: <slug>, to: <slug>, kind: <string>}`. Both endpoints must resolve to a person slug — invariant enforced by `validate.py`. |

`additionalProperties: false`.

---

## Bundle distribution

Each entry describes one bundle, parented to a walnut by slug. Per the
walnut-must-have-bundle invariant (enforced in `validate.py`), every
walnut whose `type != "minimal-life"` must own at least one bundle.

| Key            | Type   | Notes                                                                                       |
| -------------- | ------ | ------------------------------------------------------------------------------------------- |
| `slug`         | string | Bundle slug. Must satisfy slug regex. Unique within its parent walnut.                      |
| `walnut_slug`  | string | Parent walnut slug. Must resolve to a `walnut_roster[*].slug`.                              |
| `name`         | string | Human-readable bundle name.                                                                 |
| `summary`      | string | One-sentence summary of the bundle's deliverable / ongoing concern.                         |
| `status`       | string | One of `"active"`, `"working"`, `"waiting"`, `"archive"`.                                   |

`additionalProperties: false`.

---

## Time span

| Key       | Type   | Notes                                                                                              |
| --------- | ------ | -------------------------------------------------------------------------------------------------- |
| `start`   | string | ISO 8601 date (`YYYY-MM-DD`). The earliest date that may appear in any anchor moment / log entry.  |
| `end`     | string | ISO 8601 date. The latest such date. Must be `>= start`. Validator enforces.                       |

`additionalProperties: false`.

JSON Schema Draft 2020-12 has no native cross-property comparison. The
`start <= end` rule is encoded on the `time_span` node as the
`x-alive-rangeOrder: {start: "start", end: "end"}` extension annotation
in the `.json` descriptor.

---

## Session cadence

| Key                  | Type    | Notes                                                                                                 |
| -------------------- | ------- | ----------------------------------------------------------------------------------------------------- |
| `pattern`            | string  | One of `"daily"`, `"weekly"`, `"sporadic"`. Drives Stage 3 timeline density.                          |
| `sessions_per_week`  | number  | Approximate sessions per week. `0 < sessions_per_week <= 14`.                                         |

`additionalProperties: false`.

---

## Anchor moments

The handful of dates the persona's life genuinely turned on — the moments
the world is anchored around. **Count is persona-derived**: Stage 0 picks
based on description complexity. Stage 1 (next task) confirms each one
individually with the human via `AskUserQuestion`. Stage 0 MUST NOT
collapse the confirmation step — the array shipped in spine.json is a
draft.

Each entry:

| Key       | Type    | Notes                                                                                                              |
| --------- | ------- | ------------------------------------------------------------------------------------------------------------------ |
| `slug`    | string  | Slug for this anchor (used as a key downstream). Slug regex enforced.                                              |
| `name`    | string  | Short headline (e.g. "first angel cheque", "wedding").                                                             |
| `date`    | string  | ISO 8601 date. Must lie within `time_span` (validated by `validate.py`).                                           |
| `summary` | string  | One-paragraph prose summary of why this matters.                                                                   |
| `walnut_slugs` | array | Walnuts touched by this moment. Each entry must resolve to a `walnut_roster[*].slug`.                            |
| `people_slugs` | array | People involved. Each entry must resolve to a `people_roster[*].slug`.                                           |

`additionalProperties: false`.

---

## Stage 0 emit contract (the bit Stage 0 owns)

When Stage 0's subagent finishes, it writes the spine.json file with
`atomic_write_json` (via the brief's stdlib helpers). The dispatcher
(`stage0.py`) then runs `preflight_spine`, which enforces the full
structural schema above (additionalProperties, required keys, enums,
date format, range constraints, slug regex). Cross-reference integrity
and coherence rules (anchor-date-within-time-span,
walnut-must-have-bundle, etc.) are deferred to `validate.py`
(fn-2-2zz.10) — pre-flight is structure-only. Both validators share
this document as their source of truth.

---

## References

- `plugins/alive/skills/demo/stages/stage0.py` — dispatcher + pre-flight.
- `plugins/alive/skills/demo/lib.py` — `is_valid_slug`, `derive_label`,
  `new_world_ulid`.
- `plugins/alive/templates/demo/stage_prompts/stage_0_spine.v1.md` — the
  LLM prompt body.
- `plugins/alive/templates/demo/stage_prompts/summarize_description.v1.md` —
  the >4k-token-description sub-stage.
- `.flow/specs/fn-2-2zz.md` § "Approach" — pipeline architecture.
- `.flow/tasks/fn-2-2zz.10.md` — `validate.py` (the full validator owner).
