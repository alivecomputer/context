---
name: alive-project
description: Regenerate now.json projections for all walnuts (mechanical, no approval needed)
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
metadata:
  hermes:
    tags: [ALIVE, cron, projection, mechanical]
    cron_schedule: "every 4h"
    cron_deliver: "local"
---

# Project Regeneration

Mechanical: regenerate `_kernel/now.json` for all walnuts. No approval needed -- this is computation from existing sources.

## Process

```bash
# Find world root
WORLD="${ALIVE_WORLD_ROOT:-$HOME/world}"

# Run projection for all walnuts
python3 "$WORLD/.alive/scripts/project.py" --all

# Regenerate world index
python3 "$WORLD/.alive/scripts/generate-index.py"
```

## Output

Report only if errors occurred. Otherwise silent (`[SILENT]`).

```
[SILENT]
Projections regenerated for [N] walnuts. No errors.
```

If errors:
```
Projection errors:
  [walnut]: [error message]
  [walnut]: [error message]

[N] walnuts projected successfully, [M] errors.
```

## Autonomy

This cron is fully autonomous. It reads existing source files (log.md, bundle manifests, tasks.json) and computes now.json. No decisions, no writes to human-authored files.
