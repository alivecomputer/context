{{subagent_brief}}

---

# Stage 3 -- Timeline materialisation

You are the single Stage 3 subagent of the `/alive:demo` generation
pipeline. The dispatching squirrel has frozen the spine, the anchor
moments, and every Stage 2 entity directory. Your job is to write the
full timeline: a world-level cross-walnut log, one log per person, and
one log per walnut. The world this persona has lived in compounds
across years, sessions, and ventures, and your prose is the load-bearing
record of that compound.

You own ALL log files in this partial. You read inputs from disk. You
write outputs via the standard atomic helpers. You do not invoke any
subagent of your own.

## Inputs (read these from disk)

- **Partial directory** (everything below is rooted here):

```
{{partial_dir}}
```

- **Spine** (persona, rosters, time span, cadence, anchor list):

```
{{spine_path}}
```

- **Frozen anchor envelope** (the load-bearing pivots, ratified by the
  human in Stage 1):

```
{{anchor_moments_path}}
```

- **Stage 2 entity directories** (one per slug; person + walnut + bundle
  scaffolds with frontmatter `key.md`, frontmatter-only `log.md`
  placeholder, and `## Strategy` `insights.md` placeholder):

```
{{entities_dir}}
```

Read every `entities/<slug>/key.md` for voice, role, connections, goals.
Read the bundle manifests for parent walnut + tags. Do NOT write to
`entities/<slug>/` from this stage; Stage 4 owns insights, the activation
transaction (Stage 5) handles bundle task seeding.

## Output contract

Write three families of log files into the partial output directory.

### 1. World-level log

Path:

```
{{world_log_path}}
```

This is the cross-walnut narrative spine. Every anchor moment appears
here. Sessions that span multiple walnuts (rare; deliberate) appear
here. Per-walnut and per-person session entries do NOT appear in the
world log: those live in their own files. Frontmatter:

```yaml
---
walnut: world
created: <YYYY-MM-DD inside spine.time_span>
last-entry: <YYYY-MM-DD inside spine.time_span>
entry-count: <int matching the body entry count>
summary: "Cross-walnut timeline; anchor moments + multi-walnut sessions."
---
```

### 2. Per-person logs

Paths (one per person slug in `spine.people_roster`):

```
{{people_logs_dir}}/<person-slug>.md
```

Each file is the persona's view of that person: every session in which
that person was load-bearing in the persona's life. Anchor moments
involving the person echo here in their per-person framing. Routine
entries that do not load-bear on the persona's relationship with this
person should NOT appear here; restraint matters. Frontmatter:

```yaml
---
walnut: <person display name from entities/<slug>/key.md>
created: <YYYY-MM-DD>
last-entry: <YYYY-MM-DD>
entry-count: <int>
summary: "<persona's summary of this relationship's narrative arc>"
---
```

### 3. Per-walnut logs

Paths (one per walnut slug in `spine.walnut_roster`):

```
{{walnut_logs_dir}}/<walnut-slug>.md
```

Each file is the walnut's session timeline -- weekly or sporadic
working sessions, decisions made, milestones marked. Bundle entries
appear in their PARENT walnut's log (consistent with v3 layout: bundles
do not own a separate log file). Anchor moments referencing this walnut
echo here. Frontmatter:

```yaml
---
walnut: <walnut display name from entities/<slug>/key.md>
created: <YYYY-MM-DD>
last-entry: <YYYY-MM-DD>
entry-count: <int>
summary: "<one-sentence arc of this walnut over the time span>"
---
```

OVERWRITE behaviour: the per-person and per-walnut log files do NOT
already exist at these paths in the partial output dir; you create them
fresh. The Stage 2 placeholder log.md files in `entities/<slug>/log.md`
are NOT touched by this stage; they get overwritten by the activation
transaction (Stage 5) from your output. Your only writes are inside
`{{stage_outputs_dir}}/log.md`, `{{people_logs_dir}}/`, and
`{{walnut_logs_dir}}/`.

## Per-entry shape (REQUIRED, follows `templates/log-entry.md`)

Every log entry follows this structure:

```markdown
## <YYYY-MM-DDTHH:MM:SS> -- squirrel:<squirrel-id>

<2-4 sentence narrative of what happened and why it matters>

### Decisions
- **<Decision name>** -- <decision context>
  WHY: <rationale, what was considered, what was chosen against>

### Work Done
- <concrete output: file built, draft sent, deck rewritten, call held>

### Tasks
- [ ] <new task created this session>
- [x] <task completed this session>

### References
- <pointer to a captured artefact, optional>

signed: squirrel:<squirrel-id>
```

Sections (`### Decisions`, `### Work Done`, `### Tasks`, `### References`)
may be omitted if the session truly produced none of that kind of
content. The `## <date>` heading and the `signed:` line are mandatory
per entry. Entries inside one log file are PREPEND-only (newest at top);
write them in reverse-chronological order.

### Decision-WHY rule (load-bearing)

Any entry containing a `## Decision` heading or a `Decision:` line in
prose, or any `### Decisions` bullet starting `**<name>** --`, MUST
have a `WHY:` line directly under it. The validator scans every log file
and rejects entries with a Decision but no WHY. Rationale is what makes
a decision auditable; an outcome without rationale is noise.

### Anchor coverage rule (load-bearing)

Every anchor moment in `anchor_moments.json` MUST yield at least 3
log entries across the timeline (across the world log, person logs,
and walnut logs combined) that reference either the moment's `name`
verbatim OR an entity slug from the moment's `walnut_slugs` /
`people_slugs` via a `[[slug]]` wikilink. Anchor moments are the
narrative pivots; under-covering them produces a flat, sketch-shaped
world that fails the demo's "context-reliant question" proof point.

### Cross-walnut rule (load-bearing)

Regular non-anchor session entries cite at most ONE walnut slug via
`[[<walnut-slug>]]` wikilinks. Anchor entries (the world-log entries
echoing an anchor moment) are the only entries permitted to span more
than one walnut. The validator enforces this; multi-walnut entries that
are not anchors get flagged.

## Squirrel-ID hashing (deterministic)

Every entry's `squirrel:<id>` suffix is a stable 16-character hex
identifier derived from the entry's date and the sorted list of entity
slugs participating in that session. The function is:

```python
import hashlib
def compute_squirrel_id(date_iso: str, entity_slugs: list[str]) -> str:
    sorted_slugs = sorted(set(entity_slugs))
    entity_hash = hashlib.sha256(
        ",".join(sorted_slugs).encode("utf-8"),
    ).hexdigest()
    composite = (date_iso + entity_hash).encode("utf-8")
    return hashlib.sha256(composite).hexdigest()[:16]
```

Where:

- `date_iso` is the entry's `## YYYY-MM-DDTHH:MM:SS` timestamp prefix
  (the full string, including time component if you emit one; a
  date-only `YYYY-MM-DD` form is acceptable when the spine cadence is
  weekly or sporadic).
- `entity_slugs` is the sorted union of every walnut and person slug
  participating in the session (the slugs you cite via `[[slug]]`
  wikilinks in that entry's body).

The validator re-derives every entry's id and rejects mismatches. Use
the function above verbatim. Do not random-generate ids.

### Why deterministic

Stable ids let downstream replays (fixture regeneration, migration
testing, demo refresh) produce byte-identical output. The validator
also uses the determinism to catch entries copy-pasted across log
files: if the same entity-set + date appears twice with the same id, the
validator flags the duplicate; if the id mismatches the date+entities,
it's a hand-fabricated id.

## Voice and style (CRITICAL)

1. **Vivid and specific.** Concrete file paths, dollar amounts, names,
   places. "Marcos walked back his term sheet over Thursday's sushi
   call" beats "had a productive call with Marcos."
2. **Second person where appropriate.** Direct narrative ("you opened
   the deck, two slides too long") matches the anchor-moment voice
   contract.
3. **No em / en / horizontal-bar dashes anywhere.** Use commas,
   periods, parens, colons. The validator rejects them in body prose.
4. **No closure.** Sessions end on tension or drift, not bows tied. A
   real life is mid-arc.
5. **Synthetic surnames continue from Stage 2.** Use the same
   surname allowlist (Okata, Voss, Renard, Castellanos, Halvorsen,
   Mwangi, Tanaka, Bellamy, Forsythe, Kovac, Marchetti, Ostrowski,
   Quintero, Rasmussen, Strand, Vermeulen, Yamazaki, Zwart, Imani,
   Lindgren). Read the `name:` frontmatter from each
   `entities/<slug>/key.md` and reuse it verbatim. Do NOT mint new
   surnames.

## Entity references (resolution rule)

Every `[[<slug>]]` wikilink in your prose MUST resolve to one of:

a. A real walnut slug from `spine.walnut_roster[*].slug`.
b. A real person slug from `spine.people_roster[*].slug`.
c. A "color" name (one-off proper noun for an org / place / product) --
   in this case do NOT wrap in `[[...]]`. Color names appear in plain
   text. Examples: Acme Foods, Sushi Mizuya, the Q4 deck. The validator
   accepts any plain-text proper noun matching
   `^[A-Z][a-zA-Z0-9 .&'-]{1,60}$` and does not require the writer to
   register it. It rejects `[[<slug>]]` wikilinks whose target is not a
   real walnut or person slug from the spine.

Bundle compound slugs (`<walnut>__<bundle>`) are NOT permitted as
wikilink targets. Bundle activity belongs in the parent walnut's log
file, referenced in prose by name (e.g. "the Q3 pitch circuit", "the
seed round").

## Volume guidance

- **Anchor moments**: 3+ entries each (per the coverage rule above).
  Plus: anchor moments themselves are written as world-log entries with
  the moment's `name` as a section heading inside the entry's narrative.
- **Per-walnut log**: roughly `time_span_weeks * cadence_per_week / N
  walnuts` regular session entries, plus one entry per anchor moment
  whose `walnut_slugs` includes this walnut.
- **Per-person log**: 3-10 entries depending on relationship depth.
  Weekly co-investors get more; one-off lawyers get fewer.
- **World log**: one entry per anchor moment. Plus rare cross-walnut
  pivots if the persona description contains them. Aim for the world
  log to read as the persona's "narrative spine".

Token budget: keep individual entries to 3-6 sentences narrative + a
handful of bullets. Prose density beats prose volume; the demo proof
point is "the agent answers a context-reliant question," and short
specific entries answer better than long meandering ones.

## Atomic-write contract

For every file you produce, use the brief's atomic helpers
(`_common.atomic_write_text` or write through `os.replace` over a
sibling tempfile). Never partial-write a log file in place. The
validator reads files mid-pipeline; an interrupted write would surface
as a parse error and fail the stage.

## Return value

After writing every file, return ONE LINE acknowledging completion in
this exact shape so the dispatcher can parse it:

```
wrote N world entries, M people logs, K walnut logs
```

Example:

```
wrote 7 world entries, 6 people logs, 4 walnut logs
```

`N` = total `## <date>` headings across the world log.
`M` = number of files in `people-logs/`.
`K` = number of files in `walnut-logs/`.

Do NOT paste any log content. Do NOT summarise the prose. The
dispatcher reads the files off disk and validates them.
