---
name: alive
version: 0.1.0-beta
description: Personal private context infrastructure. Structured files, a caretaker runtime, and 9 skills that turn your machine into an alive computer.
author: Alive Computer
homepage: https://alivecomputer.com
repository: https://github.com/alivecomputer/alive
community: https://skool.com/worldbuilders
license: MIT
---

# ALIVE

**Personal Private Context Infrastructure**

Your context is your property. ALIVE is the infrastructure that makes it real — structured files on your machine, a caretaker runtime (the Squirrel), and 9 skills that turn your computer into an alive computer.

No cloud dependency. No vendor lock. Plain markdown files you own forever.

## Install

```bash
claude plugin install alivecomputer/alive
```

## What You Get

- **9 skills:** world, open, save, capture, find, create, housekeeping, config, recall
- **6 rules:** behaviors, conventions, voice, squirrels, world, human
- **11 hooks:** session lifecycle, log protection, rule guarding, archive enforcement, stash preservation
- **Templates:** walnut scaffold, 7 companion types, squirrel entry, world setup

## Skills

| Skill | Command | What it does |
|-------|---------|-------------|
| World | `/alive:world` | Dashboard. See your whole world. |
| Open | `/alive:open` | Open one walnut. Focus. Work. |
| Save | `/alive:save` | Checkpoint. Route stash. Keep working. |
| Capture | `/alive:capture` | Bring context in. Store and route. |
| Find | `/alive:find` | Search across everything. |
| Create | `/alive:create` | Scaffold a new walnut. Map context sources. |
| Housekeeping | `/alive:housekeeping` | System maintenance. One issue at a time. |
| Config | `/alive:config` | Customize how it works. |
| Recall | `/alive:recall` | Rebuild context from past sessions. |

## The Architecture

```
PROPERTY (yours, portable, permanent):
  Framework: ALIVE       5 domains — Archive, Life, Inputs, Ventures, Experiments
  Unit: walnut            context container with _core/
  Files: plain markdown   zero vendor lock

RUNTIME (swappable):
  Caretaker: squirrel     rules + skills + hooks
  Model: any LLM          Claude, GPT, Gemini, local — interchangeable

STORAGE (portable):
  Default: local fs       your machine
  Sync: iCloud / Dropbox  your cloud

INTERFACE (adaptable):
  Current: Claude Code    first adapter
  Future: Cursor, Codex, Windsurf, standalone
```

## The Walnut

A walnut is the unit of context. Any meaningful thing with its own identity, lifecycle, and history.

```
my-project/
  _core/
    key.md          what it is
    now.md          where it is right now
    log.md          where it's been
    insights.md     what's known
    tasks.md        what needs doing
    _squirrels/     session history
    _working/       drafts and versions
    _references/    source material
  docs/             your work (live context)
  src/
```

Everything inside `_core/` is system. Everything outside is yours.

## License

MIT. Open source. Build your world.
