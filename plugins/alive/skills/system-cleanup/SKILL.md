---
name: alive:system-cleanup
description: "The world feels messy. Stale tasks, orphan folders, v2 remnants, unsaved sessions — entropy is accumulating and needs to be addressed before it compounds. Scans across all walnuts, then surfaces issues one at a time."
user-invocable: true
---

# Tidy

System maintenance. Root health first, then one walnut at a time. Surfaces issues with recommended fixes — the human picks.

Not a dashboard (that's world). Not a search (that's find). Not session history (that's alive:session-history). Pure maintenance.

---

## v3 Architecture Reference

Walnut structure is flat under `_kernel/`:

```
walnut-name/
  _kernel/
    key.md
    log.md
    insights.md
    tasks.json
    now.json
    completed.json
  bundle-a/
    context.manifest.yaml
    raw/
  bundle-b/
    context.manifest.yaml
```

There is NO `_kernel/_generated/` subdirectory. There is NO `bundles/` container directory. Bundles sit as direct children of the walnut root alongside `_kernel/`.

Task operations go through `tasks.py`, never by reading/writing task files directly:
- `tasks.py list --walnut {path}` — all active tasks as JSON
- `tasks.py list --walnut {path} --status active` — filter by status
- `tasks.py summary --walnut {path}` — structured summary with counts

Projection rebuilds go through `project.py`:
- `project.py --walnut {path}` — rebuilds `_kernel/now.json`

---

## Three-Phase Flow

```
Phase 1: Root Audit (system-level, 7 checks — parallel subagents)
    |
Phase 2: Walnut Summary (single subagent scans frontmatter, human picks)
    |
Phase 3: Deep Audit (one walnut, 12 checks — parallel subagents)
```

---

## Subagent Strategy (non-negotiable)

Tidy is read-heavy. Every phase uses subagents to keep the main context clean.

**Discovery subagents MUST use `subagent_type: "Explore"`** — Explore agents have direct file read access without needing Bash. Use Explore for all Phase 1, Phase 2, and Phase 3 discovery checks.

**Fix-execution subagents MUST use `subagent_type: "general-purpose"`** — only general-purpose agents have Write/Edit access. Dispatch one general-purpose agent per approved fix.

- **Phase 1:** Dispatch all 7 root checks as parallel subagents. Wait for all. Present results together — one line per passing check, expand on failures only.
- **Phase 2:** Single subagent reads all walnut frontmatter, returns the health table.
- **Phase 3:** Dispatch all 12 checks as parallel subagents (one check per subagent). Wait for all. Present results together — passing checks collapsed, failures expanded one at a time.

Each subagent gets: the subagent brief (read `$ALIVE_PLUGIN_ROOT/templates/subagent-brief.md` once, prepend to every agent prompt), the check description, what to scan, what constitutes a pass/fail, and instructions to return a structured result (pass/fail + details if fail). **Without the brief, subagents will not understand walnut/bundle structure, tasks.py, or v3 conventions.**

**Never read walnut files in the main context.** All file reading happens inside subagents. The main context only sees results.

---

## Presentation Rules

- **Passing checks:** Collapse to one line. `✓ 3a. now.json zero-context — pass`. No details.
- **Failing checks:** Expand with bordered block, recommended fix, and options. One at a time.
- **After presenting all results:** Walk through failures one at a time for the human to resolve or skip.

---

## Phase 1 — Root Audit

Dispatch 7 subagents in parallel. Each checks one thing across the whole world.

### 1a. ALIVE Structure

Verify all 5 ALIVE folders exist at the world root (`01_Archive/` through `05_Experiments/`).

Pass: all 5 exist. Fail: any missing.

### 1b. Inputs Buffer

Scan `03_Inbox/` for items older than 48 hours. Unrouted inputs may contain decisions or context affecting active walnuts.

Pass: empty or nothing older than 48h. Fail: items found.

```
╭─ 🐿️ tidy — unrouted inputs
│  03_Inbox/ has 3 items older than 48 hours:
│   - vendor-brochure.pdf (3 days)
│   - meeting-notes-feb20.md (4 days)
│
│  ▸ route via alive:capture-context / skip
╰─
```

### 1c. Cross-Walnut Links

Scan ALL walnuts' `_kernel/key.md` frontmatter (`links:` and `parent:` fields) AND body text for `[[wikilinks]]`. Check for:

- **Broken wikilinks** — links pointing to walnut names that don't exist as folders
- **Orphan parents** — `parent:` pointing to a non-existent walnut
- **Undeclared connections** — `[[wikilinks]]` used in body text or log entries but NOT in the frontmatter `links:` field. These are invisible to `alive:search-world` traversal.
- **Structureless folders** — folders in ALIVE domains (02-05) that have no `_kernel/` but look like they should be walnuts

### 1d. Walnut Structural Integrity

Quick scan that every walnut has the full v3 flat `_kernel/` skeleton:
- 3 narrative files: `_kernel/key.md`, `_kernel/log.md`, `_kernel/insights.md`
- 2 data files: `_kernel/tasks.json`, `_kernel/now.json`
- 1 archive file: `_kernel/completed.json`

There is NO `_kernel/_generated/` subdirectory expected. There is NO `bundles/` container directory expected. If either is found, that is a separate check (see 1d does not flag these — checks 3k and 3l handle v2 remnant detection during deep audit).

Pass: all walnuts have all 6 files in flat `_kernel/`. Fail: list what's missing where.

### 1e. Unsaved Squirrel Entries

Scan `.alive/_squirrels/` (world-level) for YAML files where `saves: 0` (never saved) or `signed: false` (legacy schema). Flag entries that have stash items — those contain unrouted decisions/tasks.

Separate entries with stash (need review) from empty shells (safe to clear).

```
╭─ 🐿️ tidy — unsaved sessions
│  3 sessions with unrouted stash:
│   - stellarforge / squirrel:67b1e464 — 4 stash items
│   - glass-cathedral / squirrel:45dcf404 — 6 stash items
│  13 empty shells (no walnut, no stash) — safe to clear.
│
│  ▸ review stellarforge stash / review glass-cathedral stash / clear empty shells / skip
╰─
```

### 1f. Orphan Files at World Root

Flag anything at the world root that isn't an ALIVE folder (`01_Archive/` through `05_Experiments/`), `.alive/`, `.claude/`, or dotfiles. Nothing should live loose at root.

### 1g. Index Staleness

Check if `.alive/_index.yaml` exists and when it was last generated. If it doesn't exist or is older than 7 days, offer to regenerate by running `generate-index.py`.

Pass: index exists and is recent. Fail: missing or stale — offer to regenerate.

### Phase 1 Results

Present all 7 results together. Passing checks get one line. Failures expand.

```
╭─ 🐿️ root audit — 7 checks
│
│  ✓ 1a. ALIVE structure — intact
│  ✓ 1b. Inputs buffer — clean
│  ⚠ 1c. Cross-walnut links — 2 undeclared connections
│  ✓ 1d. Walnut integrity — all complete
│  ⚠ 1e. Unsaved entries — 3 with stash, 13 empty
│  ⚠ 1f. Orphan files — 2 at root
│  ✓ 1g. Index — current
│
│  3 issues to resolve.
╰─
```

Then ask which to fix:

```
╭─ 🐿️ root audit — which to fix?
│
│  1. 2 undeclared cross-walnut connections
│  2. 3 unsaved sessions with stash (11 items total)
│  3. 2 orphan files at world root
│
│  ▸ which ones? (numbers, "all", or "skip")
╰─
```

For each the human picks, propose the specific fix:

```
╭─ 🐿️ proposed fixes
│
│  1. Add [[ryn-okata]], [[jax-stellara]] to stellarforge key.md links:
│  3. Move AGENTS.md → glass-cathedral/raw/,
│     delete disaster-recovery-extraction.md
│
│  ▸ go / change something / skip
╰─
```

**On "go":** dispatch parallel `general-purpose` subagents to execute — one per fix. Each subagent reads the file, makes the edit, returns confirmation. Main context does not touch files.

```
╭─ 🐿️ root audit complete
│  7 checks. 3 issues, 2 fixed, 1 skipped.
│  ▸ continue to walnut audit / done
╰─
```

---

## Phase 2 — Walnut Summary

Single subagent scans all walnuts. For each walnut, read ONLY `_kernel/now.json` (v3 flat path) and `_kernel/key.md` frontmatter (rhythm, type). **Frontmatter only. Do not read full files.**

Return a health table. The main context presents it:

```
╭─ 🐿️ walnut health summary
│
│   #  Walnut               Type         Health    Updated         Rhythm
│   1. glass-cathedral             experiment   active    2 hours ago     weekly
│   2. stellarforge              venture      active    2 days ago      weekly
│   3. nova-station         venture      quiet     12 days ago     weekly    ⚠
│   4. glass-cathedral      experiment   waiting   34 days ago     monthly   ⚠
│
│  Which walnut to audit? (number, or "done" to finish)
╰─
```

Health thresholds (from rhythm):

| Rhythm | Quiet at | Waiting at |
|--------|----------|-----------|
| daily | 2 days | 4+ days |
| weekly | 2 weeks | 4+ weeks |
| fortnightly | 3 weeks | 6+ weeks |
| monthly | 6 weeks | 3+ months |

**Post-migration note:** If many walnuts show today's date with "active" health, the data may reflect migration timestamps rather than genuine recent work. Flag this to the human if detected (e.g., 5+ walnuts all updated on the same day).

---

## Phase 3 — Deep Audit (single walnut)

Dispatch 12 subagents in parallel — one per check. Each subagent reads only the files it needs from the walnut's `_kernel/` and its bundle directories. **Do not read the brief pack in the main context.**

The main context receives pass/fail results from all 12 subagents, presents them together, then walks through failures one at a time.

### 3a. Malformed Files (runs first conceptually — other checks depend on frontmatter)

Scan ALL `.md` files in `_kernel/` and in bundle directories (directories at the walnut root that contain `context.manifest.yaml`). Check each `.md` file starts with `---` followed by valid YAML and a closing `---`.

Pass: all files have frontmatter. Fail: list files without it.

**Fix guidance:** When the human picks "add frontmatter now", read the file body to determine:
- System file → use the schema from conventions (type-specific required fields)
- Bundle manifest → add: type, description, date, squirrel, tags + type-specific fields

### 3b. Walnut Skeleton

Check that all 6 v3 kernel files exist in the flat `_kernel/` directory:
- `_kernel/key.md`
- `_kernel/log.md`
- `_kernel/insights.md`
- `_kernel/tasks.json`
- `_kernel/now.json`
- `_kernel/completed.json`

Pass: all present. Fail: list what's missing.

If `_kernel/now.json` is missing, suggest running `project.py --walnut {path}` to generate it.

### 3c. key.md Completeness

Read `_kernel/key.md` frontmatter and body. Check:
- Required frontmatter fields filled: `type`, `goal`, `created`, `rhythm`, `tags`
- `people:` populated (especially for ventures — empty people on a venture with collaborators is a gap)
- `links:` matches body references — if body text uses `[[wikilinks]]` not declared in `links:`, flag them
- `tags:` not empty

Pass: all fields filled, links match body. Fail: list gaps.

### 3d. now.json Zero-Context

Read `_kernel/now.json` (v3 flat path) and `_kernel/log.md` (frontmatter + first ~100 lines). Apply the zero-context test: "If a brand new agent loaded this walnut with no prior context, would it have everything it needs to continue the work?"

Fail conditions:
- Context paragraph is empty or just a template comment
- Context is one sentence when log shows 3+ recent sessions of substantial work
- Context references things not in the log (hallucinated or outdated)
- `updated:` timestamp is more than 2 weeks old

**Fix guidance:** When the human picks "rewrite now", suggest running `project.py --walnut {path}` to regenerate now.json from current sources. If the log itself is stale, note that project.py will produce a stale projection and suggest a manual log entry first.

### 3e. now.json next: Validation

Read `_kernel/now.json` `next:` field. Use `tasks.py list --walnut {path} --priority urgent` to get urgent tasks. Check:
- `next:` is not empty
- `next:` is not a template placeholder
- If any tasks have urgent priority, does `next:` align with the top urgent task? Flag conflicts.

Pass: next is set and doesn't conflict with urgent tasks. Fail: missing, empty, or conflicting.

### 3f. Log Health

Read `_kernel/log.md` frontmatter and first ~150 lines. Check:
- Entries are prepend-ordered (newest at top, dates descending)
- Recent entries are signed (`signed: squirrel:[session_id]`)
- `entry-count:` in frontmatter is roughly accurate (within +/-5 of actual)
- `last-entry:` in frontmatter matches the top entry's date

Pass: ordered, signed, counts match. Fail: list specific issues.

### 3g. Stale Walnut Past Rhythm

Compare `_kernel/key.md` rhythm against `_kernel/now.json` updated timestamp using the Phase 2 thresholds.

Pass: within rhythm. Fail: quiet or waiting.

```
╭─ 🐿️ tidy — stale walnut
│  nova-station has been quiet for 18 days (rhythm: weekly)
│  Last entry: Feb 5 — "locked episode 11 structure"
│
│  ▸ open it / archive it / change rhythm / skip
╰─
```

### 3h. Bundles and Manifests

Scan bundle directories (direct children of walnut root that contain `context.manifest.yaml`). Check:
- **Orphan raw files** — files in `raw/` subdirectories with no corresponding `context.manifest.yaml` in the parent bundle
- **Manifest schema** — manifests that exist but are missing required fields (`type:`, `description:`, `date:`). A manifest without `description:` is almost as invisible as no manifest.

Pass: all raw files have manifests, all manifests have required fields. Fail: list gaps.

### 3i. Stale Bundle Drafts

Check bundle directories for bundles with `status: draft` in their `context.manifest.yaml` that are older than 30 days (by `date:` field or file modification time).

Pass: nothing older than 30 days in draft. Fail: list stale bundles.

```
╭─ 🐿️ tidy — stale draft
│  submission-draft/ — 39 days in draft status
│  ▸ promote to prototype / archive / delete / skip
╰─
```

### 3j. Completed Bundles Needing Cleanup

Scan bundle directories for bundles whose `context.manifest.yaml` has `status: done` or `status: published`. These bundles have completed their lifecycle and may contain working files, draft iterations, or temporary artefacts that can be cleaned up or archived.

Check each done/published bundle for:
- Temporary or working files (e.g., `*-draft-*.md`, `*.tmp`, `*.bak`)
- Multiple version files where only the final matters (e.g., `proposal-v1.md`, `proposal-v2.md` alongside `proposal-v3.md`)
- Large raw files that could be archived

Pass: no cleanup candidates found. Fail: list bundles with cleanup opportunities.

```
╭─ 🐿️ tidy — completed bundle cleanup
│  shielding-review/ (status: published) has 3 draft iterations
│  ▸ archive drafts / delete drafts / skip
╰─
```

### 3k. v2 Remnant Detection

Check for v2 architecture remnants that need migration:

1. **`bundles/` container directory** — In v3, bundles sit as direct children of the walnut root. A `bundles/` directory at the walnut root is a v2 remnant. Suggest moving its contents up one level.
2. **`_kernel/_generated/` subdirectory** — In v3, `now.json` lives directly in `_kernel/`. A `_generated/` subdirectory inside `_kernel/` is a v2 remnant. Suggest moving `now.json` up and removing the directory.
3. **`tasks.md` without `tasks.json`** — Scan the walnut recursively for `tasks.md` files. If a `tasks.md` exists in a directory that has no corresponding `tasks.json`, flag it as a v2 remnant needing migration. The `tasks.md` format is no longer read by `tasks.py`.

Pass: no v2 remnants found. Fail: list each remnant with migration instruction.

```
╭─ 🐿️ tidy — v2 remnants
│  bundles/ container directory found — 4 bundles inside
│  _kernel/_generated/ directory found — contains now.json
│  2 tasks.md files found without tasks.json counterpart
│
│  ▸ migrate all / review individually / skip
╰─
```

**Migration instructions per remnant type:**
- `bundles/` — move each child directory up to walnut root, then remove empty `bundles/`
- `_kernel/_generated/` — move `now.json` to `_kernel/now.json`, then remove `_generated/`
- `tasks.md` without `tasks.json` — parse tasks.md and create corresponding tasks.json using `tasks.py add`

### 3l. Orphan Folder Detection

Scan all direct child directories of the walnut root. Flag any directory that:
- Is NOT `_kernel/`
- Is NOT `raw/`
- Does NOT contain a `context.manifest.yaml`

These are orphan folders — they exist in the walnut but have no manifest, so they are invisible to the context system. They may be leftover working directories, unmigrated content, or directories that need a manifest added.

Pass: all non-system child directories have manifests. Fail: list orphan folders.

```
╭─ 🐿️ tidy — orphan folders
│  3 folders with no context.manifest.yaml:
│   - scratch-notes/
│   - old-research/
│   - meeting-prep/
│
│  ▸ add manifests / archive / delete / skip
╰─
```

### 3m. Stale Tasks via tasks.py

Use `tasks.py list --walnut {path} --status active` to get all active tasks as structured JSON. Each task includes a `created` date field.

**Stale task detection:** Filter tasks where `created` date is more than 14 days ago and status is still `active` or `todo`. These tasks may be stuck, forgotten, or no longer relevant.

Also check:
- Tasks with `status: blocked` that have no recent log entries mentioning them
- Tasks with a `due` date that has passed

```
╭─ 🐿️ tidy — stale tasks
│  3 tasks older than 14 days with no progress:
│   - t012: "Send post-launch message to Ryn" (created 2026-03-10)
│   - t015: "Review shielding spec" (created 2026-03-05, due: 2026-03-20 — OVERDUE)
│   - t018: "Update API docs" (created 2026-03-01)
│
│  ▸ still relevant / remove / reprioritise / blocked (note why) / skip
╰─
```

### Phase 3 Results

Present all 12 results together. Passing checks collapsed, failures listed.

```
╭─ 🐿️ stellarforge audit — 12 checks
│
│  ✓ 3a. malformed files       — all 34 .md files have frontmatter
│  ✓ 3b. walnut skeleton       — complete (6/6 kernel files)
│  ⚠ 3c. key.md completeness   — links: [] but body references 4 people
│  ✓ 3d. now.json zero-context — pass
│  ✓ 3e. now.json next:        — set, aligns with tasks
│  ✓ 3f. log health            — ordered, signed, counts match
│  ✓ 3g. stale rhythm          — active
│  ✓ 3h. bundles               — all manifests valid
│  ✓ 3i. stale drafts          — none
│  ✓ 3j. completed bundles     — clean
│  ⚠ 3k. v2 remnants           — bundles/ container found, 1 tasks.md without tasks.json
│  ⚠ 3l. orphan folders        — 2 folders without manifests
│  ⚠ 3m. stale tasks           — 3 tasks older than 14 days
│
│  4 issues to resolve.
╰─
```

Ask which to fix:

```
╭─ 🐿️ stellarforge — which to fix?
│
│  1. key.md links: [] but body references 4 person walnuts
│  2. bundles/ container + 1 tasks.md needing migration
│  3. 2 orphan folders without manifests
│  4. 3 stale tasks older than 14 days
│
│  ▸ which ones? (numbers, "all", or "skip")
╰─
```

For each the human picks, propose the specific fix:

```
╭─ 🐿️ proposed fixes
│
│  1. Add [[ryn-okata]], [[jax-stellara]], [[mira-solaris]],
│     [[orion-vex]] to stellarforge `_kernel/key.md` frontmatter links: field
│  2. Move 4 bundle directories from bundles/ to walnut root,
│     migrate tasks.md in submission-draft/ to tasks.json
│  3. Add context.manifest.yaml to scratch-notes/ and old-research/
│
│  ▸ go / change something / skip
╰─
```

**On "go":** dispatch parallel `general-purpose` subagents to execute — one per fix. Each subagent reads the file, makes the edit, returns confirmation. Main context does not touch files.

```
╭─ 🐿️ stellarforge — fixes applied
│
│  ✓ `_kernel/key.md` links updated — 4 person walnuts added
│  ✓ bundles/ migrated — 4 directories moved, tasks.md converted
│  ✓ manifests added — 2 orphan folders now have context.manifest.yaml
│  ✗ stale tasks — skipped
│
│  ▸ audit another walnut / done
╰─
```

If "audit another walnut" — return to Phase 2 summary with updated health flags.
If "done" — Final Summary.

---

## Final Summary

```
╭─ 🐿️ tidy complete
│
│  Root: 7 checks, 3 issues, 2 resolved
│  stellarforge: 12 checks, 4 issues, 3 resolved
│
│  9 resolved, 2 skipped. World is healthy.
╰─
```

After presenting the final summary, write the current date to `.alive/.last_tidy` so the session hook can track when tidy was last run:

```bash
date -u +"%Y-%m-%d" > "$WORLD_ROOT/.alive/.last_tidy"
```
