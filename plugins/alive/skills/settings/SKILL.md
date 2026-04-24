---
name: alive:settings
description: "The human wants to adjust how the system behaves — not what it contains, but how it feels. Voice, rhythm, preferences, walnut-level config, full permissions toggleable, named squirrel config, action logging toggle. The system adapting to them. Routes to preferences.yaml or walnut config.yaml depending on scope. For creating new skills, rules, or hooks, route to alive:build-extensions instead."
user-invocable: true
---

# Tune

Adjust how the ALIVE Context System works. Two levels: world-wide preferences and per-walnut config.

For creating new skills, rules, and hooks — that's `alive:build-extensions`.

---

## The Spectrum

| Level | What it is | Example | Where it lives |
|-------|-----------|---------|---------------|
| **Preference** | Toggle on/off | "Turn off sparks" | `.alive/preferences.yaml` |
| **Config** | Walnut-level setting | "Nova Station should have a technical voice" | `_kernel/config.yaml` in the walnut |

**The line:** Toggle = preference. Setting = config. Process or capability = `alive:build-extensions`.

## How It Routes

When the human says "I want X":

1. **Is it a toggle?** → Write to `preferences.yaml`. Takes effect immediately.
2. **Is it walnut-specific?** → Write YAML config to that walnut's `_kernel/config.yaml`. Different walnuts, different settings.
3. **Is it a repeatable process or new capability?** → Route to `alive:build-extensions`.

If unclear, ask once:

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
always_watching: true          # Background instincts: people, bundle progress, capturable content
save_prompt: true              # Ask "anything else?" before save

# World behavior
health_nudges: true            # Surface stale walnut warnings proactively
feedback_nudges: true          # Suggest /alive:feedback after errors or hook failures

# Permissions
full_permissions: true         # All auto-approve permissions active (toggle off to require confirmation)

# Squirrel identity
squirrel_name: null            # Named squirrel config (e.g., "oak", "hazel") — null for default

# Logging
action_logging: false          # Log every tool call and action to .alive/action-log.yaml

# Display
theme: vibrant                 # vibrant | minimal | clean (companion app)
```

Set any key to `false` to disable. Takes effect next session (or after `/compact`).

### Context Sources

External context the system knows about. Used by `alive:world` (dashboard), `alive:session-history` (session timeline), `alive:search-world` (query), and `alive:capture-context` (import).

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

Per-walnut settings in `_kernel/config.yaml`:

```yaml
# _kernel/config.yaml
voice:
  character: [technical, precise, confident]
  blend: 90% sage, 10% rebel
  never_say: [basically, essentially, it's worth noting]
rhythm: daily
capture:
  default_mode: deep            # override fast default for this walnut
  auto_types: [transcript, email]  # always deep capture these types
```

**Voice scope:** Voice can be set at world level in `preferences.yaml` (default for all walnuts) or at walnut level in `_kernel/config.yaml` (overrides the default for that walnut). If both exist, walnut-level takes precedence. Most users set voice at world level only.

**Backward compat:** If `_kernel/` doesn't exist, check walnut root for `config.yaml`.

---

## System Audit

"How am I using this?" triggers an audit:

```
╭─ 🐿️ system audit
│
│  Preferences: 6 set (all defaults)
│  Walnuts: 14 total (5 active, 4 quiet, 3 waiting, 2 archived)
│  Sessions: 47 squirrel entries across all walnuts
│  Bundles: 89 total (62 with manifests, 27 missing manifests)
│  Custom skills: 0
│  Plugins: 1 (alive core)
│
│  Recommendation: run alive:system-cleanup to address 27 incomplete bundles
│  and stale drafts.
╰─
```

---

## Adapt Mode

Wrap third-party tools to be walnut-native:

"I use Notion for project management. Can the ALIVE Context System work with it?"

Draft an adapter concept, then route to `alive:build-extensions` for implementation. MCP integration where possible, manual import flow where not.

---

## Version Control

**System files** (hooks, core rules, skills) → always updated by plugin, never modified by the human.
**Customization files** (preferences.yaml, voice config, walnut-level config) → never touched by plugin updates.
**Hybrid files** (some rules) → version-tagged in frontmatter. On plugin update, if the human modified the file, present diff instead of overwriting.

Every rules file has `version:` in frontmatter. Update compares checksums.
