# ALIVE Context System -- Hermes Agent Memory Plugin

**Personal context manager.** File-based, fully local, zero dependencies.

ALIVE gives your Hermes Agent structured persistent memory through *walnuts* -- context units with identity, history, domain knowledge, and current state. Everything lives on your filesystem. Nothing phones home.

## Install

1. Install the ALIVE system: `claude plugin install alive@alivecontext`
2. Set up your world (or use an existing one)
3. In Hermes: `hermes memory setup` -> select `alive`
4. Set `ALIVE_WORLD_ROOT` to your world path (or it auto-detects `~/world`)

## What It Does

| Tool | Purpose |
|------|---------|
| `alive_load` | Load a walnut's full context -- identity, state, bundles, recent history |
| `alive_world` | List all walnuts with health signals (active/quiet/waiting) |
| `alive_search` | Search across all walnuts -- logs, insights, people, decisions |

## Smart Prefetch

Unlike other memory providers that inject context every turn, ALIVE uses **smart prefetch** -- context is injected only at transition points:

| Trigger | Injection |
|---------|-----------|
| Session start | Full walnut briefing |
| Walnut switch | New walnut's briefing |
| Post-compression | Re-orient with current state |
| Orientation query | "What am I working on?" |
| Normal working turn | Nothing (context already in conversation) |

This saves tokens and keeps the context window clean.

## Lifecycle Hooks

| Hook | Behavior |
|------|----------|
| `system_prompt_block()` | Lean ~50 token block. ALIVE availability + tool names. |
| `prefetch()` | Smart injection at transitions only. |
| `sync_turn()` | No-op. ALIVE captures explicitly, not every turn. |
| `on_session_end()` | Persists stash to `.alive/stash.json`, writes squirrel YAML. |
| `on_pre_compress()` | Flags for re-brief, preserves stash through compression. |
| `on_memory_write()` | Routes built-in memory writes to walnut insights. |
| `on_delegation()` | Observes subagent results, stashes findings. |

## Architecture

```
~/.hermes/plugins/memory/alive/
  __init__.py    <- MemoryProvider implementation
  plugin.yaml    <- metadata + hook declarations
  README.md      <- this file

Your ALIVE world (auto-detected or ALIVE_WORLD_ROOT):
  .alive/        <- world marker + squirrel entries
  02_Life/       <- personal walnuts
  04_Ventures/   <- business walnuts
  05_Experiments/ <- experiment walnuts
```

Each walnut has `_kernel/` with:
- `key.md` -- identity (type, goal, people, rhythm)
- `log.md` -- prepend-only history
- `insights.md` -- standing domain knowledge
- `now.json` -- computed current state (script-generated)

## Differentiation

| | Mem0 / Honcho / etc. | ALIVE |
|---|---|---|
| **Model** | Fact extraction from conversations | Structured world of context units |
| **Storage** | Cloud API / semantic DB | Local filesystem (zero dependencies) |
| **Granularity** | Individual memories | Walnuts with identity, history, knowledge |
| **Capture** | Automatic (every turn) | Explicit (stash mechanic, human approval) |
| **Compounding** | Accumulates facts | Compound loop: crons read/write walnuts |
| **Cross-project** | Flat namespace | Hierarchical: domains -> walnuts -> bundles |
| **Cost** | API fees | Zero (file reads/writes) |

## Zero Dependencies

Pure Python. Reads/writes plain files. No API keys. No network calls.
The only requirement is an ALIVE world on your filesystem.

## Links

- [ALIVE on GitHub](https://github.com/alivecontext/alive)
- [What is ALIVE?](https://alivecontext.com)
