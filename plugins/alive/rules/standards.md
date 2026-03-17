---
version: 1.0.1-beta
type: foundational
description: Frontmatter, file naming, signing, wikilinks, third-party skill overrides.
---

# Standards

The infrastructure that prevents entropy. Every file follows these. No exceptions.

---

## Frontmatter on Everything

**This is the most important convention in the system.**

Every `.md` file in the system has YAML frontmatter. No exceptions. The frontmatter is the scannable layer — a squirrel reads frontmatter before bodies. If a file doesn't have frontmatter, it's malformed.

| File type | Required frontmatter |
|-----------|---------------------|
| System files (key, now, log, insights, tasks) | Schema defined in world.md |
| Capsule companions | type: capsule, goal, status, version, sensitivity, pii, sources, linked_capsules, tags |
| Working files (legacy) | squirrel, model, version, previous, kept, changed |
| Reference companions (legacy) | type, description, type-specific fields, squirrel, tags |
| Rules | version, type, description |
| Skills | name, description, user-invocable |

**Every companion must have `description:` in frontmatter.** This is the one-line scan that tells the squirrel what the reference contains without reading the body. It's the difference between a useful reference system and a pile of files.

---

## Signing

Every file the squirrel creates or modifies carries attribution:

- `squirrel: [session_id]` — which session created/modified it
- `model: [engine]` — which AI model was running

Log entries are additionally signed at the end: `signed: squirrel:[session_id]`

Squirrel entries carry the full metadata: session_id, runtime_id, engine, walnut, timestamps.

---

## File Naming

### Capsule Folders (_core/_capsules/)

Pattern: `kebab-case-descriptive-name/`

The folder name IS the capsule identity. Short, clear, unique within the walnut.

```
shielding-review/
launch-checklist/
festival-submission/
```

### Capsule Versions

Versions are files inside the capsule. No version in folder name. File names include the capsule name for self-documentation.

Pattern: `{capsule-name}-draft-{nn}.md` for working drafts, `{capsule-name}-v1.md` for shipped.

```
_core/_capsules/shielding-review/
  companion.md                          <- THE INDEX
  shielding-review-draft-01.md          <- first draft
  shielding-review-draft-02.md          <- iterated
  shielding-review-v1.md                <- graduated
  raw/                                  <- source material
    2026-02-23-jax-review.mp3
    2026-02-20-vendor-proposal.pdf
```

### Raw Files (inside capsules)

Pattern: `YYYY-MM-DD-descriptive-name.ext`

```
2026-02-23-jax-shielding-review.mp3
2026-02-20-vendor-proposal.pdf
```

### Raw File Renaming

Garbage filenames get renamed on import. The original name is preserved in the companion frontmatter as `original_filename:`.

| Before | After |
|--------|-------|
| `CleanShot 2026-02-23 at 14.32.07@2x.png` | `2026-02-23-competitor-pricing-screenshot.png` |
| `IMG_4892.jpg` | `2026-02-20-prototype-photo.jpg` |
| `Document (3).pdf` | `2026-02-18-vendor-proposal-v3.pdf` |

### Companion Files

Same name as the raw file, `.md` extension. Lives alongside the raw file's parent directory (not inside `raw/`).

```
_core/_capsules/transcripts/
  2026-02-23-jax-shielding-review.md      <- companion
  raw/
    2026-02-23-jax-shielding-review.mp3   <- raw
```

---

## Wikilinks

`[[walnut-name]]` is the syntax for linking walnuts together. It's a text convention — not rendered, not enforced by tooling. It's a marker that says "this references another walnut."

**Where to use them:**
- `_core/key.md` frontmatter `links:` field — the canonical list of connections
- `_core/key.md` body — in the Connections and Key People sections
- `_core/log.md` entries — inline when referencing other walnuts ("discussed with [[ryn-okata]]")
- `_core/insights.md` — when an insight relates to another walnut

**Rules:**
- If you add a `[[link]]` inline, also add it to key.md `links:` frontmatter
- `alive:find` searches for these across all walnuts
- Person walnuts use `[[first-last]]` format (e.g., `[[ryn-okata]]`)
- Walnut names use kebab-case (e.g., `[[nova-station]]`, `[[glass-cathedral]]`)

---

## Third-Party Skill Overrides

External plugins (superpowers, etc.) assume standard code project layouts — `docs/plans/`, `src/`, `tests/`. These paths don't exist in a World. The squirrel must intercept and reroute.

**Before any skill creates a file, ask:** "Am I in a walnut? Where does this belong?"

| Skill output | Route to instead |
|-------------|-----------------|
| `docs/plans/*.md` | `{active-walnut}/_core/_capsules/{capsule-name}/` (create capsule if needed) |
| `docs/*.md` | `{active-walnut}/_core/_capsules/{capsule-name}/` |
| Any file at World root | Determine the walnut it belongs to, route to a capsule |

**No orphan files at World root.** The only things at root level are the 5 ALIVE domain folders, `.claude/`, and dotfiles. Everything else belongs inside a walnut.

If no walnut is active and a skill wants to write a file, ask the human where it should go before writing.
