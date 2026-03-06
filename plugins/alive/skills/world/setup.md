---
name: setup
description: First-time world creation. Triggered automatically when alive:world detects no existing ALIVE structure.
internal: true
---

# Setup

First time. No ALIVE folders exist. You just installed ALIVE. Make it feel like something just came alive.

---

## Detection

`alive:world` checks for `01_Archive/`, `02_Life/`, etc. If none found → this fires.

## Flow

### 1. Welcome

```
╭─ 🐿️ welcome
│
│  No world found. Let's build one.
│
│  This takes about 3 minutes. I'll create the folder structure,
│  set up your first walnut, and configure your context sources.
│
│  Ready?
╰─
```

### 2. Identity

→ AskUserQuestion: "What's your name?"
- First name. Stored in `.alive/key.md`. The squirrel uses it everywhere.

→ AskUserQuestion: "Where should your world live?"
- Default: current directory
- Other: type a path

### 3. Create ALIVE Structure

```
╭─ 🐿️ building your world...
│
│  ▸ 01_Archive/
│  ▸ 02_Life/
│  ▸ 02_Life/people/
│  ▸ 02_Life/goals/
│  ▸ 03_Inputs/
│  ▸ 04_Ventures/
│  ▸ 05_Experiments/
│  ▸ .alive/key.md (your identity)
│  ▸ .alive/preferences.yaml (defaults)
│  ▸ .alive/overrides.md (your customizations)
│  ▸ .alive/rules/ → .claude/rules/ (6 rules symlinked)
│  ▸ .alive/agents.md → .claude/CLAUDE.md (symlinked)
│  ▸ .alive/_squirrels/ (session history)
│
│  Done. Five domains. Your world is alive.
╰─
```

### 4. Context Sources

→ AskUserQuestion: "Where does your existing context live? Pick all that apply."
- Options: ChatGPT, Claude Desktop, Gmail, Slack, Fathom/Otter, Apple Notes, Notion, WhatsApp, None yet
- multiSelect: true

For each selected source, ask for the path or confirm it's an MCP integration.

Add context sources to `.alive/preferences.yaml` under the `context_sources:` key. Each source gets `status: available` — the system knows they're there but hasn't processed them yet.

```
╭─ 🐿️ context sources registered
│
│  ▸ ChatGPT — ~/exports/chatgpt/ (available — not yet indexed)
│  ▸ Gmail — MCP live (active)
│  ▸ Fathom — ~/exports/fathom/ (available — not yet indexed)
│
│  These won't be loaded by default. The system knows they exist
│  and can search them when relevant context might be there.
│  Run /alive:recall to browse them anytime.
╰─
```

### 5. First Walnut

→ AskUserQuestion: "What's the most important thing you're working on right now?"
- Free text. This becomes the first walnut.

→ AskUserQuestion: "Is that a venture (revenue), experiment (testing), or life goal?"
- Routes to the right ALIVE domain.

Create the walnut with `_core/` structure. Pre-fill `_core/key.md` from their answer.

```
╭─ 🐿️ first walnut created
│
│  ▸ 04_Ventures/nova-station/
│  ▸   _core/key.md — "Build the first civilian orbital platform"
│  ▸   _core/now.md — phase: starting
│  ▸   _core/log.md — first entry signed
│  ▸   _core/insights.md — empty, ready
│  ▸   _core/tasks.md — empty, ready
│  ▸   _core/_squirrels/
│  ▸   _core/_working/
│  ▸   _core/_references/
│
│  Your first walnut is alive.
╰─
```

### 6. Done

```
╭─ 🐿️ your world is alive
│
│  World: /path/to/your/world
│  First walnut: nova-station (04_Ventures/)
│
│  9 skills ready:
│    world · open · save · capture · find · create · housekeeping · config · recall
│
│  Say "open nova-station" to start working.
│  Say "world" anytime to see everything.
│  Say "save" to checkpoint your work.
│
│  Build your world.
╰─
```

---

## What Setup Creates

| Path | Purpose |
|------|---------|
| `01_Archive/` | Graduated walnuts |
| `02_Life/people/` | Person walnuts |
| `02_Life/goals/` | Life goals |
| `03_Inputs/` | Buffer — route out within 48h |
| `04_Ventures/` | Revenue intent |
| `05_Experiments/` | Testing grounds |
| `.alive/key.md` | World identity (name, goal, timezone) |
| `.alive/preferences.yaml` | Toggles and context sources |
| `.alive/overrides.md` | Your rule customizations (never overwritten) |
| `.alive/_squirrels/` | Centralized session entries |
| `.alive/rules/*.md` | 6 rules (originals, symlinked to `.claude/rules/`) |
| `.alive/agents.md` | Runtime contract (symlinked to `.claude/CLAUDE.md`) |
| `[first-walnut]/_core/` | Full walnut structure |

## What Setup Does NOT Do

- Import existing context (use `/alive:recall` to progressively search and index)
- Set up API integrations (use `/alive:config`)
- Configure voice (defaults are fine, customize later via `/alive:config`)
- Create multiple walnuts (one is enough to start)
