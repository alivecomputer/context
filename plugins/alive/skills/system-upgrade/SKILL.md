---
name: alive:system-upgrade
description: "Upgrade from any previous version of the ALIVE Context System. Mines existing structure, detects legacy patterns, handles YAML edge cases, audits sync scripts, and executes a scripted upgrade with rollback."
user-invocable: true
---

# System Upgrade

Upgrade a world from any previous version of the ALIVE Context System to the current version. Handles structural renames, file migrations, terminology updates, legacy cleanup, and integrity verification.

This skill has been battle-tested on a 16GB world with 142 walnuts, 88 bundles, 81 people, and 6 distinct legacy patterns. Every edge case below comes from a real upgrade.

---

## When It Fires

- The session-new hook detects a legacy structure (`.walnut/`, `_core/`, `_capsules/`, `companion.md`)
- The human explicitly invokes `/alive:system-upgrade`
- The human says "upgrade my world", "migrate to the new version", "update alive"

---

## Process

### Phase 1: Mine Existing System

Before touching anything, understand what's there. Dispatch a scout agent (or multiple in parallel) to map the full world structure.

**Core structure scan:**
- `.walnut/` vs `.alive/` — which system folder exists? Do BOTH exist (merge needed)?
- `_core/` vs `_kernel/` — which kernel structure is in use?
- `_capsules/` vs `bundles/` — which bundle structure is in use?
- `companion.md` vs `context.manifest.yaml` — which manifest format exists?
- `now.md` vs `now.json` — which state format is in use?
- Walnut count, people count, bundle/capsule count
- Squirrel entries and their format
- Custom skills, rules, hooks in the human's space
- `.claude/` configuration referencing old paths

**Legacy structure detection (critical — older ALIVE versions used different layouts):**
- Un-numbered domain folders at world root: `archive/`, `life/`, `ventures/`, `experiments/`, `inbox/`, `docs/`, `product/`, `plugin/`, `_working/` — these are from pre-ALIVE-framework eras when domains didn't have numbered prefixes
- `_brain/` folders inside walnuts (v3 era state management)
- `_state/` folders inside walnuts (v3 era)
- Flat walnut structures (key.md at walnut root, no `_core/` or `_kernel/`)
- `src/` or other code directories orphaned at world root from website development
- Any folder at world root that is NOT: `01_Archive/`, `02_Life/`, `03_Inputs/`, `04_Ventures/`, `05_Experiments/`, `People/`, `.alive/`, `.claude/`, or standard dotfiles

**Duplicate walnut detection:**
- Scan for person walnuts that appear in multiple locations (e.g., `people/professional/attila/` AND `people/attila-mora/`)
- Compare key.md content: which is the stub, which has real data?
- Flag all duplicates for human review before upgrade

**World size check:**
- Run `du -sh` on the world root
- If >1GB: warn that git is impractical, recommend tarball backup
- If >5GB: flag specific heavy directories (likely code repos with node_modules)

**Sync script audit:**
- Read `preferences.yaml` `context_sources:` for configured sync scripts
- Check `.claude/scripts/` for any scripts with hardcoded paths
- Flag any that reference `_core/`, `_capsules/`, `.walnut/`, `companion.md`, `now.md`, or un-numbered domain paths like `inbox/` instead of `03_Inputs/`

**Root-level now.md duplicates:**
- Some walnuts have `now.md` at BOTH the walnut root AND inside `_core/`
- Detect all duplicates, flag for merge (use `_core/` version as canonical)

**_core/ directories with only _capsules/ inside:**
- Some `_core/` dirs contain only `_capsules/` (no key.md, now.md, etc.)
- These still need renaming — include them in the kernel rename phase

```
╭─ 🐿️ system scan complete
│
│  Current version: [detected]
│  System folder: [.walnut/ | .alive/ | both]
│  Kernel: [_core/ | _kernel/ | mixed]
│  Bundles: [_capsules/ | bundles/ | mixed]
│  State: [now.md | now.json | mixed]
│  Squirrels: [count] in [location]
│  Custom: [N skills, N rules, N hooks]
│
│  ⚠ Legacy: [un-numbered domains at root | _brain/ folders | etc.]
│  ⚠ Duplicates: [N person walnuts in multiple locations]
│  ⚠ World size: [size] — [git OK | tarball recommended]
│  ⚠ Sync scripts: [N scripts reference old paths]
│
│  Upgrade path: [specific operations needed]
╰─
```

### Phase 2: Visualise Refactor Plan

Generate an interactive HTML visualisation showing what will change. Open it in the browser so the human can review before committing.

**The visualisation shows:**
- Every file/folder that will be renamed or moved (old path -> new path)
- Files that will be converted (companion.md -> context.manifest.yaml, now.md -> now.json)
- Files that won't be touched (log.md, key.md, tasks.md, raw/ contents)
- Risk assessment per change (safe rename / content conversion / potential conflict)
- Estimated scope (number of operations, affected walnuts)
- Legacy folders flagged for cleanup
- Duplicate walnuts flagged for merge

### Phase 3: Ask for Preferences and Permissions

Before executing, confirm with the human:

**Preferences:**
1. Squirrel name — "What should your squirrel be called?" (current name carried over if set)
2. Backup strategy — if world <1GB: "Create a git branch?" If >1GB: "Create a tarball backup of system files?" (always recommended)
3. Batch size — "Upgrade all walnuts at once, or walnut-by-walnut?" (recommended: all at once)
4. Legacy folders — "Clean up [N] legacy folders at world root?" (list them, let human decide)
5. Duplicates — "Merge [N] duplicate person walnuts?" (show pairs, let human pick primary)

**Permissions — batched into one AskUserQuestion:**
1. Approve all (recommended)
2. Review each change type
3. Do a dry run first
4. Cancel

### Phase 4: Execute Upgrade

Generate a Python upgrade script and run it. The script MUST be generated fresh (not fetched from a URL) so the human can review it. Write it to `.alive/_generated/upgrade.py`.

**The script must handle these known edge cases:**

#### YAML Frontmatter Parsing

companion.md and now.md files in the wild contain YAML that breaks standard parsers:
- **Em-dashes** (`—`, `–`) in unquoted description strings
- **Wikilinks** (`[[walnut-name]]`) that YAML interprets as nested flow sequences
- **Colons** in unquoted description strings (e.g., `description: Schema spec for dev: block`)
- **Mixed date formats** (`2026-03-12` vs `2026-03-12T14:00:00` vs `2026-03-12T14:00:00Z`)

The parser must:
1. Try `yaml.safe_load()` first
2. If that fails, sanitize the frontmatter (quote strings with em-dashes, escape wikilinks) and retry
3. If that still fails, fall back to a regex-based key-value extractor
4. Never crash on malformed YAML — warn and use what was extracted

#### companion.md -> context.manifest.yaml Conversion

Field mapping:
- `type: capsule` → removed (inferred from location in bundles/)
- `goal:` → preserved, also used to generate `description:` (first 120 chars)
- `status:` → preserved (known values: draft, prototype, published, done, active)
- `linked_capsules:` → `linked_bundles:`
- `squirrel:` (singular, rare) → normalize to `squirrels:` (plural, as array)
- `sources:` → preserved, but normalize bare strings to `{path: str, description: "", type: "document"}`
- All other frontmatter fields → preserved as-is

Body section handling (body sections vary wildly across worlds):
- `## Context` / `## Summary` / `## Current State` → `context:` YAML field in manifest
- `## Tasks` → extract to `tasks.md` sibling file (if contains `[ ]`, `[x]`, `[~]` markers)
- `## Work Log` → extract to `observations.md` sibling file
- `## Changelog`, `## Decisions`, `## Open Questions`, custom sections → preserved in `context:` or dropped (these are historical, not operational)
- Files with no body → fine, just write frontmatter-only manifest
- Files with no frontmatter → warn, skip

After conversion, rename original to `companion.md.bak` (never delete source material during upgrade).

#### now.md -> now.json Conversion

Field mapping:
- `phase:` → preserved (10+ distinct values exist in the wild: active, building, launching, pre-launch, onboarding, waiting, legacy, planning, ready, complete, starting, retainer-pending)
- `updated:` → preserved as string (normalize to ISO format if possible)
- `capsule:` → rename to `bundle:` (can be null)
- `next:` → preserved
- `squirrel:` → preserved (can be a hex hash OR the word "migration")
- `health:` → dropped (calculated at read time in v2)
- `links:` → dropped (lives in key.md)
- `model:` → dropped (lives in squirrel entry)
- Body (`## Context`, `## Open`, custom headings, or bare prose) → `context:` JSON string

Create `_kernel/_generated/` directory before writing `now.json`. Delete `now.md` after successful conversion.

**Credential scrubbing:** Scan body text for patterns matching `password`, `api[_-]?key`, `secret`, `token`, `Bearer`. Scrub matching lines from the JSON context field. Warn the human about what was removed.

#### _capsules/ Move Semantics (two patterns)

- **_capsules/ inside _core/ (now _kernel/):** These must be MOVED to walnut root `bundles/`, not just renamed. In v2, bundles live at walnut root, not inside the kernel. The move changes relative path depth.
- **_capsules/ at walnut root:** Simple rename to `bundles/`.

#### Template File Exclusion

Skip files in `templates/` directories — these are plugin scaffolding templates, not walnut state:
- `templates/walnut/now.md` — do not convert
- `templates/companion/companion.md` — do not convert

#### Root-level now.md Duplicates

If both `walnut-root/now.md` AND `_core/now.md` (or `_kernel/now.md`) exist:
- Use `_core/` / `_kernel/` version as canonical (it's the system file)
- Delete the root-level duplicate
- If root version has unique content, merge it into the context field

#### Execution Order (matters for safety)

1. **Create backup** — tarball of .walnut/ + all _core/ dirs + all companion.md + all now.md. Write to `.alive/_generated/pre-upgrade-backup.tar.gz`
2. **System folder merge** — `.walnut/` into `.alive/` (handle overlapping squirrels, skills, scripts)
3. **Kernel renames** — `_core/` -> `_kernel/` (deepest first to avoid parent-before-child issues). Include dirs that only contain `_capsules/`.
4. **Bundle moves** — `_capsules/` -> `bundles/` (check both `_kernel/_capsules/` and walnut-root `_capsules/`)
5. **Manifest conversions** — `companion.md` -> `context.manifest.yaml` (with YAML sanitization)
6. **State conversions** — `now.md` -> `_kernel/_generated/now.json` (with credential scrubbing)
7. **Post-upgrade fixups** — find-replace `_core/` -> `_kernel/`, `_capsules/` -> `bundles/`, etc. in all custom skills, scripts, CLAUDE.md files, hooks
8. **Log the upgrade** — write entry to `.alive/log.md`

Log every operation to `.alive/_generated/upgrade-log.yaml` with type, source, target, timestamp, status.

### Phase 5: Verify Everything Works

**Structural verification:**
1. `.alive/` exists with correct structure (`_squirrels/`, `scripts/`, `preferences.yaml`)
2. No `.walnut/` remains
3. No `_core/` kernel directories remaining (find + verify)
4. No `_capsules/` directories remaining
5. No unconverted `companion.md` inside `bundles/` (`.bak` files are OK)
6. No `now.md` remaining in `_kernel/` directories
7. All `_kernel/` dirs have `_generated/` subdirectory
8. All squirrel entries intact and readable
9. Custom skills reference correct paths (grep for `_core/` — should find nothing)
10. Statusline renders without errors

**Root-level hygiene:**
11. No unexpected folders at world root — only `01_Archive/`, `02_Life/`, `03_Inputs/`, `04_Ventures/`, `05_Experiments/`, `People/`, `.alive/`, `.claude/`, and standard dotfiles should exist
12. Flag any legacy folders that remain (un-numbered domains, _working, src, etc.)

**Sync script audit:**
13. Check configured sync scripts for old path references — flag any that will recreate legacy folders
14. Surface specific line numbers and suggested fixes

**Report:**
```
╭─ 🐿️ upgrade complete
│
│  Version: ALIVE Context System v2.0
│  System folder: .alive/
│  Walnuts upgraded: N/N
│  Bundles converted: N/N
│  Custom capabilities updated: N/N
│  Verification: N/N checks passed
│
│  ⚠ [any remaining issues]
│
│  Backup: .alive/_generated/pre-upgrade-backup.tar.gz
│  Log: .alive/_generated/upgrade-log.yaml
╰─
```

---

## Upgrade Paths Supported

| From | To | Operations |
|------|----|-----------|
| Un-numbered domains + `_brain/` + `_state/` | Current v2 | Full restructure — route to numbered domains, convert state |
| `.alive/` + `_core/` + `_capsules/` + `companion.md` | `.alive/` + `_kernel/` + `bundles/` + `context.manifest.yaml` | Kernel rename, bundle move, manifest convert, state convert |
| `.walnut/` + `_core/` + `_capsules/` + `companion.md` | `.alive/` + `_kernel/` + `bundles/` + `context.manifest.yaml` | System folder merge + all above |
| `.walnut/` + `_kernel/` + `bundles/` | `.alive/` + `_kernel/` + `bundles/` | System folder merge only |
| `.alive/` + `_kernel/` + `bundles/` + `context.manifest.yaml` | (current) | Already up to date — verify only |
| Both `.walnut/` AND `.alive/` exist | Merge `.walnut/` into `.alive/` | Handle overlapping squirrels, skills, scripts — keep newer versions |
| No system folder | Fresh install | Redirect to `alive:world` for initial setup |

The upgrade detects what's present and only performs the operations needed. It never forces a full rebuild when a partial upgrade suffices.

---

## Rollback

If something goes wrong mid-upgrade:

1. Check `upgrade-log.yaml` for the last successful operation
2. Offer restore from tarball backup
3. Or offer manual fix for the specific failed operation

The tarball contains every file that was about to be renamed or converted — full restore is always possible.

```
╭─ 🐿️ upgrade issue
│
│  Failed at: converting [path]/companion.md
│  Error: [specific error]
│
│  ▸ Options:
│  1. Skip this one, continue (fix manually later)
│  2. Show me the file so I can fix it
│  3. Restore from backup
╰─
```

---

## What This Skill Does NOT Touch

- **Walnut content** — key.md, log.md, tasks.md, insights, raw files are never modified (only moved within renames)
- **Git history** — no force pushes, no history rewrites
- **Plugin cache** — `~/.claude/plugins/` is managed by Claude Code, not this skill

## What This Skill DOES Audit (but doesn't auto-fix)

- **Sync scripts** — flags old paths, shows the fix, lets the human decide
- **External integrations** — MCP servers, email, Slack sync scripts are surfaced if they reference old structure
- **Legacy folders** — surfaced for human cleanup, never auto-deleted

---

## What System Upgrade Is NOT

- Not `alive:build-extensions` — extensions create new capabilities. Upgrade migrates existing structure.
- Not `alive:system-cleanup` — cleanup fixes broken things in the current version. Upgrade moves between versions.
- Not a fresh install — if no existing system is found, redirect to `alive:world` for initial setup.

Cleanup fixes. Upgrade transforms.
