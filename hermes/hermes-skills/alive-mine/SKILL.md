---
name: alive-mine
description: Deep context extraction from source material -- transcripts, documents, sessions. The archaeologist
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "mine"
  - "extract context"
  - "deep dive into"
  - "process this transcript"
  - "mine for context"
metadata:
  hermes:
    tags: [ALIVE, context, mining, extraction]
---

# Mine for Context

Deep extraction from source material. Creates reference bundles, builds extraction plans, tracks what's been mined.

## What Gets Mined

- Session transcripts (JSONL files)
- Meeting transcripts (Fathom, Otter)
- Email threads
- Documents, PDFs
- Slack/chat exports
- Any substantial text source

## Extraction Targets

For each source, extract:
1. **Decisions** -- what was decided, by whom, with rationale
2. **Tasks** -- action items with owners and deadlines
3. **People context** -- new information about known people, or new people
4. **Insights** -- domain knowledge that persists across sessions
5. **Quotes** -- sharp, memorable statements worth preserving
6. **Connections** -- references to other walnuts, bundles, or projects

## Process

1. **Read the source.** Full file, no skimming.
2. **Extract per category.** Group findings by type.
3. **Present for approval.** Nothing routes without the user seeing it.

```
Extracted from [source]:

Decisions (3):
  1. [decision] -> [[walnut]]
  2. [decision] -> [[walnut]]
  3. [decision] -> [[walnut]]

Tasks (2):
  4. [task] -> [[walnut]]
  5. [task] -> [[walnut]]

People (1):
  6. [person context] -> [[person]]

Insights (1):
  7. [insight] -> [[walnut]] insights

Approve all, or pick numbers to route?
```

4. **Route approved items.** Decisions -> log entries. Tasks -> tasks.py. People -> person walnuts. Insights -> insights.md.
5. **Mark source as mined.** Update bundle manifest `mining:` field.

## Mining State

Track in bundle manifest:
- `mining: active` -- still gathering sources
- `mining: paused` -- temporarily stopped
- `mining: exhausted` -- all known sources captured

## Batch Mining

For multiple sources (e.g., scanning all unsaved sessions):

```bash
# Find unmined squirrel entries
find "$ALIVE_WORLD_ROOT/.alive/_squirrels" -name "*.yaml" -newer "$ALIVE_WORLD_ROOT/.alive/_squirrels/.last-mined" 2>/dev/null
```

Process each, present aggregated results.
