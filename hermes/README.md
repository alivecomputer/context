# ALIVE x Hermes Agent

Structured context layer for autonomous agents. Five independent layers that compound when used together.

## Architecture

| Layer | Name | Description |
|-------|------|-------------|
| 4 | Runtime Integration | `soul-patch.md` + `agents.md`. Auto-discovered by Hermes. |
| 3 | Cron Templates | 8 background jobs. Observe, present, await approval. |
| 2 | Hermes Skills | 11 ALIVE operations as `/slash` commands. agentskills.io format. |
| 1 | Memory Provider | Smart prefetch, 3 tools. PR to NousResearch/hermes-agent. |
| 0 | The World | Walnuts, bundles, `_kernel/`, `.alive/`. The shared filesystem. |

Each layer works independently. A user on Mem0 can use ALIVE skills and crons without the memory provider.

## Quick Start

### Path A: You already have ALIVE (Claude Code user)

```bash
# 1. Copy memory provider to Hermes
cp -r hermes/memory-provider/* ~/.hermes/hermes-agent/plugins/memory/alive/

# 2. Add skills to Hermes config
# In ~/.hermes/config.yaml:
# skills:
#   external_dirs:
#     - /path/to/alivecontext/alive/hermes/hermes-skills
#     - /path/to/alivecontext/alive/hermes/cron-templates

# 3. Activate memory provider
hermes memory setup  # select "alive"

# 4. Install crons (optional)
bash hermes/setup-crons.sh

# 5. Append SOUL.md patch
cat hermes/soul-patch.md >> ~/.hermes/SOUL.md

# 6. Copy AGENTS.md to world root
cp hermes/agents.md ~/world/AGENTS.md
```

### Path B: You're new to ALIVE (Hermes user)

```bash
# 1. Install ALIVE
claude plugin install alive@alivecontext

# 2. Then follow Path A above
```

## Directory Structure

```
hermes/
  memory-provider/           <- Layer 1: Hermes memory plugin
    __init__.py              <- MemoryProvider implementation
    plugin.yaml              <- Plugin metadata + hook declarations
    README.md                <- Provider docs

  hermes-skills/             <- Layer 2: 11 interactive skills
    alive-load/SKILL.md      <- Load walnut context
    alive-save/SKILL.md      <- Checkpoint: route stash, write log
    alive-world/SKILL.md     <- World dashboard
    alive-capture/SKILL.md   <- Capture external content
    alive-search/SKILL.md    <- Cross-walnut search
    alive-create/SKILL.md    <- Scaffold new walnut
    alive-bundle/SKILL.md    <- Bundle lifecycle
    alive-daily/SKILL.md     <- Morning operating system
    alive-history/SKILL.md   <- Session history search
    alive-mine/SKILL.md      <- Deep context extraction
    alive-cleanup/SKILL.md   <- System maintenance

  cron-templates/            <- Layer 3: 8 background enrichment jobs
    alive-morning/SKILL.md   <- 7am daily briefing
    alive-project/SKILL.md   <- Every 4h: regenerate projections
    alive-inbox/SKILL.md     <- Every 2h: scan inbox
    alive-health/SKILL.md    <- 9am daily: health check
    alive-stash-router/      <- Every 4h: route pending stash
    alive-mine/SKILL.md      <- 2am nightly: mine transcripts
    alive-prune/SKILL.md     <- 3am Sunday: log/insight pruning
    alive-people/SKILL.md    <- 9am Monday: people check

  agents.md                  <- Layer 4: Squirrel runtime rules
  soul-patch.md              <- Layer 4: 3-line personality patch
  setup-crons.sh             <- Installs all 8 crons in Hermes
  README.md                  <- This file
```

## Design Spec

Full 14-page specification: see `alive-hermes-spec.pdf` in the alivecomputer walnut's hermes-plugin bundle.

## Links

- [ALIVE Context System](https://github.com/alivecontext/alive)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [@stackwalnuts](https://x.com/stackwalnuts)
