---
name: config
description: "Use when the human wants to change how the system behaves — toggle preferences, apply walnut-level voice or capture settings, codify a repeatable process as a custom skill, or audit current configuration. Routes to preferences.yaml, walnut config, or a new skill file depending on scope."
user-invocable: true
---

# Config

Customize how the system works. Four levels, from simplest to most complex.

---

## The Spectrum

| Level | What it is | Example | Where it lives |
|-------|-----------|---------|---------------|
| **Preference** | Toggle on/off | "Turn off sparks" | `preferences.yaml` |
| **Config** | Walnut-level setting | "Nova Station should have a technical voice" | YAML config in walnut's `_core/` |
| **Skill** | Repeatable workflow | "When I paste a transcript, always extract action items" | Custom skill .md file |
| **Plugin** | Distributable package | "Package my launch checklist for other teams" | Plugin directory structure |

**The line:** Toggle = preference. Setting = config. Process = skill. Shareable = plugin.

## How It Routes

When the human says "I want X":

1. **Is it a toggle?** → Write to `preferences.yaml`. Takes effect immediately.
2. **Is it walnut-specific?** → Write YAML config to that walnut's `_core/`. Different walnuts, different settings.
3. **Is it a repeatable process?** → Draft a custom skill .md and install it.
4. **Is it something others could use?** → Structure as a plugin with manifest.

If unclear, the squirrel asks once:

```
╭─ 🐿️ that sounds like a preference (toggle).
│  Add to preferences.yaml?
│  Or is this walnut-specific config?
╰─
```

---

## Preferences

`.alive/preferences.yaml` — read by session-start hook via `alive-resolve-preferences.sh`.

### Toggle Keys (default: all ON)

```yaml
# Squirrel behavior
spark: true                    # The Spark observation at walnut open
show_reads: true               # Show ▸ indicators when loading files
stash_checkpoint: true         # Shadow-write stash to squirrel YAML every 5 items / 20 min
always_watching: true          # Background instincts: people, working fits, capturable content
save_prompt: true              # Ask "anything else?" before save

# World behavior
health_nudges: true            # Surface stale walnut warnings proactively

# Display
theme: vibrant                 # vibrant | minimal | clean (companion app)
```

Set any key to `false` to disable. Takes effect next session (or after `/compact`).

### Context Sources

External context the system knows about. Used by `alive:world` (dashboard), `alive:recall` (search), `alive:find` (query), and `alive:capture` (import).

```yaml
context_sources:
  gmail:
    type: mcp_live             # live API via MCP server
    status: active
    walnuts: all
  slack:
    type: sync_script          # pulled by script
    script: .claude/scripts/slack-sync.mjs
    status: active
    walnuts: all
  chatgpt:
    type: static_export        # one-time export file
    path: ~/exports/chatgpt/conversations.json
    status: indexed
    walnuts: all
```

Source types: `mcp_live`, `sync_script`, `static_export`, `markdown_vault`.
Status: `active` (live), `indexed` (imported), `available` (registered, not imported).
Scoping: `walnuts: all` or `walnuts: [nova, gtm]` for specific walnuts only.

---

## Walnut-Level Config

Per-walnut settings in `_core/config.yaml`:

```yaml
# _core/config.yaml
voice:
  character: [technical, precise, confident]
  blend: 90% sage, 10% rebel
  never_say: [basically, essentially, it's worth noting]
rhythm: daily
capture:
  default_mode: deep            # override fast default for this walnut
  auto_types: [transcript, email]  # always deep capture these types
```

---

## Custom Skills

When a process should always happen the same way:

```
╭─ 🐿️ that sounds like a repeatable workflow.
│  Want me to draft it as a custom skill?
│
│  It would fire when: [trigger description]
│  It would do: [steps]
╰─
```

Custom skills live in `.claude/skills/` or the plugin's skills directory.

---

## System Audit

"How am I using this?" triggers an audit:

```
╭─ 🐿️ system audit
│
│  Preferences: 6 set (all defaults)
│  Walnuts: 14 total (5 active, 4 quiet, 3 waiting, 2 archived)
│  Sessions: 47 squirrel entries across all walnuts
│  References: 89 captured (62 indexed, 27 missing from key.md)
│  Working files: 23 drafts (4 older than 30 days)
│  Custom skills: 0
│  Plugins: 1 (walnut core)
│
│  Recommendation: run alive:housekeeping to address 27 unindexed refs
│  and 4 stale drafts.
╰─
```

---

## Adapt Mode

Wrap third-party tools to be walnut-native:

"I use Notion for project management. Can walnut work with it?"

The squirrel drafts an adapter — a custom skill that bridges the external tool's data model with the walnut structure. MCP integration where possible, manual import flow where not.

---

## Version Control

**System files** (hooks, core rules, skills) → always updated by plugin, never user-modified.
**User files** (preferences.yaml, voice config, custom skills, walnut-level config) → never touched by plugin updates.
**Hybrid files** (some rules) → version-tagged in frontmatter. On plugin update, if user modified the file, present diff instead of overwriting.

Every rules file has `version:` in frontmatter. Update compares checksums.
