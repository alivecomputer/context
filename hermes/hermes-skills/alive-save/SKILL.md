---
name: alive-save
description: Checkpoint -- route stash items, write log entry, update bundle, trigger projection, reset stash
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "save"
  - "checkpoint"
  - "let's save"
  - "stash is heavy"
metadata:
  hermes:
    tags: [ALIVE, context, save, checkpoint]
---

# Save

Route the stash, write the log, update the bundle, trigger projection.

## Pre-Save

1. Ask: "Anything else before I save?"
2. Scan back through conversation for missed stash items
3. Present stash grouped by type:

```
Stash (N items):

Decisions:
  1. [decision] -> [walnut]
  2. [decision] -> [walnut]

Tasks:
  3. [task] -> [walnut]

Notes:
  4. [note] -> [walnut]
  5. [note] -> [[person]]

Confirm, edit, or drop items?
```

## Write Log Entry

Prepend to `_kernel/log.md` after frontmatter. Use terminal:

```bash
# Read existing log, prepend new entry after frontmatter
```

Entry format:

```markdown
## YYYY-MM-DDTHH:MM:SS -- squirrel:[session_id]

**Type:** [feature/decision/research/strategy/etc]

[Narrative paragraph -- what happened, why, what it means]

### Decisions
- [decision with rationale]

### Artifacts
- [files created/modified]

### Next
[single next action]

signed: squirrel:[session_id]
```

## Update Active Bundle

Write to the active bundle's `context.manifest.yaml`:
- Update `context:` field with current state
- Update `status:` if changed

## Route Tasks

Use `tasks.py` via terminal:

```bash
python3 .alive/scripts/tasks.py add --walnut "[path]" --title "[task]" --priority "[priority]"
python3 .alive/scripts/tasks.py done --walnut "[path]" --id "[task_id]"
```

Never read or write tasks.json directly.

## Route People

Stash items tagged with `[[person-name]]` get dispatched to person walnuts as brief log entries.

## Post-Save

The post-save hook triggers `project.py` to regenerate `now.json`. This is automatic.

Update `_kernel/log.md` frontmatter:
- `last-entry:` timestamp
- `entry-count:` increment
- `summary:` one-line summary

## Zero-Context Check

Ask: "If a new agent loaded this walnut with no context, would it have everything it needs?"

If no, fix the log entry or manifest before finishing.

## Nudge Sharing

"Any bundles worth sharing?"

## Reset

Stash clears. Session continues.
